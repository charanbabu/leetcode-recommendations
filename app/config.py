from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    app_name: str = "LeetCode AI Agent"
    environment: str = "development"
    base_url: str = "http://localhost:8000"

    # Database
    database_url: str = "sqlite+aiosqlite:///data/leetcode.db"

    # Anthropic
    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # Defaults
    default_leetcode_username: str | None = None

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str  # e.g. whatsapp:+14155238886

    # Scheduler
    daily_send_hour: int = 8  # 8am in user's timezone (default UTC)
    daily_problem_count: int = 6  # problems per day (mix of topics/difficulties)

    # Autonomous features
    auto_seed_on_startup: bool = True
    auto_seed_max_problems: int = 3500
    followup_after_hours: int = 6  # nudge user X hours after sending a problem
    weekly_report_day: int = 0  # 0=Monday
    weekly_report_hour: int = 9  # 9am in user's timezone
    sync_interval_hours: int = 6  # sync LeetCode profiles every X hours
    enable_followups: bool = True
    enable_weekly_reports: bool = True
    enable_streak_reminders: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
