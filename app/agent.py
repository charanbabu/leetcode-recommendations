import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import anthropic
import pytz
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ChatMessage, CompanyProblem, Difficulty, Problem, Submission, SubmissionStatus, User,
)
from app.recommender import get_recommended_problem, TOPIC_ORDER

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# ---------------------------------------------------------------------------
# Tool definitions for the agent
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_pending",
        "description": (
            "List all problems currently pending (sent but not yet solved). "
            "Use when the user asks 'what are my problems', 'show today's list', "
            "'what do I have pending', 'what should I do', etc."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mark_solved",
        "description": (
            "Mark a specific problem as solved. Use when the user says they solved a problem. "
            "They might say 'solved Two Sum', 'done with #3', 'finished problem 2', etc. "
            "Extract the problem title or number from their message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "problem_ref": {
                    "type": "string",
                    "description": "Problem title, slug, or number from the daily plan (e.g. 'Two Sum', '3', 'two-sum')",
                },
            },
            "required": ["problem_ref"],
        },
    },
    {
        "name": "skip_problem",
        "description": "Skip a specific problem. Use when the user wants to skip/replace one.",
        "input_schema": {
            "type": "object",
            "properties": {
                "problem_ref": {
                    "type": "string",
                    "description": "Problem title, slug, or number",
                },
            },
            "required": ["problem_ref"],
        },
    },
    {
        "name": "send_hint",
        "description": (
            "Give the user a hint for a specific problem. Use when they ask for help, "
            "a hint, a nudge, are stuck on a particular problem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "problem_ref": {
                    "type": "string",
                    "description": "Problem title, slug, or number. If not specified, use the most recent.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "explain_solution",
        "description": (
            "Explain the optimal solution/approach for a problem. Use when the user asks "
            "'how do I solve this', 'what's the approach for Two Sum', 'explain #3', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "problem_ref": {
                    "type": "string",
                    "description": "Problem title, slug, or number. If not specified, use the most recent.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "review_code",
        "description": "Review code the user submitted for a problem. Use when the user pastes code and wants feedback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The user's code to review",
                },
                "problem_ref": {
                    "type": "string",
                    "description": "Problem title, slug, or number the code is for",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_daily_plan",
        "description": (
            "Generate a daily plan of 4-6 problems with different topics and difficulty mixes. "
            "Use when the user asks for 'today's problems', 'daily plan', 'give me problems', "
            "'batch of problems', 'what should I solve today', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of problems (1-10, default 6)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "recommend_problem",
        "description": "Recommend a single new problem. Use when they want just one more problem.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_leetcode_username",
        "description": "Set or update the user's LeetCode username. Extracts username from URLs like https://leetcode.com/u/username/ too.",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "The LeetCode username or profile URL",
                },
            },
            "required": ["username"],
        },
    },
    {
        "name": "set_target_company",
        "description": "Set the company the user is preparing for (e.g. google, meta, amazon). Problems from that company will be prioritized.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {
                    "type": "string",
                    "description": "Company name (lowercase)",
                },
            },
            "required": ["company"],
        },
    },
    {
        "name": "get_user_stats",
        "description": "Get the user's progress stats: problems solved by difficulty, weak topics, company target, streaks, etc.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "sync_leetcode_profile",
        "description": (
            "Sync the user's solved problems from their LeetCode profile right now. "
            "Use when the user says 'I just solved it on LeetCode', 'sync my profile', "
            "'refresh my stats', 'update from LeetCode', etc."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_interview_plan",
        "description": (
            "Show the user's interview prep coverage: which topics are covered, where the gaps are. "
            "Use when they ask about interview readiness, prep plan, 'what topics am I missing', etc."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_interview_problem",
        "description": "Recommend the next problem specifically for interview prep - targets the user's weakest topic gap.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_leetcode_session",
        "description": (
            "Set the user's LeetCode premium session cookie to unlock company-specific "
            "interview frequency data. Use when the user pastes a long token/cookie string "
            "or says 'here is my session', 'my cookie is...', 'premium login', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_cookie": {
                    "type": "string",
                    "description": "The LEETCODE_SESSION cookie value",
                },
                "csrftoken": {
                    "type": "string",
                    "description": "The csrftoken cookie value (optional)",
                },
            },
            "required": ["session_cookie"],
        },
    },
    {
        "name": "explain_topic",
        "description": "Explain a data structure or algorithm topic. Use when they ask 'what is DP', 'explain graphs', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The topic to explain",
                },
            },
            "required": ["topic"],
        },
    },
]

SYSTEM_PROMPT = """You are a friendly LeetCode coaching assistant on WhatsApp. You help users practice coding problems daily, track their progress, and prepare for technical interviews.

Your personality: encouraging, concise (this is WhatsApp - keep messages short), and knowledgeable about algorithms and data structures.

KEY WORKFLOW:
- You send the user 4-6 problems daily (automated) or on request, with varied topics and difficulties
- The user goes to LeetCode to solve them and submits solutions THERE, not on WhatsApp
- You automatically sync their LeetCode profile periodically to detect what they've solved
- When they tell you they solved something, use sync_leetcode_profile to pick up new solves, then congratulate them
- You answer any questions they have: hints, explain approaches, discuss topics, review code, etc.

You have tools to:
- List their pending (unsent) problems
- Generate a daily plan (4-6 problems with varied topics and difficulty mix)
- Mark problems solved/skipped by title or number
- Sync their LeetCode profile to detect new solves
- Give hints or explain solutions for specific problems (by title or number)
- Review code they paste
- Show interview prep coverage and gaps
- Recommend interview-focused problems
- Set their LeetCode username or target company
- Show progress stats
- Explain algorithm/DS topics

Guidelines:
- Keep responses SHORT. This is WhatsApp, not an essay. 2-4 sentences max unless explaining a topic/solution.
- Use WhatsApp formatting: *bold*, _italic_, ```code```
- When a user refers to a problem by number (e.g. "#3", "problem 2"), match it to their pending list order
- When a user says they solved something, sync their LeetCode profile first to auto-detect, then confirm
- If they seem stuck, proactively offer a hint
- If they just solved something, congratulate them and mention their streak
- Celebrate milestones: streaks (3, 7, 14, 30 days), total solves (10, 25, 50, 100)
- When recommending problems, always use tools - don't make up problems
- The daily plan has varied difficulty combos (e.g. 2 Hard, 2 Medium, 1 Easy) and covers DIFFERENT topics each day for holistic preparation
- If the user has LeetCode Premium and sets a target company, they'll get a link to connect their premium account (one-click web page). Once connected, problems show interview frequency (e.g. "Asked 45x at Google in last 6 months")
- Users can also paste their LEETCODE_SESSION cookie directly via set_leetcode_session if they prefer"""


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def execute_tool(
    tool_name: str,
    tool_input: dict,
    db: AsyncSession,
    user: User,
) -> str:
    """Execute a tool and return the result as a string for the agent."""

    if tool_name == "list_pending":
        return await _tool_list_pending(db, user)

    elif tool_name == "mark_solved":
        return await _tool_mark_solved(db, user, tool_input["problem_ref"])

    elif tool_name == "skip_problem":
        return await _tool_skip_problem(db, user, tool_input["problem_ref"])

    elif tool_name == "send_hint":
        return await _tool_send_hint(db, user, tool_input.get("problem_ref"))

    elif tool_name == "explain_solution":
        return await _tool_explain_solution(db, user, tool_input.get("problem_ref"))

    elif tool_name == "review_code":
        return await _tool_review_code(db, user, tool_input["code"], tool_input.get("problem_ref"))

    elif tool_name == "get_daily_plan":
        return await _tool_daily_plan(db, user, tool_input.get("count", 6))

    elif tool_name == "recommend_problem":
        return await _tool_recommend_problem(db, user)

    elif tool_name == "set_leetcode_username":
        return await _tool_set_username(db, user, tool_input["username"])

    elif tool_name == "set_target_company":
        return await _tool_set_company(db, user, tool_input["company"])

    elif tool_name == "get_user_stats":
        return await _tool_get_stats(db, user)

    elif tool_name == "sync_leetcode_profile":
        return await _tool_sync_profile(db, user)

    elif tool_name == "get_interview_plan":
        return await _tool_interview_plan(db, user)

    elif tool_name == "get_interview_problem":
        return await _tool_interview_problem(db, user)

    elif tool_name == "set_leetcode_session":
        return await _tool_set_session(db, user, tool_input["session_cookie"], tool_input.get("csrftoken"))

    elif tool_name == "explain_topic":
        return tool_input["topic"]  # Agent will explain in its response

    return "Unknown tool"


# ---------------------------------------------------------------------------
# Multi-problem helpers
# ---------------------------------------------------------------------------

async def _get_pending_submissions(db: AsyncSession, user: User) -> list[tuple[Submission, Problem]]:
    """Get all pending (sent/attempted, not solved/skipped) submissions with their problems."""
    result = await db.execute(
        select(Submission, Problem)
        .join(Problem, Submission.problem_id == Problem.id)
        .where(
            Submission.user_id == user.id,
            Submission.status.in_([SubmissionStatus.SENT, SubmissionStatus.ATTEMPTED]),
        )
        .order_by(Submission.sent_at.desc())
    )
    return result.fetchall()


async def _find_submission(
    db: AsyncSession, user: User, problem_ref: str | None
) -> tuple[Submission, Problem] | None:
    """Find a specific pending submission by title, slug, or number in pending list."""
    pending = await _get_pending_submissions(db, user)
    if not pending:
        return None

    if not problem_ref:
        # Default to most recent
        return pending[0]

    ref = problem_ref.strip().lstrip("#")

    # Try as number (1-indexed position in pending list)
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(pending):
            return pending[idx]

    # Try title/slug match (case-insensitive, partial)
    ref_lower = ref.lower()
    for sub, prob in pending:
        if ref_lower in prob.title.lower() or ref_lower in prob.slug.lower():
            return (sub, prob)

    # Try across ALL submissions (not just pending) for solved tracking
    all_result = await db.execute(
        select(Submission, Problem)
        .join(Problem, Submission.problem_id == Problem.id)
        .where(Submission.user_id == user.id)
        .order_by(Submission.sent_at.desc())
    )
    for sub, prob in all_result.fetchall():
        if ref_lower in prob.title.lower() or ref_lower in prob.slug.lower():
            return (sub, prob)

    return None


def _update_streak(user: User) -> str:
    """Update streak after a solve. Returns streak message."""
    tz = pytz.timezone(user.timezone)
    today_str = datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(timezone.utc).astimezone(tz) - timedelta(days=1)).strftime("%Y-%m-%d")

    if user.last_solve_date == today_str:
        pass  # already solved today
    elif user.last_solve_date == yesterday_str or user.last_solve_date is None:
        user.streak += 1
    else:
        user.streak = 1

    user.last_solve_date = today_str
    if user.streak > user.longest_streak:
        user.longest_streak = user.streak
    user.last_active_at = datetime.now(timezone.utc)

    streak_msg = ""
    if user.streak >= 2:
        streak_msg = f" Streak: {user.streak} days!"
    if user.streak == user.longest_streak and user.streak >= 3:
        streak_msg += " New personal best!"
    return streak_msg


async def _solved_count(db: AsyncSession, user: User) -> int:
    result = await db.execute(
        select(func.count(Submission.id)).where(
            Submission.user_id == user.id,
            Submission.status == SubmissionStatus.SOLVED,
        )
    )
    return result.scalar() or 0


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_list_pending(db: AsyncSession, user: User) -> str:
    pending = await _get_pending_submissions(db, user)
    if not pending:
        return "NO_PENDING: No problems pending. Use get_daily_plan to get today's problems!"

    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
    lines = [f"PENDING_PROBLEMS: {len(pending)} problems pending\n"]
    for i, (sub, prob) in enumerate(pending, 1):
        emoji = difficulty_emoji.get(prob.difficulty.value, "⚪")
        status_tag = " (working on it)" if sub.status == SubmissionStatus.ATTEMPTED else ""
        lines.append(
            f"{i}. {emoji} *{prob.title}* ({prob.difficulty.value.capitalize()}){status_tag}\n"
            f"   {prob.tags or 'General'}\n"
            f"   https://leetcode.com/problems/{prob.slug}/"
        )
    return "\n".join(lines)


async def _tool_mark_solved(db: AsyncSession, user: User, problem_ref: str) -> str:
    match = await _find_submission(db, user, problem_ref)
    if not match:
        return f"NOT_FOUND: Couldn't find a problem matching '{problem_ref}' in your list. Use list_pending to see your problems."

    sub, prob = match
    if sub.status == SubmissionStatus.SOLVED:
        return f"ALREADY_SOLVED: '{prob.title}' is already marked as solved."

    sub.status = SubmissionStatus.SOLVED
    sub.solved_at = datetime.now(timezone.utc)
    streak_msg = _update_streak(user)
    await db.commit()

    total = await _solved_count(db, user)
    pending = await _get_pending_submissions(db, user)
    remaining = len(pending)

    return (
        f"SOLVED: Marked '{prob.title}' as solved! Total: {total}.{streak_msg}\n"
        f"Remaining today: {remaining} problem{'s' if remaining != 1 else ''}."
    )


async def _tool_skip_problem(db: AsyncSession, user: User, problem_ref: str) -> str:
    match = await _find_submission(db, user, problem_ref)
    if not match:
        return f"NOT_FOUND: Couldn't find '{problem_ref}' in your pending list."

    sub, prob = match
    sub.status = SubmissionStatus.SKIPPED
    await db.commit()
    return f"SKIPPED: Skipped '{prob.title}'."


async def _tool_send_hint(db: AsyncSession, user: User, problem_ref: str | None) -> str:
    match = await _find_submission(db, user, problem_ref)
    if not match:
        if problem_ref:
            return f"NOT_FOUND: Couldn't find '{problem_ref}' in your list."
        return "NO_ACTIVE_PROBLEM: No pending problems. Use get_daily_plan to get problems first!"

    sub, prob = match
    sub.status = SubmissionStatus.ATTEMPTED
    await db.commit()

    tag_context = f"Topics: {prob.tags}" if prob.tags else ""
    return (
        f"HINT_REQUEST for '{prob.title}' (difficulty: {prob.difficulty.value}). {tag_context}\n"
        "Generate a helpful 2-3 sentence hint pointing toward the right approach/data structure. "
        "Do NOT give the full solution."
    )


async def _tool_explain_solution(db: AsyncSession, user: User, problem_ref: str | None) -> str:
    match = await _find_submission(db, user, problem_ref)
    if not match:
        if problem_ref:
            return f"NOT_FOUND: Couldn't find '{problem_ref}' in your list."
        return "NO_ACTIVE_PROBLEM: No pending problems."

    sub, prob = match

    # Fetch full description if we don't have it
    if not prob.description:
        from app.leetcode_client import fetch_problem_detail
        try:
            detail = await fetch_problem_detail(prob.slug)
            if detail and detail.get("content"):
                prob.description = detail["content"]
                await db.commit()
        except Exception:
            pass

    tag_context = f"Topics: {prob.tags}" if prob.tags else ""
    desc = f"Problem description:\n{prob.description}" if prob.description else ""

    return (
        f"EXPLAIN_SOLUTION for '{prob.title}' ({prob.difficulty.value}). {tag_context}\n"
        f"{desc}\n\n"
        "Explain the optimal solution clearly:\n"
        "1. Key intuition - why does this approach work?\n"
        "2. Step-by-step approach\n"
        "3. Python code solution\n"
        "4. Time and space complexity\n"
        "Keep it concise but thorough."
    )


async def _tool_review_code(db: AsyncSession, user: User, code: str, problem_ref: str | None) -> str:
    match = await _find_submission(db, user, problem_ref)
    if not match:
        if problem_ref:
            return f"NOT_FOUND: Couldn't find '{problem_ref}'. Review the code anyway based on its content."
        # Still review even without a match
        return (
            f"REVIEW_CODE (no specific problem context):\n```\n{code}\n```\n"
            "Give feedback: correctness, time/space complexity, and improvements."
        )

    sub, prob = match
    sub.user_code = code
    sub.status = SubmissionStatus.ATTEMPTED
    await db.commit()

    return (
        f"REVIEW_CODE for '{prob.title}' ({prob.difficulty.value}):\n"
        f"```\n{code}\n```\n"
        "Give brief feedback: correctness, time/space complexity, and one improvement. Keep it under 150 words."
    )


async def _tool_daily_plan(db: AsyncSession, user: User, count: int) -> str:
    from app.recommender import build_daily_plan, get_topic_coverage
    from app.leetcode_client import fetch_problem_company_stats

    count = max(1, min(10, count or 6))

    if user.leetcode_username:
        from app.leetcode_sync import sync_user_solved
        await sync_user_solved(db, user)

    plan = await build_daily_plan(db, user, count=count)
    if not plan:
        return "NO_PROBLEMS: No problems available for a daily plan."

    # Record all as sent
    for item in plan:
        submission = Submission(
            user_id=user.id,
            problem_id=item["problem"].id,
            status=SubmissionStatus.SENT,
        )
        db.add(submission)
    await db.commit()

    # Fetch company frequency data if user is premium and has a target company
    company_info: dict[int, str] = {}  # problem_id -> frequency info string
    if user.is_premium and user.target_company and user.lc_session_cookie:
        target = user.target_company.lower()
        for item in plan:
            p = item["problem"]
            try:
                stats = await fetch_problem_company_stats(
                    p.slug, user.lc_session_cookie, user.lc_csrftoken
                )
                for company_name, entries in stats.items():
                    if target in company_name.lower():
                        recent = next((e for e in entries if "0-6" in e["time_range"]), None)
                        if recent and recent["times_encountered"] > 0:
                            company_info[p.id] = f"Asked {recent['times_encountered']}x at {company_name} (last 6mo)"
                        break
            except Exception:
                pass

    coverage = await get_topic_coverage(db, user)
    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}

    diff_counts: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    for item in plan:
        diff_counts[item["slot"]] += 1
    mix_parts = [f"{v} {k.capitalize()}" for k, v in diff_counts.items() if v > 0]

    lines = [
        f"DAILY_PLAN: {len(plan)} problems | Mix: {' + '.join(mix_parts)} | Coverage: {coverage['coverage_pct']}%\n"
    ]
    for i, item in enumerate(plan, 1):
        p = item["problem"]
        emoji = difficulty_emoji.get(item["slot"], "⚪")
        freq = company_info.get(p.id, "")
        freq_line = f"\n   📊 {freq}" if freq else ""
        lines.append(
            f"{i}. {emoji} *{p.title}* ({item['slot'].capitalize()})\n"
            f"   {item['reason']}{freq_line}\n"
            f"   https://leetcode.com/problems/{p.slug}/"
        )

    lines.append(f"\nSolve on LeetCode, then tell me and I'll sync your progress!")
    return "\n".join(lines)


async def _tool_recommend_problem(db: AsyncSession, user: User) -> str:
    problem = await get_recommended_problem(db, user)
    if not problem:
        return "NO_PROBLEMS: No problems available. The database may need to be seeded."

    submission = Submission(
        user_id=user.id,
        problem_id=problem.id,
        status=SubmissionStatus.SENT,
    )
    db.add(submission)
    await db.commit()

    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(problem.difficulty.value, "⚪")
    tags = problem.tags or "General"
    ac_str = f"\nAcceptance rate: {problem.ac_rate:.1f}%" if problem.ac_rate else ""

    return (
        f"RECOMMENDED: {difficulty_emoji} *{problem.title}*\n"
        f"Difficulty: {problem.difficulty.value.capitalize()}\n"
        f"Topics: {tags}\n"
        f"Link: https://leetcode.com/problems/{problem.slug}/"
        f"{ac_str}"
    )


async def _tool_set_username(db: AsyncSession, user: User, raw: str) -> str:
    from app.leetcode_client import validate_username
    from app.leetcode_sync import sync_user_solved

    username = raw.rstrip("/").split("/")[-1].strip()

    # Validate the username exists on LeetCode
    try:
        exists = await validate_username(username)
    except Exception:
        return f"ERROR: Couldn't reach LeetCode to verify '{username}'. Try again in a moment."

    if not exists:
        return (
            f"INVALID_USERNAME: '{username}' does not exist on LeetCode. "
            f"Please check the spelling and try again. "
            f"You can also paste your profile URL (e.g. https://leetcode.com/u/yourname/)."
        )

    user.leetcode_username = username
    await db.commit()

    synced = await sync_user_solved(db, user)
    await db.refresh(user)

    return (
        f"USERNAME_SET: LeetCode username set to '{username}'. "
        f"Profile shows {user.lc_total_solved} problems solved "
        f"({user.lc_easy_solved} Easy, {user.lc_medium_solved} Medium, {user.lc_hard_solved} Hard). "
        f"Synced {synced} recent solves into our tracking DB."
    )


async def _tool_set_company(db: AsyncSession, user: User, company: str) -> str:
    from app.leetcode_sync import sync_company_problems

    company = company.lower().strip()
    user.target_company = company
    await db.commit()

    existing = await db.execute(
        select(CompanyProblem).where(CompanyProblem.company == company).limit(1)
    )
    if not existing.scalar_one_or_none():
        linked = await sync_company_problems(
            db, company,
            session_cookie=user.lc_session_cookie,
            csrftoken=user.lc_csrftoken,
        )
        if linked > 0:
            return (
                f"COMPANY_SET: Target company set to '{company}'. "
                f"Loaded {linked} company-tagged problems with interview frequency data. "
                f"Future recommendations will prioritize high-frequency {company} problems."
            )
        elif not user.is_premium:
            from app.routes import _generate_connect_token
            token = _generate_connect_token(user.id)
            link = f"{settings.base_url}/connect-leetcode?user_id={user.id}&token={token}"
            return (
                f"COMPANY_SET: Target company set to '{company}'. "
                f"To get company-specific interview frequency data (how many times each problem "
                f"was asked), connect your LeetCode Premium account here:\n{link}"
            )
        else:
            return f"COMPANY_SET: Target company set to '{company}'. No problems found for this company slug — try a different spelling (e.g. 'google', 'meta', 'amazon')."

    count_result = await db.execute(
        select(func.count(CompanyProblem.id)).where(CompanyProblem.company == company)
    )
    existing_count = count_result.scalar() or 0
    return f"COMPANY_SET: Target company set to '{company}'. {existing_count} company-tagged problems loaded with frequency data."


async def _tool_set_session(db: AsyncSession, user: User, session_cookie: str, csrftoken: str | None) -> str:
    """Store user's LeetCode premium session cookie for company data access."""
    from app.leetcode_client import fetch_user_stats

    user.lc_session_cookie = session_cookie.strip()
    if csrftoken:
        user.lc_csrftoken = csrftoken.strip()
    user.is_premium = True
    await db.commit()

    # Verify the cookie works by fetching stats
    try:
        stats = await fetch_user_stats(user.leetcode_username or "")
        if stats.get("total", 0) > 0:
            # If user has a target company, sync company problems now
            if user.target_company:
                from app.leetcode_sync import sync_company_problems
                linked = await sync_company_problems(
                    db, user.target_company,
                    session_cookie=user.lc_session_cookie,
                    csrftoken=user.lc_csrftoken,
                )
                return (
                    f"PREMIUM_SET: Session cookie saved. Premium access verified! "
                    f"Loaded {linked} problems for {user.target_company} with interview frequency data. "
                    f"Your recommendations will now include how often each problem is asked in interviews."
                )
            return (
                f"PREMIUM_SET: Session cookie saved. Premium access verified! "
                f"Set a target company (e.g. 'I'm prepping for Google') to get "
                f"company-specific interview frequency data."
            )
    except Exception:
        pass

    return (
        f"PREMIUM_SET: Session cookie saved. "
        f"Set a target company to load company-specific problems with frequency data."
    )


async def _tool_get_stats(db: AsyncSession, user: User) -> str:
    # Sync real LeetCode counts first
    if user.leetcode_username:
        from app.leetcode_client import fetch_user_stats
        try:
            lc_stats = await fetch_user_stats(user.leetcode_username)
            user.lc_total_solved = lc_stats["total"]
            user.lc_easy_solved = lc_stats["easy"]
            user.lc_medium_solved = lc_stats["medium"]
            user.lc_hard_solved = lc_stats["hard"]
            await db.commit()
        except Exception:
            pass

    # Pending count
    pending = await _get_pending_submissions(db, user)

    topic_idx = user.last_topic_index
    current_topic = TOPIC_ORDER[(topic_idx + 1) % len(TOPIC_ORDER)] if topic_idx >= 0 else TOPIC_ORDER[0]

    stats = {
        "total_solved": user.lc_total_solved,
        "easy_solved": user.lc_easy_solved,
        "medium_solved": user.lc_medium_solved,
        "hard_solved": user.lc_hard_solved,
        "pending_count": len(pending),
        "leetcode_username": user.leetcode_username,
        "target_company": user.target_company,
        "next_topic": current_topic,
        "current_streak": user.streak,
        "longest_streak": user.longest_streak,
    }
    return f"USER_STATS: {json.dumps(stats)}"


async def _tool_sync_profile(db: AsyncSession, user: User) -> str:
    from app.leetcode_sync import sync_user_solved

    if not user.leetcode_username:
        return "NO_USERNAME: User hasn't set their LeetCode username yet. Ask them to set it first."

    # Get pending problems BEFORE sync to detect newly solved ones
    pending_before = await _get_pending_submissions(db, user)
    pending_ids_before = {sub.problem_id for sub, _ in pending_before}

    synced = await sync_user_solved(db, user)

    # Check which pending problems got solved by the sync
    newly_solved = []
    if synced > 0 and pending_ids_before:
        for pid in pending_ids_before:
            sub_result = await db.execute(
                select(Submission, Problem)
                .join(Problem, Submission.problem_id == Problem.id)
                .where(
                    Submission.user_id == user.id,
                    Submission.problem_id == pid,
                    Submission.status == SubmissionStatus.SOLVED,
                )
            )
            match = sub_result.fetchone()
            if match:
                newly_solved.append(match[1].title)

    # Update streak if new solves detected
    if newly_solved:
        streak_msg = _update_streak(user)
        await db.commit()
    else:
        streak_msg = ""

    await db.refresh(user)
    pending_after = await _get_pending_submissions(db, user)

    result_msg = (
        f"SYNCED: Refreshed profile for '{user.leetcode_username}'. "
        f"LeetCode total: {user.lc_total_solved} solved "
        f"({user.lc_easy_solved}E / {user.lc_medium_solved}M / {user.lc_hard_solved}H)."
    )

    if newly_solved:
        result_msg += f"\nNewly solved from your list: {', '.join(newly_solved)}.{streak_msg}"

    if pending_after:
        result_msg += f"\nStill pending: {len(pending_after)} problem(s)."

    return result_msg


async def _tool_interview_plan(db: AsyncSession, user: User) -> str:
    from app.recommender import get_topic_coverage

    if user.leetcode_username:
        from app.leetcode_sync import sync_user_solved
        await sync_user_solved(db, user)

    coverage = await get_topic_coverage(db, user)

    lines = [
        f"INTERVIEW_PLAN: Coverage {coverage['coverage_pct']}% | "
        f"Total solved: {coverage['total_solved']}"
    ]

    gaps = []
    for topic, data in coverage["topics"].items():
        if data["complete"]:
            continue
        missing = []
        for diff in ("easy", "medium", "hard"):
            need = data[diff]["target"] - data[diff]["solved"]
            if need > 0:
                missing.append(f"{need} {diff}")
        if missing:
            gaps.append(f"  {topic}: need {', '.join(missing)}")

    if gaps:
        lines.append("GAPS (topics needing more problems):")
        lines.extend(gaps[:10])
        if len(gaps) > 10:
            lines.append(f"  ...and {len(gaps) - 10} more topics")
    else:
        lines.append("ALL TOPICS COVERED! You're interview-ready on core topics.")

    if coverage["weakest_topics"]:
        lines.append(f"PRIORITY: {', '.join(coverage['weakest_topics'][:3])}")
    if coverage["strongest_topics"]:
        lines.append(f"STRONG: {', '.join(coverage['strongest_topics'][:3])}")

    return "\n".join(lines)


async def _tool_interview_problem(db: AsyncSession, user: User) -> str:
    from app.recommender import get_interview_problem, get_topic_coverage

    if user.leetcode_username:
        from app.leetcode_sync import sync_user_solved
        await sync_user_solved(db, user)

    problem = await get_interview_problem(db, user)
    if not problem:
        return "NO_PROBLEMS: No unsolved problems available for interview prep."

    submission = Submission(
        user_id=user.id,
        problem_id=problem.id,
        status=SubmissionStatus.SENT,
    )
    db.add(submission)
    await db.commit()

    coverage = await get_topic_coverage(db, user)
    gap_info = ""
    if problem.tags:
        tags = [t.strip() for t in problem.tags.split(",")]
        for tag in tags:
            if tag in coverage["topics"] and not coverage["topics"][tag]["complete"]:
                gap_info = f" (fills gap in {tag})"
                break

    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(problem.difficulty.value, "⚪")

    return (
        f"INTERVIEW_PROBLEM: {difficulty_emoji} *{problem.title}*{gap_info}\n"
        f"Difficulty: {problem.difficulty.value.capitalize()}\n"
        f"Topics: {problem.tags or 'General'}\n"
        f"Link: https://leetcode.com/problems/{problem.slug}/\n"
        f"Interview coverage: {coverage['coverage_pct']}%"
    )


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

async def _load_chat_history(db: AsyncSession, user: User, limit: int = 20) -> list[dict]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


async def _save_chat_message(db: AsyncSession, user: User, role: str, content: str):
    msg = ChatMessage(user_id=user.id, role=role, content=content)
    db.add(msg)
    await db.commit()


async def run_agent(user_message: str, db: AsyncSession, user: User) -> str:
    """Run the AI agent with tools. Returns the final text response to send to the user."""
    client = get_anthropic_client()

    # Build dynamic system context with ALL pending problems
    system = SYSTEM_PROMPT
    pending = await _get_pending_submissions(db, user)
    if pending:
        problems_ctx = "\n".join(
            f"  {i}. '{prob.title}' ({prob.difficulty.value}) - {prob.tags or 'N/A'} - https://leetcode.com/problems/{prob.slug}/"
            for i, (sub, prob) in enumerate(pending, 1)
        )
        system += (
            f"\n\n[CONTEXT] The user has {len(pending)} pending problems:\n{problems_ctx}\n"
            f"When the user refers to a problem by number, use this ordering."
        )
    else:
        system += "\n\n[CONTEXT] The user has no pending problems right now."

    if user.target_company:
        system += f"\n[CONTEXT] Target company: {user.target_company}"
    if user.leetcode_username:
        system += f"\n[CONTEXT] LeetCode username: {user.leetcode_username}"

    history = await _load_chat_history(db, user)
    messages = history + [{"role": "user", "content": user_message}]

    await _save_chat_message(db, user, "user", user_message)

    # Agentic loop
    while True:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1200,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            final_text = "\n".join(text_parts) if text_parts else "I'm here to help! Ask me anything about LeetCode."
            await _save_chat_message(db, user, "assistant", final_text)
            return final_text

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_call in tool_calls:
            result = await execute_tool(tool_call.name, tool_call.input, db, user)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn" and text_parts:
            final_text = "\n".join(text_parts)
            await _save_chat_message(db, user, "assistant", final_text)
            return final_text


# ---------------------------------------------------------------------------
# Legacy / utility functions (used by scheduler)
# ---------------------------------------------------------------------------

async def generate_hint(problem_title: str, difficulty: str, tags: str | None) -> str:
    client = get_anthropic_client()
    tag_context = f"Topics: {tags}" if tags else ""
    message = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Give a concise, helpful hint for the LeetCode problem '{problem_title}' "
                    f"(difficulty: {difficulty}). {tag_context}\n"
                    "Do NOT give the full solution. Give 2-3 sentences pointing toward the right "
                    "approach/data structure. Keep it beginner-friendly."
                ),
            }
        ],
    )
    return message.content[0].text


async def run_agent_proactive(prompt: str, db: AsyncSession, user: User) -> str:
    """Run the agent for proactive outreach (follow-ups, reports, streak reminders)."""
    client = get_anthropic_client()

    system = (
        "You are a friendly LeetCode coaching assistant on WhatsApp. "
        "You are proactively reaching out to the user (they did NOT message you). "
        "Keep messages SHORT (1-3 sentences). Be encouraging, not annoying. "
        "Use WhatsApp formatting: *bold*, _italic_. "
        "Never say you're an AI or bot."
    )

    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text
