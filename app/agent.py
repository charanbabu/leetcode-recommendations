import json
import logging
from collections import Counter
from datetime import datetime, timezone

import anthropic
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
        "name": "mark_solved",
        "description": "Mark the user's current active problem as solved. Use when the user says they solved it, finished it, got it, etc.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "skip_problem",
        "description": "Skip the user's current active problem. Use when they want to move on, skip, get a different one, etc.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_hint",
        "description": "Give the user a hint for their current active problem. Use when they ask for help, a hint, a nudge, are stuck, etc.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "review_code",
        "description": "Review code the user submitted for their current problem. Use when the user pastes code and wants feedback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The user's code to review",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "recommend_problem",
        "description": "Recommend and send a new problem to the user. Use when they ask for a problem, want to practice, or say 'give me something'.",
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
        "description": "Set the company the user is preparing for (e.g. google, meta, amazon, microsoft). Problems from that company will be prioritized.",
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
        "description": "Get the user's progress stats: problems solved by difficulty, weak topics, current company target, etc. Use when they ask about their progress, stats, or how they're doing.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "sync_leetcode_profile",
        "description": "Sync the user's solved problems from their LeetCode profile. Use when the user disputes their stats, claims a different solve count, or asks you to refresh/update their progress from LeetCode. Requires the user to have set their LeetCode username first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "explain_solution",
        "description": "Explain the optimal solution/approach for the user's current active problem. Use when the user asks 'how do I solve this', 'what's the solution', 'explain the answer', 'walk me through it', etc. Gives a full explanation with approach, intuition, complexity, and code.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "explain_topic",
        "description": "Explain a data structure or algorithm topic to the user. Use when they ask 'what is DP', 'explain graphs', 'how does binary search work', etc.",
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

Your personality: encouraging, concise (this is WhatsApp — keep messages short), and knowledgeable about algorithms and data structures.

You have tools to:
- Mark problems as solved or skipped
- Send hints for the current problem
- Review submitted code
- Recommend new problems (uses a smart algorithm based on topic rotation, weak areas, and company targeting)
- Set the user's LeetCode username or target company
- Show their progress stats
- Explain algorithm/DS topics

Guidelines:
- Keep responses SHORT. This is WhatsApp, not an essay. 2-4 sentences max unless explaining a topic.
- Use WhatsApp formatting: *bold*, _italic_, ```code```
- Be encouraging but honest about code quality
- When a user sends code, use review_code to analyze it
- If they seem stuck, proactively offer a hint
- If they just solved something, congratulate them and suggest what to work on next
- If you're unsure what they want, just ask — don't guess wrong
- When recommending a problem, always use the recommend_problem tool — don't make up problems"""


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

    if tool_name == "mark_solved":
        return await _tool_mark_solved(db, user)

    elif tool_name == "skip_problem":
        return await _tool_skip_problem(db, user)

    elif tool_name == "send_hint":
        return await _tool_send_hint(db, user)

    elif tool_name == "review_code":
        return await _tool_review_code(db, user, tool_input["code"])

    elif tool_name == "recommend_problem":
        return await _tool_recommend_problem(db, user)

    elif tool_name == "set_leetcode_username":
        return await _tool_set_username(db, user, tool_input["username"])

    elif tool_name == "set_target_company":
        return await _tool_set_company(db, user, tool_input["company"])

    elif tool_name == "get_user_stats":
        return await _tool_get_stats(db, user)

    elif tool_name == "explain_solution":
        return await _tool_explain_solution(db, user)

    elif tool_name == "sync_leetcode_profile":
        return await _tool_sync_profile(db, user)

    elif tool_name == "explain_topic":
        return tool_input["topic"]  # Agent will explain in its response

    return "Unknown tool"


async def _tool_mark_solved(db: AsyncSession, user: User) -> str:
    submission = await _get_active_submission(db, user)
    if not submission:
        return "NO_ACTIVE_PROBLEM: User has no active problem to mark as solved."

    problem = await db.get(Problem, submission.problem_id)
    submission.status = SubmissionStatus.SOLVED
    submission.solved_at = datetime.now(timezone.utc)
    await db.commit()
    return f"SOLVED: Marked '{problem.title}' as solved. User has now solved {await _solved_count(db, user)} problems total."


async def _tool_skip_problem(db: AsyncSession, user: User) -> str:
    submission = await _get_active_submission(db, user)
    if not submission:
        return "NO_ACTIVE_PROBLEM: User has no active problem to skip."

    problem = await db.get(Problem, submission.problem_id)
    submission.status = SubmissionStatus.SKIPPED
    await db.commit()
    return f"SKIPPED: Skipped '{problem.title}'."


async def _tool_send_hint(db: AsyncSession, user: User) -> str:
    submission = await _get_active_submission(db, user)
    if not submission:
        return "NO_ACTIVE_PROBLEM: User has no active problem to get a hint for."

    problem = await db.get(Problem, submission.problem_id)
    submission.status = SubmissionStatus.ATTEMPTED
    await db.commit()

    tag_context = f"Topics: {problem.tags}" if problem.tags else ""
    return (
        f"HINT_REQUEST for '{problem.title}' (difficulty: {problem.difficulty.value}). {tag_context}\n"
        "Generate a helpful 2-3 sentence hint pointing toward the right approach/data structure. "
        "Do NOT give the full solution."
    )


async def _tool_review_code(db: AsyncSession, user: User, code: str) -> str:
    submission = await _get_active_submission(db, user)
    if not submission:
        return "NO_ACTIVE_PROBLEM: User has no active problem to review code for."

    problem = await db.get(Problem, submission.problem_id)
    submission.user_code = code
    submission.status = SubmissionStatus.ATTEMPTED
    await db.commit()

    return (
        f"REVIEW_CODE for '{problem.title}' ({problem.difficulty.value}):\n"
        f"```\n{code}\n```\n"
        "Give brief feedback: correctness, time/space complexity, and one improvement. Keep it under 150 words."
    )


async def _tool_recommend_problem(db: AsyncSession, user: User) -> str:
    problem = await get_recommended_problem(db, user)
    if not problem:
        return "NO_PROBLEMS: No problems available. The database may need to be seeded."

    # Create submission record
    submission = Submission(
        user_id=user.id,
        problem_id=problem.id,
        status=SubmissionStatus.SENT,
    )
    db.add(submission)
    await db.commit()

    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(problem.difficulty.value, "⚪")
    tags = problem.tags or "General"
    return (
        f"RECOMMENDED: {difficulty_emoji} *{problem.title}*\n"
        f"Difficulty: {problem.difficulty.value.capitalize()}\n"
        f"Topics: {tags}\n"
        f"Link: https://leetcode.com/problems/{problem.slug}/\n"
        f"Acceptance rate: {problem.ac_rate:.1f}%" if problem.ac_rate else
        f"RECOMMENDED: {difficulty_emoji} *{problem.title}*\n"
        f"Difficulty: {problem.difficulty.value.capitalize()}\n"
        f"Topics: {tags}\n"
        f"Link: https://leetcode.com/problems/{problem.slug}/"
    )


async def _tool_set_username(db: AsyncSession, user: User, raw: str) -> str:
    from app.leetcode_sync import sync_user_solved

    username = raw.rstrip("/").split("/")[-1]
    user.leetcode_username = username
    await db.commit()

    synced = await sync_user_solved(db, user)
    return f"USERNAME_SET: LeetCode username set to '{username}'. Synced {synced} solved problems from their profile."


async def _tool_set_company(db: AsyncSession, user: User, company: str) -> str:
    from app.leetcode_sync import sync_company_problems

    company = company.lower().strip()
    user.target_company = company
    await db.commit()

    # Seed company problems if needed
    existing = await db.execute(
        select(CompanyProblem).where(CompanyProblem.company == company).limit(1)
    )
    if not existing.scalar_one_or_none():
        linked = await sync_company_problems(db, company)
        return f"COMPANY_SET: Target company set to '{company}'. Loaded {linked} company-tagged problems."

    return f"COMPANY_SET: Target company set to '{company}'. Company problems already loaded."


async def _tool_get_stats(db: AsyncSession, user: User) -> str:
    # Solved counts by difficulty
    solved_counts = {}
    for diff in Difficulty:
        result = await db.execute(
            select(func.count(Submission.id))
            .join(Problem, Submission.problem_id == Problem.id)
            .where(
                Submission.user_id == user.id,
                Submission.status == SubmissionStatus.SOLVED,
                Problem.difficulty == diff,
            )
        )
        solved_counts[diff.value] = result.scalar() or 0

    total = sum(solved_counts.values())

    # Weak topics
    weak_result = await db.execute(
        select(Problem.tags, func.count(Submission.id))
        .join(Problem, Submission.problem_id == Problem.id)
        .where(
            Submission.user_id == user.id,
            Submission.status.in_([SubmissionStatus.SENT, SubmissionStatus.ATTEMPTED, SubmissionStatus.SKIPPED]),
            Problem.tags.isnot(None),
        )
        .group_by(Problem.tags)
        .order_by(func.count(Submission.id).desc())
        .limit(5)
    )
    weak_tags = [row[0] for row in weak_result.fetchall()]

    # Current topic in rotation
    topic_idx = user.last_topic_index
    current_topic = TOPIC_ORDER[(topic_idx + 1) % len(TOPIC_ORDER)] if topic_idx >= 0 else TOPIC_ORDER[0]

    stats = {
        "total_solved": total,
        "easy_solved": solved_counts.get("easy", 0),
        "medium_solved": solved_counts.get("medium", 0),
        "hard_solved": solved_counts.get("hard", 0),
        "leetcode_username": user.leetcode_username,
        "target_company": user.target_company,
        "next_topic": current_topic,
        "weak_areas": weak_tags[:3] if weak_tags else ["None yet"],
    }
    return f"USER_STATS: {json.dumps(stats)}"


async def _tool_explain_solution(db: AsyncSession, user: User) -> str:
    submission = await _get_active_submission(db, user)
    if not submission:
        return "NO_ACTIVE_PROBLEM: User has no active problem to explain."

    problem = await db.get(Problem, submission.problem_id)

    # Fetch full description if we don't have it
    if not problem.description:
        from app.leetcode_client import fetch_problem_detail
        try:
            detail = await fetch_problem_detail(problem.slug)
            if detail and detail.get("content"):
                problem.description = detail["content"]
                await db.commit()
        except Exception:
            pass

    tag_context = f"Topics: {problem.tags}" if problem.tags else ""
    description_context = f"Problem description:\n{problem.description}" if problem.description else ""

    return (
        f"EXPLAIN_SOLUTION for '{problem.title}' ({problem.difficulty.value}). {tag_context}\n"
        f"{description_context}\n\n"
        "Explain the optimal solution clearly:\n"
        "1. Key intuition — why does this approach work?\n"
        "2. Step-by-step approach\n"
        "3. Python code solution\n"
        "4. Time and space complexity\n"
        "Keep it concise but thorough — this is WhatsApp, so use formatting wisely."
    )


async def _tool_sync_profile(db: AsyncSession, user: User) -> str:
    from app.leetcode_sync import sync_user_solved

    if not user.leetcode_username:
        return "NO_USERNAME: User hasn't set their LeetCode username yet. Ask them to set it first."

    synced = await sync_user_solved(db, user)

    # Return updated stats so the agent can report accurate numbers
    total = await _solved_count(db, user)
    return (
        f"SYNCED: Refreshed profile for '{user.leetcode_username}'. "
        f"{synced} new solved problems synced. Total solved in our system: {total}. "
        f"Note: LeetCode's API only returns the last ~500 recent submissions, "
        f"so the count may differ from their full profile."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_active_submission(db: AsyncSession, user: User) -> Submission | None:
    result = await db.execute(
        select(Submission)
        .where(
            Submission.user_id == user.id,
            Submission.status.in_([SubmissionStatus.SENT, SubmissionStatus.ATTEMPTED]),
        )
        .order_by(Submission.sent_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _solved_count(db: AsyncSession, user: User) -> int:
    result = await db.execute(
        select(func.count(Submission.id)).where(
            Submission.user_id == user.id,
            Submission.status == SubmissionStatus.SOLVED,
        )
    )
    return result.scalar() or 0


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

async def _load_chat_history(db: AsyncSession, user: User, limit: int = 20) -> list[dict]:
    """Load recent conversation history for context."""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    # Reverse so oldest first
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


async def _save_chat_message(db: AsyncSession, user: User, role: str, content: str):
    msg = ChatMessage(user_id=user.id, role=role, content=content)
    db.add(msg)
    await db.commit()


async def run_agent(user_message: str, db: AsyncSession, user: User) -> str:
    """Run the AI agent with tools. Returns the final text response to send to the user."""
    client = get_anthropic_client()

    # Build dynamic system context with active problem info
    system = SYSTEM_PROMPT
    active_sub = await _get_active_submission(db, user)
    if active_sub:
        problem = await db.get(Problem, active_sub.problem_id)
        if problem:
            system += (
                f"\n\n[CONTEXT] The user's current active problem is: "
                f"'{problem.title}' ({problem.difficulty.value}), "
                f"tags: {problem.tags or 'N/A'}, "
                f"link: https://leetcode.com/problems/{problem.slug}/"
            )

    # Load conversation history for context
    history = await _load_chat_history(db, user)
    messages = history + [{"role": "user", "content": user_message}]

    # Save the user message
    await _save_chat_message(db, user, "user", user_message)

    # Agentic loop — keep going until the model stops calling tools
    while True:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=800,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Collect text and tool use blocks
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            # No more tool calls — return the final text
            final_text = "\n".join(text_parts) if text_parts else "I'm here to help! Ask me anything about LeetCode."
            await _save_chat_message(db, user, "assistant", final_text)
            return final_text

        # Execute tools and build tool results
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

        # If stop_reason is end_turn and we have text, we're done
        if response.stop_reason == "end_turn" and text_parts:
            final_text = "\n".join(text_parts)
            await _save_chat_message(db, user, "assistant", final_text)
            return final_text


# ---------------------------------------------------------------------------
# Legacy functions (used by scheduler for daily sends)
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
