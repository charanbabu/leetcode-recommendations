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
