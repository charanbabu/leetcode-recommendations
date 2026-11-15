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

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
