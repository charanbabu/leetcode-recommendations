from pydantic import BaseModel
from datetime import datetime
from app.models import Difficulty, SubmissionStatus


class UserCreate(BaseModel):
    whatsapp_number: str
    leetcode_username: str | None = None
    name: str | None = None
    target_company: str | None = None
    timezone: str = "UTC"
    daily_send_hour: int = 8


class UserOut(BaseModel):
    id: int
    whatsapp_number: str
    leetcode_username: str | None
    name: str | None
    target_company: str | None
    timezone: str
    is_active: bool
    daily_send_hour: int
    created_at: datetime

    class Config:
        from_attributes = True


class ProblemOut(BaseModel):
    id: int
    leetcode_id: int
    slug: str
    title: str
    difficulty: Difficulty
    tags: str | None

    class Config:
        from_attributes = True


class SubmissionOut(BaseModel):
    id: int
    user_id: int
    problem_id: int
    status: SubmissionStatus
    sent_at: datetime
    solved_at: datetime | None

    class Config:
        from_attributes = True


class WhatsAppWebhook(BaseModel):
    From: str
    Body: str
    NumMedia: str = "0"
