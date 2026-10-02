from typing import Any

from ..models import GradingResult, Question, User
from ..schemas.api import GradingResultOut, GradeItem, QuestionOut, StudentGradingResultOut, StudentGradeItem, UserOut


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, username=user.username, display_name=user.display_name,
        role=user.role, status=user.status, last_login_at=user.last_login_at,
        phone=user.phone, requested_course_id=user.requested_course_id,
        requested_class_name=user.requested_class_name, requested_at=user.requested_at,
        student_number=user.student_number,
    )


def question_out(question: Question | dict[str, Any]) -> QuestionOut:
    get = question.get if isinstance(question, dict) else lambda key, default=None: getattr(question, key, default)
    return QuestionOut(
        id=get("id"), code=get("code", "snapshot"), type=get("type"),
        stem=get("stem"), options=get("options", []),
        reference_answer=get("reference_answer", ""),
        knowledge_points=get("knowledge_points", []), difficulty=get("difficulty", "medium"),
        status=get("status", "published"), course_id=get("course_id"), chapter=get("chapter"),
        rubric=get("rubric", []), quick_response=get("quick_response", False),
        source=get("source", ""), review_comment=get("review_comment", ""),
        version=get("version", 1),
    )


def grading_out(result: GradingResult) -> GradingResultOut:
    return GradingResultOut(
        id=result.id, session_id=result.session_id, status=result.status,
        total=result.total, max_total=result.max_total, graded_by=result.graded_by,
        per_question=[GradeItem(**item) for item in result.per_question],
    )


def student_grading_out(result: GradingResult) -> StudentGradingResultOut:
    return StudentGradingResultOut(
        id=result.id, session_id=result.session_id, status=result.status,
        total=result.total, max_total=result.max_total, graded_by=result.graded_by,
        per_question=[
            StudentGradeItem(
                question_id=item["question_id"], score=item["score"],
                max_score=item["max_score"], method=item["method"], why=item["why"],
            )
            for item in result.per_question
        ],
    )
