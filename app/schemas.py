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


class TopicDifficultyStats(BaseModel):
    solved: int
    target: int


class TopicCoverage(BaseModel):
    easy: TopicDifficultyStats
    medium: TopicDifficultyStats
    hard: TopicDifficultyStats
    complete: bool


class SolvedProblemOut(BaseModel):
    id: int
    leetcode_id: int
    slug: str
    title: str
    difficulty: Difficulty
    tags: str | None
    solved_at: datetime | None

    class Config:
        from_attributes = True


class LeetCodeStats(BaseModel):
    total_solved: int
    easy_solved: int
    medium_solved: int
    hard_solved: int


class UserProfileOut(BaseModel):
    user: UserOut
    leetcode_stats: LeetCodeStats
    tracked_solved: int
    coverage_pct: float
    weakest_topics: list[str]
    strongest_topics: list[str]
    topic_coverage: dict[str, TopicCoverage]
    recent_solves: list[SolvedProblemOut]
    streak: int
    longest_streak: int


class NextProblemOut(BaseModel):
    problem: ProblemOut
    reason: str
    gap_topic: str | None = None
    gap_difficulty: str | None = None
    coverage_pct: float


class DailyPlanProblem(BaseModel):
    problem: ProblemOut
    reason: str
    slot: str  # "easy", "medium", "hard"


class DailyPlanOut(BaseModel):
    user_id: int
    date: str
    problem_count: int
    problems: list[DailyPlanProblem]
    coverage_pct: float
    difficulty_mix: dict[str, int]  # e.g. {"easy": 2, "medium": 3, "hard": 1}


class WhatsAppWebhook(BaseModel):
    From: str
    Body: str
    NumMedia: str = "0"
