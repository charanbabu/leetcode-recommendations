import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator

from app.agent import generate_hint, run_agent
from app.config import settings
from app.database import get_db
from app.leetcode_sync import sync_company_problems, sync_user_solved
from app.models import CompanyProblem, Problem, Submission, SubmissionStatus, User
from app.recommender import get_recommended_problem
from app.schemas import UserCreate, UserOut
from app.whatsapp import format_daily_problem, send_whatsapp_message

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# User registration
# ---------------------------------------------------------------------------

@router.post("/users", response_model=UserOut, status_code=201)
async def register_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(User).where(User.whatsapp_number == payload.whatsapp_number)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="User already registered")

    user = User(**payload.model_dump())
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/users/{whatsapp_number}", response_model=UserOut)
async def get_user(whatsapp_number: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.whatsapp_number == whatsapp_number)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.delete("/users/{whatsapp_number}", status_code=204)
async def deactivate_user(whatsapp_number: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.whatsapp_number == whatsapp_number)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    await db.commit()


# ---------------------------------------------------------------------------
# WhatsApp webhook (Twilio sends POST here on incoming messages)
# ---------------------------------------------------------------------------

@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    From: str = Form(...),
    Body: str = Form(...),
):
    # Verify Twilio signature in production
    if settings.environment != "development":
        validator = RequestValidator(settings.twilio_auth_token)
        form_data = dict(await request.form())
        signature = request.headers.get("X-Twilio-Signature", "")
        url = str(request.url)
        if not validator.validate(url, form_data, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    sender = From  # e.g. whatsapp:+14155551234
    body_stripped = Body.strip()

    result = await db.execute(
        select(User).where(User.whatsapp_number == sender)
    )
    user = result.scalar_one_or_none()

    is_new = False
    if not user:
        # Auto-register with defaults
        user = User(
            whatsapp_number=sender,
            leetcode_username=settings.default_leetcode_username,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        await sync_user_solved(db, user)
        is_new = True

    # Prepend context for the agent if new user
    agent_input = body_stripped
    if is_new:
        agent_input = (
            "[SYSTEM: This is a brand new user who just registered. "
            "Welcome them and tell them they can set their LeetCode username, "
            "target company, or just ask for a problem.]\n\n"
            f"User message: {body_stripped}"
        )

    # Run the AI agent
    try:
        response = await run_agent(agent_input, db, user)
    except Exception as e:
        logger.error(f"Agent error for user {user.id}: {e}")
        response = "Something went wrong on my end. Try again in a moment!"

    try:
        await send_whatsapp_message(sender, response)
    except Exception as e:
        logger.error(f"Failed to send WhatsApp message to {sender}: {e}")
        return {"status": "send_failed", "error": str(e)}

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin: seed problems from LeetCode
# ---------------------------------------------------------------------------

@router.post("/admin/seed-problems", status_code=200)
async def seed_problems(db: AsyncSession = Depends(get_db)):
    from app.leetcode_client import fetch_all_problems
    from app.models import Difficulty as DiffEnum

    problems = await fetch_all_problems(max_problems=500)
    added = 0

    diff_map = {"Easy": DiffEnum.EASY, "Medium": DiffEnum.MEDIUM, "Hard": DiffEnum.HARD}

    for p in problems:
        existing = await db.execute(
            select(Problem).where(Problem.leetcode_id == int(p["frontendQuestionId"]))
        )
        if existing.scalar_one_or_none():
            continue

        tags = ",".join(t["name"] for t in p.get("topicTags", []))
        problem = Problem(
            leetcode_id=int(p["frontendQuestionId"]),
            slug=p["titleSlug"],
            title=p["title"],
            difficulty=diff_map.get(p["difficulty"], DiffEnum.MEDIUM),
            tags=tags or None,
            ac_rate=p.get("acRate"),
        )
        db.add(problem)
        added += 1

    await db.commit()
    return {"seeded": added}


# ---------------------------------------------------------------------------
# Manual trigger (for testing)
# ---------------------------------------------------------------------------

@router.post("/admin/send-now/{user_id}")
async def send_now(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    problem = await get_recommended_problem(db, user)
    if not problem:
        raise HTTPException(status_code=404, detail="No problem available")

    hint = await generate_hint(problem.title, problem.difficulty.value, problem.tags)
    message = format_daily_problem(problem.title, problem.difficulty.value, problem.slug, hint)
    await send_whatsapp_message(user.whatsapp_number, message)

    submission = Submission(user_id=user.id, problem_id=problem.id, status=SubmissionStatus.SENT)
    db.add(submission)
    await db.commit()

    return {"sent": problem.title, "to": user.whatsapp_number}


# ---------------------------------------------------------------------------
# Sync solved problems from LeetCode
# ---------------------------------------------------------------------------

@router.post("/admin/sync/{user_id}")
async def sync_solved(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.leetcode_username:
        raise HTTPException(status_code=400, detail="User has no LeetCode username set")

    synced = await sync_user_solved(db, user)
    return {"synced": synced, "username": user.leetcode_username}


@router.post("/admin/seed-company/{company_slug}")
async def seed_company(company_slug: str, db: AsyncSession = Depends(get_db)):
    synced = await sync_company_problems(db, company_slug)
    return {"company": company_slug, "problems_linked": synced}
