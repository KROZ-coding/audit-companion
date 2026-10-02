from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class User:
    id: str
    username: str
    display_name: str
    password_hash: str
    role: str
    status: str = "active"
    last_login_at: datetime | None = None
    phone: str = ""
    requested_course_id: str | None = None
    requested_class_name: str = ""
    requested_at: datetime | None = None
    student_number: str = ""


@dataclass(slots=True)
class Course:
    id: str
    name: str
    term: str
    teacher_id: str
    class_code: str = ""


@dataclass(slots=True)
class Question:
    id: str
    code: str
    type: str
    stem: str
    options: list[str]
    answer: Any
    reference_answer: str
    rubric: list[dict[str, Any]]
    knowledge_points: list[str]
    difficulty: str = "medium"
    status: str = "published"
    course_id: str | None = None
    chapter: str | None = None
    created_by: str | None = None
    reviewed_by: str | None = None
    version: int = 1
    quick_response: bool = False
    source: str = ""
    review_comment: str = ""


@dataclass(slots=True)
class QuizSession:
    id: str
    user_id: str
    course_id: str | None
    questions: list[dict[str, Any]]
    status: str = "ongoing"
    started_at: datetime = field(default_factory=utcnow)
    submitted_at: datetime | None = None
    bank_version: str = "global-v1"
    title: str = "练习测验"
    assigned: bool = False
    assignment_id: str | None = None
    submitted_answers: dict[str, Any] = field(default_factory=dict)
    draft_answers: dict[str, Any] = field(default_factory=dict)
    draft_updated_at: datetime | None = None


@dataclass(slots=True)
class GradingResult:
    id: str
    session_id: str
    per_question: list[dict[str, Any]]
    total: float
    max_total: float
    status: str
    submitted_answers: dict[str, Any] = field(default_factory=dict)
    graded_by: str = "rule"
    graded_at: datetime = field(default_factory=utcnow)
