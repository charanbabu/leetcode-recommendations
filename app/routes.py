import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator

from app.agent import generate_hint, run_agent
from app.config import settings
from app.database import get_db
from app.leetcode_sync import sync_company_problems, sync_user_solved
from app.models import CompanyProblem, Problem, Submission, SubmissionStatus, User
from app.recommender import build_daily_plan, get_interview_problem, get_recommended_problem, get_topic_coverage
from app.schemas import (
    DailyPlanOut, DailyPlanProblem, LeetCodeStats, NextProblemOut, ProblemOut,
    SolvedProblemOut, UserCreate, UserOut, UserProfileOut,
)
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

    # Validate LeetCode username if provided
    if payload.leetcode_username:
        from app.leetcode_client import validate_username
        try:
            exists = await validate_username(payload.leetcode_username)
            if not exists:
                raise HTTPException(
                    status_code=400,
                    detail=f"LeetCode username '{payload.leetcode_username}' does not exist"
                )
        except HTTPException:
            raise
        except Exception:
            pass  # If LeetCode is down, allow registration and validate later

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
# User profile + interview prep (by user_id)
# ---------------------------------------------------------------------------

@router.get("/users/id/{user_id}", response_model=UserProfileOut)
async def get_user_profile(user_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve full user profile by user ID: solve history, topic coverage, gaps."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Sync from LeetCode first so we have the latest data
    if user.leetcode_username:
        await sync_user_solved(db, user)

    # Get topic coverage analysis
    coverage = await get_topic_coverage(db, user)

    # Get recent solved problems
    recent_result = await db.execute(
        select(Submission, Problem)
        .join(Problem, Submission.problem_id == Problem.id)
        .where(
            Submission.user_id == user.id,
            Submission.status == SubmissionStatus.SOLVED,
        )
        .order_by(Submission.solved_at.desc())
        .limit(20)
    )
    recent_solves = []
    for sub, prob in recent_result.fetchall():
        recent_solves.append(SolvedProblemOut(
            id=prob.id,
            leetcode_id=prob.leetcode_id,
            slug=prob.slug,
            title=prob.title,
            difficulty=prob.difficulty,
            tags=prob.tags,
            solved_at=sub.solved_at,
        ))

    return UserProfileOut(
        user=UserOut.model_validate(user),
        leetcode_stats=LeetCodeStats(
            total_solved=user.lc_total_solved,
            easy_solved=user.lc_easy_solved,
            medium_solved=user.lc_medium_solved,
            hard_solved=user.lc_hard_solved,
        ),
        tracked_solved=coverage["total_solved"],
        coverage_pct=coverage["coverage_pct"],
        weakest_topics=coverage["weakest_topics"],
        strongest_topics=coverage["strongest_topics"],
        topic_coverage=coverage["topics"],
        recent_solves=recent_solves,
        streak=user.streak,
        longest_streak=user.longest_streak,
    )


def _generate_connect_token(user_id: int) -> str:
    """Generate a simple HMAC token for the premium connect link."""
    secret = settings.twilio_auth_token  # reuse an existing secret
    msg = f"connect-premium-{user_id}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


class ConnectPremiumRequest(BaseModel):
    session_cookie: str
    csrftoken: str | None = None


@router.get("/users/id/{user_id}/connect-link")
async def get_connect_link(user_id: int, db: AsyncSession = Depends(get_db)):
    """Generate a one-click link for the user to connect their LeetCode Premium."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    token = _generate_connect_token(user_id)
    link = f"{settings.base_url}/connect-leetcode?user_id={user_id}&token={token}"
    return {"link": link, "user_id": user_id}


@router.post("/users/id/{user_id}/connect-premium")
async def connect_premium(
    user_id: int,
    payload: ConnectPremiumRequest,
    db: AsyncSession = Depends(get_db),
    x_connect_token: str = Header(None),
):
    """Store the user's LeetCode premium session cookie."""
    # Verify token
    expected = _generate_connect_token(user_id)
    if not x_connect_token or not hmac.compare_digest(x_connect_token, expected):
        raise HTTPException(status_code=403, detail="Invalid or expired connect token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Validate the cookie works by trying to fetch company data
    from app.leetcode_client import fetch_company_problems
    test_problems = await fetch_company_problems(
        "google",
        session_cookie=payload.session_cookie,
        csrftoken=payload.csrftoken,
    )

    if not test_problems:
        raise HTTPException(
            status_code=400,
            detail="Cookie didn't work - make sure you're logged into LeetCode Premium and the cookie is fresh.",
        )

    user.lc_session_cookie = payload.session_cookie
    user.lc_csrftoken = payload.csrftoken
    user.is_premium = True
    await db.commit()

    # If user has a target company, sync company problems now
    company_msg = ""
    if user.target_company:
        linked = await sync_company_problems(
            db, user.target_company,
            session_cookie=payload.session_cookie,
            csrftoken=payload.csrftoken,
        )
        company_msg = f" Loaded {linked} {user.target_company} problems with frequency data."

    return {
        "status": "connected",
        "message": f"Premium connected! Verified with {len(test_problems)} Google problems.{company_msg} Go back to WhatsApp!",
    }


@router.get("/users/id/{user_id}/next-problem", response_model=NextProblemOut)
async def get_next_problem(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get the next unsolved interview prep problem for a user.

    Syncs their LeetCode profile first, analyzes topic coverage gaps,
    and recommends a problem they haven't solved, targeting their weakest area.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Sync latest from LeetCode
    if user.leetcode_username:
        await sync_user_solved(db, user)

    # Get coverage to report the gap being filled
    coverage = await get_topic_coverage(db, user)

    # Get interview-focused recommendation
    problem = await get_interview_problem(db, user)
    if not problem:
        raise HTTPException(status_code=404, detail="No unsolved problems available")

    # Figure out which gap this problem fills
    gap_topic = None
    gap_difficulty = None
    reason = "Recommended based on your overall progress."

    if problem.tags:
        tags = [t.strip() for t in problem.tags.split(",")]
        for tag in tags:
            if tag in coverage["topics"]:
                topic_data = coverage["topics"][tag]
                for diff in ("medium", "easy", "hard"):
                    if topic_data[diff]["solved"] < topic_data[diff]["target"]:
                        gap_topic = tag
                        gap_difficulty = diff
                        remaining = topic_data[diff]["target"] - topic_data[diff]["solved"]
                        reason = (
                            f"You need {remaining} more {diff} {tag} problem(s) "
                            f"for interview readiness. Coverage: {coverage['coverage_pct']}%."
                        )
                        break
            if gap_topic:
                break

    if not gap_topic and coverage["weakest_topics"]:
        gap_topic = coverage["weakest_topics"][0]
        reason = (
            f"Targeting your weakest area: {gap_topic}. "
            f"Coverage: {coverage['coverage_pct']}%."
        )

    # Record that we sent this problem
    submission = Submission(
        user_id=user.id,
        problem_id=problem.id,
        status=SubmissionStatus.SENT,
    )
    db.add(submission)
    await db.commit()

    return NextProblemOut(
        problem=ProblemOut.model_validate(problem),
        reason=reason,
        gap_topic=gap_topic,
        gap_difficulty=gap_difficulty,
        coverage_pct=coverage["coverage_pct"],
    )


@router.get("/users/id/{user_id}/coverage")
async def get_coverage(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get a compact topic coverage breakdown for a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.leetcode_username:
        await sync_user_solved(db, user)

    return await get_topic_coverage(db, user)


@router.get("/users/id/{user_id}/daily-plan", response_model=DailyPlanOut)
async def get_daily_plan(
    user_id: int,
    count: int = 6,
    db: AsyncSession = Depends(get_db),
):
    """Generate a daily plan of 5-6 problems with varied topics and difficulties.

    - Each problem targets a different topic (no two from the same primary topic)
    - Difficulty mix adjusts to user level (beginner/intermediate/advanced)
    - Prioritizes interview gaps, company problems, weak areas
    - Never repeats solved or already-sent problems
    """
    if count < 1 or count > 10:
        raise HTTPException(status_code=400, detail="count must be between 1 and 10")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Sync latest from LeetCode
    if user.leetcode_username:
        await sync_user_solved(db, user)

    plan = await build_daily_plan(db, user, count=count)
    if not plan:
        raise HTTPException(status_code=404, detail="No problems available for daily plan")

    # Record all as sent
    for item in plan:
        submission = Submission(
            user_id=user.id,
            problem_id=item["problem"].id,
            status=SubmissionStatus.SENT,
        )
        db.add(submission)
    await db.commit()

    # Get coverage for context
    coverage = await get_topic_coverage(db, user)

    # Build difficulty mix summary
    diff_mix: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    plan_items = []
    for item in plan:
        diff_mix[item["slot"]] += 1
        plan_items.append(DailyPlanProblem(
            problem=ProblemOut.model_validate(item["problem"]),
            reason=item["reason"],
            slot=item["slot"],
        ))

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return DailyPlanOut(
        user_id=user_id,
        date=today_str,
        problem_count=len(plan_items),
        problems=plan_items,
        coverage_pct=coverage["coverage_pct"],
        difficulty_mix=diff_mix,
    )


@router.get("/users/id/{user_id}/history")
async def get_solve_history(
    user_id: int,
    status: str | None = None,
    topic: str | None = None,
    difficulty: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get a user's full problem history with optional filters."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    query = (
        select(Submission, Problem)
        .join(Problem, Submission.problem_id == Problem.id)
        .where(Submission.user_id == user.id)
    )

    if status:
        try:
            status_enum = SubmissionStatus(status.lower())
            query = query.where(Submission.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if difficulty:
        from app.models import Difficulty
        try:
            diff_enum = Difficulty(difficulty.lower())
            query = query.where(Problem.difficulty == diff_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid difficulty: {difficulty}")

    if topic:
        query = query.where(Problem.tags.like(f"%{topic}%"))

    query = query.order_by(Submission.sent_at.desc())
    rows = await db.execute(query)

    history = []
    for sub, prob in rows.fetchall():
        history.append({
            "problem": {
                "id": prob.id,
                "leetcode_id": prob.leetcode_id,
                "slug": prob.slug,
                "title": prob.title,
                "difficulty": prob.difficulty.value,
                "tags": prob.tags,
            },
            "status": sub.status.value,
            "sent_at": sub.sent_at.isoformat() if sub.sent_at else None,
            "solved_at": sub.solved_at.isoformat() if sub.solved_at else None,
        })

    return {"user_id": user_id, "count": len(history), "history": history}


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

    # Track last activity
    user.last_active_at = datetime.now(timezone.utc)
    await db.commit()

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
