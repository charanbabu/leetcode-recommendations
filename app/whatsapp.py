import asyncio
from functools import partial

from twilio.rest import Client

from app.config import settings

_client: Client | None = None


def get_twilio_client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def _send_sync(to: str, body: str) -> str:
    client = get_twilio_client()
    message = client.messages.create(
        from_=settings.twilio_whatsapp_number,
        to=to if to.startswith("whatsapp:") else f"whatsapp:{to}",
        body=body,
    )
    return message.sid


async def send_whatsapp_message(to: str, body: str) -> str:
    """Send a WhatsApp message without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_send_sync, to, body))


def format_daily_problem(problem_title: str, difficulty: str, slug: str, hint: str) -> str:
    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(difficulty.lower(), "⚪")
    return (
        f"*Daily LeetCode Problem* {difficulty_emoji}\n\n"
        f"*{problem_title}*\n"
        f"Difficulty: {difficulty.capitalize()}\n"
        f"🔗 https://leetcode.com/problems/{slug}/\n\n"
        f"💡 *Hint:*\n{hint}\n\n"
        f"Just reply naturally — tell me when you've solved it, "
        f"ask for hints, or paste your code for a review!"
    )


def format_daily_plan_message(plan: list[dict], streak_text: str = "") -> str:
    """Format a multi-problem daily plan for WhatsApp."""
    difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}

    # Count by difficulty
    diff_counts: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    for item in plan:
        diff_counts[item["slot"]] += 1

    mix_parts = []
    for d in ("easy", "medium", "hard"):
        if diff_counts[d] > 0:
            mix_parts.append(f"{diff_counts[d]} {d.capitalize()}")
    mix_str = " | ".join(mix_parts)

    lines = [
        f"*Today's LeetCode Plan* ({len(plan)} problems)",
        f"Mix: {mix_str}",
    ]

    if streak_text:
        lines.append(streak_text)

    lines.append("")  # blank line

    for i, item in enumerate(plan, 1):
        p = item["problem"]
        emoji = difficulty_emoji.get(item["slot"], "⚪")
        lines.append(
            f"{i}. {emoji} *{p.title}* ({item['slot'].capitalize()})\n"
            f"   {item['reason']}\n"
            f"   https://leetcode.com/problems/{p.slug}/"
        )

    lines.append("")
    lines.append(
        "Reply with the problem number when done (e.g. 'solved 1'), "
        "ask for a hint, or paste code for review!"
    )

    return "\n".join(lines)
