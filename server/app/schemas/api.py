from datetime import datetime
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    display_name: str
    role: Literal["student", "teacher", "admin"]
    status: str
    last_login_at: datetime | None = None
    phone: str = ""
    requested_course_id: str | None = None
    requested_class_name: str = ""
    requested_at: datetime | None = None
    student_number: str = ""


class CourseOut(BaseModel):
    id: str
    name: str
    term: str
    teacher_id: str
    class_code: str = ""


class CourseCreate(BaseModel):
    id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    term: str = Field(min_length=1, max_length=32)
    teacher_id: str


class CourseUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    term: str = Field(min_length=1, max_length=32)
    teacher_id: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    phone: str = Field(pattern=r"^1[3-9]\d{9}$")
    class_code: str = Field(min_length=4, max_length=16)
    display_name: str = Field(default="", max_length=64)
    student_number: str = Field(default="", max_length=32)


class RegisterResponse(BaseModel):
    status: Literal["pending"]
    message: str
    course_name: str
    course_id: str


class PendingRegistrationOut(BaseModel):
    id: str
    username: str
    display_name: str
    phone: str
    status: str
    requested_course_id: str | None = None
    requested_course_name: str = ""
    requested_class_name: str = ""
    requested_at: datetime | None = None
    student_number: str = ""


class ApproveRegistrationRequest(BaseModel):
    class_name: str = Field(default="", max_length=100)


class RejectRegistrationRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


class EnrollmentCreate(BaseModel):
    class_name: str = Field(default="", max_length=100)


class RollCallCreate(BaseModel):
    student_id: str
    question: str = Field(min_length=1, max_length=4000)
    correct: bool


class MeResponse(UserOut):
    courses: list[CourseOut]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    role: Literal["student", "teacher", "admin"] | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class LoginResponse(BaseModel):
    user: UserOut


class ChatAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    course_id: str | None = None


class ChatSource(BaseModel):
    name: str
    score: float | None = None


class ChatResponse(BaseModel):
    answer_markdown: str
    sections: dict[str, str]
    mind_map: list[str]
    sources: list[ChatSource]
    degraded: bool = False
    degraded_reason: Literal[
        "no_knowledge", "model_unconfigured", "model_unavailable", "model_http_error",
        "model_rate_limited", "model_busy", "model_cooling_down", "model_timeout", "model_transport_error", "model_invalid_response",
        "model_empty_response",
    ] | None = None


class QuizCounts(BaseModel):
    single: int = Field(default=1, ge=0, le=20)
    multi: int = Field(default=1, ge=0, le=20)
    judge: int = Field(default=0, ge=0, le=20)
    fill: int = Field(default=0, ge=0, le=20)
    short: int = Field(default=1, ge=0, le=20)
    case: int = Field(default=0, ge=0, le=20)


class QuizStartRequest(BaseModel):
    course_id: str | None = None
    chapter: str | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    counts: QuizCounts = Field(default_factory=QuizCounts)


class QuizQuestionOut(BaseModel):
    id: str
    type: str
    stem: str
    options: list[str]
    score: int
    knowledge_points: list[str]
    chapter: str | None = None


class QuizSessionOut(BaseModel):
    id: str
    status: str
    questions: list[QuizQuestionOut]
    course_id: str | None = None
    bank_version: str = "global-v1"
    title: str = "练习测验"
    assignment_id: str | None = None
    saved_answers: dict[str, Any] = Field(default_factory=dict)
    draft_answers: dict[str, Any] = Field(default_factory=dict)
    draft_updated_at: datetime | None = None


class QuizDraftRequest(BaseModel):
    answers: dict[str, Any]


class PracticeGenerateRequest(BaseModel):
    course_id: str | None = None
    count: int = Field(default=3, ge=1, le=8)
    source_question: str | None = Field(default=None, max_length=4000)


class PracticeQuestionOut(BaseModel):
    id: str
    type: str
    stem: str
    options: list[str] = Field(default_factory=list)
    score: int
    knowledge_points: list[str] = Field(default_factory=list)


class PracticeListItemOut(BaseModel):
    id: str
    created_at: datetime
    course_id: str | None = None
    status: str
    question_count: int
    total: float | None = None
    max_total: float = 0
    source_questions: list[str] = Field(default_factory=list)


class PracticeDetailOut(BaseModel):
    id: str
    status: str
    course_id: str | None = None
    created_at: datetime
    source_questions: list[str] = Field(default_factory=list)
    questions: list[PracticeQuestionOut]
    answers: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None


class PracticeSubmitRequest(BaseModel):
    answers: dict[str, Any]


class QuizSubmitRequest(BaseModel):
    answers: dict[str, Any]


class GradeItem(BaseModel):
    question_id: str
    score: float
    max_score: float
    method: str
    why: str
    knowledge_points: list[str] = Field(default_factory=list)
    per_point: list[dict[str, Any]] = Field(default_factory=list)


class GradingResultOut(BaseModel):
    id: str
    session_id: str
    status: str
    total: float
    max_total: float
    graded_by: str
    per_question: list[GradeItem]


class StudentGradeItem(BaseModel):
    question_id: str
    score: float
    max_score: float
    method: str
    why: str


class StudentGradingResultOut(BaseModel):
    id: str
    session_id: str
    status: str
    total: float
    max_total: float
    graded_by: str
    per_question: list[StudentGradeItem]


class QuizSubmitResponse(BaseModel):
    status: str
    session_id: str
    result: GradingResultOut


class StudentQuizSubmitResponse(BaseModel):
    status: str
    session_id: str
    result: StudentGradingResultOut


class ReviewGradeRequest(BaseModel):
    question_id: str
    score: float = Field(ge=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=1000)


class QuestionCreate(BaseModel):
    type: Literal["single_choice", "multi_choice", "judge", "fill", "short_answer", "case"]
    stem: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    answer: Any = None
    reference_answer: str = ""
    rubric: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_points: list[str] = Field(default_factory=list)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    course_id: str | None = None
    chapter: str | None = Field(default=None, max_length=200)
    quick_response: bool = False
    source: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_content(self) -> "QuestionCreate":
        if not self.stem.strip():
            raise ValueError("题干不能为空")
        if self.type in {"single_choice", "multi_choice"}:
            if len(self.options) < 2 or any(not option.strip() for option in self.options):
                raise ValueError("选择题至少需要两个非空选项")
        if self.type == "single_choice":
            if type(self.answer) is not int or not 0 <= self.answer < len(self.options):
                raise ValueError("单选题答案必须是有效选项下标")
        if self.type == "multi_choice":
            if (
                not isinstance(self.answer, list)
                or not self.answer
                or any(type(index) is not int or not 0 <= index < len(self.options) for index in self.answer)
                 or len(set(self.answer)) != len(self.answer)
            ):
                raise ValueError("多选题答案必须是不重复的有效选项下标")
        if self.type == "judge" and type(self.answer) is not bool:
            raise ValueError("判断题答案必须是布尔值")
        if self.type == "fill" and not (
            isinstance(self.answer, str) and self.answer.strip()
            or isinstance(self.answer, list) and self.answer and all(isinstance(item, str) and item.strip() for item in self.answer)
        ):
            raise ValueError("填空题答案必须是非空文本或文本数组")
        if self.type in {"fill", "short_answer", "case"} and not self.reference_answer.strip():
            raise ValueError("填空题、简答题和案例题必须提供参考答案")
        if self.type in {"short_answer", "case"} and self.rubric:
            total = 0.0
            maximum = 25 if self.type == "short_answer" else 30
            for item in self.rubric:
                point = str(item.get("point") or "").strip()
                score = item.get("score")
                if not point or isinstance(score, bool):
                    raise ValueError("Rubric 每个评分点必须包含名称和有效分值")
                try:
                    value = float(score)
                except (TypeError, ValueError):
                    raise ValueError("Rubric 分值必须是数字") from None
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Rubric 分值必须是有限的非负数")
                total += value
            if total > maximum:
                raise ValueError(f"Rubric 总分不能超过题目满分 {maximum} 分")
        return self


class QuizAssignRequest(BaseModel):
    course_id: str
    title: str = Field(default="随堂测验", min_length=1, max_length=100)
    question_ids: list[str] = Field(min_length=1)
    student_ids: list[str] | None = None
    class_name: str | None = Field(default=None, max_length=100)
    due_at: datetime | None = None


class AiQuestionGenerateRequest(BaseModel):
    course_id: str
    knowledge_points: list[str] = Field(min_length=1, max_length=10)
    question_type: Literal["single_choice", "multi_choice", "judge", "fill", "short_answer", "case"] = "single_choice"
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    count: int = Field(default=3, ge=1, le=10)


class AiQuestionGenerateResponse(BaseModel):
    status: Literal["draft"]
    source_count: int
    questions: list[dict[str, Any]]


class QuestionOut(BaseModel):
    id: str
    code: str
    type: str
    stem: str
    options: list[str]
    reference_answer: str
    knowledge_points: list[str]
    difficulty: str
    status: str
    course_id: str | None
    chapter: str | None = None
    rubric: list[dict[str, Any]] = Field(default_factory=list)
    quick_response: bool = False
    source: str = ""
    review_comment: str = ""
    version: int = 1


class QuestionDetailOut(QuestionOut):
    answer: Any = None



class ReviewRequest(BaseModel):
    decision: Literal["pass", "reject"]
    comment: str = Field(default="", max_length=1000)


class DocumentOut(BaseModel):
    id: str
    name: str
    target_kb: str
    size: int
    vector_status: str
    uploaded_at: str | None = None
    source_path: str = ""
    shared: bool = False
    summary: str = ""
    topics: list[str] = Field(default_factory=list)


class MdUpdate(BaseModel):
    content: str = Field(max_length=2_000_000)


class GraphNodeCreate(BaseModel):
    graph: Literal["knowledge", "comp", "problem"]
    branch: Literal["A", "B", "C"]
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    map_from: str | None = None


class GraphNodeOut(GraphNodeCreate):
    id: str


class GraphMappingRequest(BaseModel):
    source_id: str
    target_id: str


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["student", "teacher", "admin"] = "student"
    student_number: str = Field(default="", max_length=32)


class AdminUserStatusUpdate(BaseModel):
    status: Literal["active", "disabled"]


class AdminPasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)


class ExcelScheduleUpdate(BaseModel):
    enabled: bool
    interval_hours: int = Field(ge=1, le=720)


class ExcelSnapshotRequest(BaseModel):
    course_id: str | None = None


class HistoryCleanupRequest(BaseModel):
    before: datetime | None = None
    all_records: bool = False
    course_id: str | None = None
    categories: list[Literal["quiz", "chat", "classroom", "usage"]] = Field(min_length=1)
    confirmed: bool = False

    @model_validator(mode="after")
    def require_timezone(self) -> "HistoryCleanupRequest":
        if not self.all_records and self.before is None:
            raise ValueError("请选择截止日期或全部删除")
        if self.before is not None and self.before.tzinfo is None:
            raise ValueError("清理截止时间必须包含时区")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("清理类型不能重复")
        return self
