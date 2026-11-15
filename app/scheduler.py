import logging
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.agent import generate_hint
from app.database import AsyncSessionLocal
from app.leetcode_sync import sync_user_solved
from app.models import Submission, SubmissionStatus, User
from app.recommender import get_recommended_problem
from app.whatsapp import format_daily_problem, send_whatsapp_message

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def send_daily_problems() -> None:
    """Send daily problem to all active users based on their timezone/hour preference."""
    now_utc = datetime.now(pytz.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

        for user in users:
            try:
                tz = pytz.timezone(user.timezone)
                user_now = now_utc.astimezone(tz)

                # Only send if current hour matches user's preferred send hour
                if user_now.hour != user.daily_send_hour:
                    continue

                # Sync solved problems from LeetCode before recommending
                if user.leetcode_username:
                    await sync_user_solved(db, user)

                problem = await get_recommended_problem(db, user)
                if not problem:
                    logger.warning(f"No problem found for user {user.id}")
                    continue

                hint = await generate_hint(
                    problem.title, problem.difficulty.value, problem.tags
                )
                message = format_daily_problem(
                    problem.title, problem.difficulty.value, problem.slug, hint
                )

                await send_whatsapp_message(user.whatsapp_number, message)

                submission = Submission(
                    user_id=user.id,
                    problem_id=problem.id,
                    status=SubmissionStatus.SENT,
                )
                db.add(submission)

                logger.info(f"Sent problem '{problem.title}' to user {user.id}")

            except Exception as e:
                logger.error(f"Failed to send to user {user.id}: {e}")

        await db.commit()


def start_scheduler() -> None:
    scheduler.add_job(
        send_daily_problems,
        trigger=CronTrigger(minute=0),  # runs every hour on the hour, filters by user pref
        id="daily_problems",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler() -> None:
    scheduler.shutdown()
