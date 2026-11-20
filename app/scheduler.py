import logging
from datetime import datetime, timezone, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, func

from app.agent import generate_hint, run_agent_proactive
from app.database import AsyncSessionLocal
from app.leetcode_sync import sync_user_solved
from app.models import Submission, SubmissionStatus, User, Problem
from app.recommender import build_daily_plan, get_recommended_problem
from app.whatsapp import format_daily_plan_message, format_daily_problem, send_whatsapp_message
from app.config import settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Auto-seed: populate the problem database on first run
# ---------------------------------------------------------------------------

async def auto_seed_problems() -> None:
    """Seed the problem database if empty. Runs once on startup."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count(Problem.id)))
        count = result.scalar() or 0

        if count > 0:
            logger.info(f"Problem database already has {count} problems, skipping seed")
            return

        logger.info("Problem database is empty — auto-seeding from LeetCode...")
        from app.leetcode_client import fetch_all_problems
        from app.models import Difficulty as DiffEnum

        try:
            problems = await fetch_all_problems(max_problems=settings.auto_seed_max_problems)
        except Exception as e:
            logger.error(f"Auto-seed failed to fetch problems: {e}")
            return

        diff_map = {"Easy": DiffEnum.EASY, "Medium": DiffEnum.MEDIUM, "Hard": DiffEnum.HARD}
        added = 0

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
        logger.info(f"Auto-seeded {added} problems from LeetCode")


# ---------------------------------------------------------------------------
# Daily problem sender (existing, enhanced with streaks)
# ---------------------------------------------------------------------------

async def send_daily_problems() -> None:
    """Send daily problem plan to all active users based on their timezone/hour preference."""
    now_utc = datetime.now(pytz.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

        for user in users:
            try:
                tz = pytz.timezone(user.timezone)
                user_now = now_utc.astimezone(tz)

                if user_now.hour != user.daily_send_hour:
                    continue

                # Reset followup flag for the new day
                user.followup_sent_today = False

                # Sync solved problems from LeetCode before recommending
                if user.leetcode_username:
                    await sync_user_solved(db, user)

                # Build a full daily plan
                plan = await build_daily_plan(db, user, count=settings.daily_problem_count)
                if not plan:
                    logger.warning(f"No problems found for user {user.id}")
                    continue

                # Build streak info
                streak_text = ""
                if user.streak > 0:
                    streak_text = f"Streak: {user.streak} day{'s' if user.streak != 1 else ''}"
                elif user.longest_streak > 0:
                    streak_text = f"Your best streak was {user.longest_streak} days. Let's start a new one!"

                message = format_daily_plan_message(plan, streak_text)
                await send_whatsapp_message(user.whatsapp_number, message)

                # Record all problems as sent
                for item in plan:
                    submission = Submission(
                        user_id=user.id,
                        problem_id=item["problem"].id,
                        status=SubmissionStatus.SENT,
                    )
                    db.add(submission)

                logger.info(
                    f"Sent daily plan ({len(plan)} problems) to user {user.id}"
                )

            except Exception as e:
                logger.error(f"Failed to send to user {user.id}: {e}")

        await db.commit()


# ---------------------------------------------------------------------------
# Follow-up reminders: nudge users who haven't engaged
# ---------------------------------------------------------------------------

async def send_followup_reminders() -> None:
    """Send a nudge to users who have pending problems they haven't touched."""
    if not settings.enable_followups:
        return

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=settings.followup_after_hours)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

        for user in users:
            try:
                if user.followup_sent_today:
                    continue

                # Sync LeetCode first to auto-detect solves
                if user.leetcode_username:
                    await sync_user_solved(db, user)

                # Find pending problems sent before the cutoff
                sub_result = await db.execute(
                    select(Submission, Problem)
                    .join(Problem, Submission.problem_id == Problem.id)
                    .where(
                        Submission.user_id == user.id,
                        Submission.status == SubmissionStatus.SENT,
                        Submission.sent_at <= cutoff,
                    )
                )
                pending = sub_result.fetchall()

                if not pending:
                    continue

                problem_names = [f"'{prob.title}'" for _, prob in pending[:3]]
                remaining = len(pending)

                nudge = await run_agent_proactive(
                    f"[PROACTIVE_FOLLOWUP] The user has {remaining} unsolved problem(s) "
                    f"from {settings.followup_after_hours}+ hours ago: {', '.join(problem_names)}. "
                    f"Send a brief, friendly nudge. Maybe pick one and give a tiny hint. "
                    f"1-2 sentences max. Don't be pushy.",
                    db, user,
                )

                await send_whatsapp_message(user.whatsapp_number, nudge)
                user.followup_sent_today = True
                logger.info(f"Sent followup reminder to user {user.id}")

            except Exception as e:
                logger.error(f"Followup failed for user {user.id}: {e}")

        await db.commit()


# ---------------------------------------------------------------------------
# Weekly progress report
# ---------------------------------------------------------------------------

async def send_weekly_reports() -> None:
    """Send weekly progress report to all active users on their configured day."""
    if not settings.enable_weekly_reports:
        return

    now_utc = datetime.now(pytz.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

        for user in users:
            try:
                tz = pytz.timezone(user.timezone)
                user_now = now_utc.astimezone(tz)

                # Only send on the configured day and hour
                if user_now.weekday() != settings.weekly_report_day:
                    continue
                if user_now.hour != settings.weekly_report_hour:
                    continue

                # Count stats for the past week
                week_ago = now_utc - timedelta(days=7)

                solved_result = await db.execute(
                    select(func.count(Submission.id)).where(
                        Submission.user_id == user.id,
                        Submission.status == SubmissionStatus.SOLVED,
                        Submission.solved_at >= week_ago,
                    )
                )
                solved_this_week = solved_result.scalar() or 0

                attempted_result = await db.execute(
                    select(func.count(Submission.id)).where(
                        Submission.user_id == user.id,
                        Submission.status.in_([SubmissionStatus.ATTEMPTED, SubmissionStatus.SENT]),
                        Submission.sent_at >= week_ago,
                    )
                )
                unsolved_this_week = attempted_result.scalar() or 0

                total_solved_result = await db.execute(
                    select(func.count(Submission.id)).where(
                        Submission.user_id == user.id,
                        Submission.status == SubmissionStatus.SOLVED,
                    )
                )
                total_solved = total_solved_result.scalar() or 0

                # Build the report via AI for a natural tone
                report_prompt = (
                    f"[PROACTIVE_WEEKLY_REPORT] Generate a brief weekly progress report for the user.\n"
                    f"Stats this week: {solved_this_week} solved, {unsolved_this_week} still pending.\n"
                    f"Total solved all-time: {total_solved}.\n"
                    f"Current streak: {user.streak} days. Best streak: {user.longest_streak} days.\n"
                    f"Target company: {user.target_company or 'not set'}.\n"
                    f"Keep it encouraging, 3-5 sentences max. Use WhatsApp formatting."
                )

                report = await run_agent_proactive(report_prompt, db, user)
                await send_whatsapp_message(user.whatsapp_number, report)
                logger.info(f"Sent weekly report to user {user.id}")

            except Exception as e:
                logger.error(f"Weekly report failed for user {user.id}: {e}")


# ---------------------------------------------------------------------------
# Periodic LeetCode sync
# ---------------------------------------------------------------------------

async def periodic_leetcode_sync() -> None:
    """Sync all users' solved problems from LeetCode periodically."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(
                User.is_active == True,
                User.leetcode_username.isnot(None),
            )
        )
        users = result.scalars().all()

        for user in users:
            try:
                synced = await sync_user_solved(db, user)
                if synced > 0:
                    logger.info(f"Periodic sync: {synced} new solves for user {user.id}")
            except Exception as e:
                logger.error(f"Periodic sync failed for user {user.id}: {e}")


# ---------------------------------------------------------------------------
# Streak check: detect broken streaks and send reminders
# ---------------------------------------------------------------------------

async def check_streaks() -> None:
    """Check and update streaks for all users. Send reminders for at-risk streaks."""
    if not settings.enable_streak_reminders:
        return

    now_utc = datetime.now(pytz.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()

        for user in users:
            try:
                tz = pytz.timezone(user.timezone)
                user_now = now_utc.astimezone(tz)
                user_today = user_now.strftime("%Y-%m-%d")
                user_yesterday = (user_now - timedelta(days=1)).strftime("%Y-%m-%d")

                # Only run this check in the evening (8pm local)
                if user_now.hour != 20:
                    continue

                # If they already solved today, streak is fine
                if user.last_solve_date == user_today:
                    continue

                # If they had a streak and last solved yesterday, they're at risk
                if user.streak > 0 and user.last_solve_date == user_yesterday:
                    # They haven't solved today yet — send a streak reminder
                    reminder = await run_agent_proactive(
                        f"[PROACTIVE_STREAK_REMINDER] The user has a {user.streak}-day streak "
                        f"but hasn't solved a problem today yet. It's 8pm their time. "
                        f"Send a brief, friendly reminder to keep their streak alive. "
                        f"1-2 sentences max.",
                        db, user,
                    )
                    await send_whatsapp_message(user.whatsapp_number, reminder)
                    logger.info(f"Sent streak reminder to user {user.id} (streak: {user.streak})")

                # If they missed yesterday entirely, break the streak
                elif user.streak > 0 and user.last_solve_date and user.last_solve_date < user_yesterday:
                    user.streak = 0

            except Exception as e:
                logger.error(f"Streak check failed for user {user.id}: {e}")

        await db.commit()


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    # Daily problems — runs every hour, filters by user preference
    scheduler.add_job(
        send_daily_problems,
        trigger=CronTrigger(minute=0),
        id="daily_problems",
        replace_existing=True,
    )

    # Follow-up reminders — check every 2 hours
    scheduler.add_job(
        send_followup_reminders,
        trigger=CronTrigger(minute=30, hour="*/2"),
        id="followup_reminders",
        replace_existing=True,
    )

    # Weekly progress reports — runs every hour on configured day, filters by user
    scheduler.add_job(
        send_weekly_reports,
        trigger=CronTrigger(minute=0),
        id="weekly_reports",
        replace_existing=True,
    )

    # Periodic LeetCode sync
    scheduler.add_job(
        periodic_leetcode_sync,
        trigger=IntervalTrigger(hours=settings.sync_interval_hours),
        id="leetcode_sync",
        replace_existing=True,
    )

    # Streak checks — runs every hour, filters by user timezone (8pm local)
    scheduler.add_job(
        check_streaks,
        trigger=CronTrigger(minute=5),
        id="streak_checks",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: daily problems, followups, weekly reports, "
        "leetcode sync, streak checks"
    )


def stop_scheduler() -> None:
    scheduler.shutdown()
