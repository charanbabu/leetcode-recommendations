import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.leetcode_client import fetch_company_problems, fetch_user_solved, fetch_user_stats
from app.models import CompanyProblem, Problem, Submission, SubmissionStatus, User

logger = logging.getLogger(__name__)


async def sync_user_solved(db: AsyncSession, user: User) -> int:
    """Sync a user's solved problems from LeetCode into our database.

    Returns the number of newly synced solved problems.
    """
    if not user.leetcode_username:
        return 0

    # Fetch real LeetCode stats (official counts) — always commit these
    try:
        lc_stats = await fetch_user_stats(user.leetcode_username)
        user.lc_total_solved = lc_stats["total"]
        user.lc_easy_solved = lc_stats["easy"]
        user.lc_medium_solved = lc_stats["medium"]
        user.lc_hard_solved = lc_stats["hard"]
        user.lc_tag_counts = json.dumps(lc_stats.get("tag_counts", {}))
        await db.commit()
        logger.info(
            f"LeetCode stats for {user.leetcode_username}: "
            f"{lc_stats['total']} total ({lc_stats['easy']}E/{lc_stats['medium']}M/{lc_stats['hard']}H)"
        )
    except Exception as e:
        logger.error(f"Failed to fetch LeetCode stats for {user.leetcode_username}: {e}")

    try:
        solved_slugs = await fetch_user_solved(user.leetcode_username)
    except Exception as e:
        logger.error(f"Failed to fetch solved problems for {user.leetcode_username}: {e}")
        return 0

    if not solved_slugs:
        return 0

    # Get problems we have in our DB that match the solved slugs
    result = await db.execute(
        select(Problem).where(Problem.slug.in_(solved_slugs))
    )
    solved_problems = result.scalars().all()

    # Get existing submissions for this user
    existing_result = await db.execute(
        select(Submission.problem_id).where(Submission.user_id == user.id)
    )
    existing_problem_ids = {row[0] for row in existing_result.fetchall()}

    synced = 0
    for problem in solved_problems:
        if problem.id in existing_problem_ids:
            # Update existing submission to SOLVED if not already
            sub_result = await db.execute(
                select(Submission).where(
                    Submission.user_id == user.id,
                    Submission.problem_id == problem.id,
                )
            )
            sub = sub_result.scalar_one_or_none()
            if sub and sub.status != SubmissionStatus.SOLVED:
                sub.status = SubmissionStatus.SOLVED
                sub.solved_at = datetime.now(timezone.utc)
                synced += 1
        else:
            # Create a new submission record marked as SOLVED
            submission = Submission(
                user_id=user.id,
                problem_id=problem.id,
                status=SubmissionStatus.SOLVED,
                solved_at=datetime.now(timezone.utc),
            )
            db.add(submission)
            synced += 1

    await db.commit()
    logger.info(f"Synced {synced} solved problems for user {user.id} ({user.leetcode_username})")
    return synced


async def sync_company_problems(
    db: AsyncSession,
    company_slug: str,
    session_cookie: str | None = None,
    csrftoken: str | None = None,
) -> int:
    """Fetch company-tagged problems from LeetCode and link them in our DB.

    Returns the number of problems linked.
    """
    company_slug = company_slug.lower().strip()

    try:
        questions = await fetch_company_problems(company_slug, session_cookie, csrftoken)
    except Exception as e:
        logger.error(f"Failed to fetch company problems for {company_slug}: {e}")
        return 0

    if not questions:
        return 0

    # Build a slug -> freq_bar mapping
    slug_freq = {}
    for q in questions:
        slug = q.get("titleSlug")
        freq = q.get("freqBar") or 0
        if slug:
            slug_freq[slug] = float(freq)

    # Find matching problems in our DB
    result = await db.execute(
        select(Problem).where(Problem.slug.in_(list(slug_freq.keys())))
    )
    problems = result.scalars().all()

    # Check which company-problem links already exist
    existing_result = await db.execute(
        select(CompanyProblem.problem_id).where(CompanyProblem.company == company_slug)
    )
    existing_ids = {row[0] for row in existing_result.fetchall()}

    linked = 0
    for problem in problems:
        if problem.id in existing_ids:
            continue
        cp = CompanyProblem(
            company=company_slug,
            problem_id=problem.id,
            frequency=slug_freq.get(problem.slug, 0.0),
        )
        db.add(cp)
        linked += 1

    await db.commit()
    logger.info(f"Linked {linked} problems for company '{company_slug}'")
    return linked
