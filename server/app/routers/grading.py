from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_store, require_roles
from ..models import User
from ..schemas.api import GradingResultOut, ReviewGradeRequest
from ..store import Store
from .helpers import grading_out, student_grading_out

router = APIRouter(prefix="/api/grading", tags=["grading"])


@router.get("/{session_id}/status", response_model=None)
def status(session_id: str, user: User = Depends(require_roles("student", "teacher", "admin")), store: Store = Depends(get_store)) -> GradingResultOut:
    result = next((item for item in store.results.values() if item.session_id == session_id), None)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "grading_not_found", "message": "批改结果尚未生成"})
    session = store.quiz_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "quiz_not_found", "message": "测验不存在"})
    if user.role == "student" and session.user_id != user.id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该批改结果"})
    if user.role != "student":
        _check_teacher_access(session.course_id, user, store)
    return student_grading_out(result) if user.role == "student" else grading_out(result)


@router.get("/queue", response_model=list[GradingResultOut])
def queue(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> list[GradingResultOut]:
    return [
        grading_out(item) for item in store.results.values()
        if item.status == "needs_review"
        and _session_visible_to_teacher(store.quiz_sessions.get(item.session_id), user, store)
    ]


@router.get("/queue/details")
def queue_details(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> list[dict]:
    rows = []
    for result in store.results.values():
        if result.status != "needs_review":
            continue
        session = store.quiz_sessions.get(result.session_id)
        if not _session_visible_to_teacher(session, user, store):
            continue
        student = store.users.get(session.user_id)
        if student is None:
            continue
        questions = {item["id"]: item for item in session.questions}
        for item in result.per_question:
            if item["method"] != "manual_pending":
                continue
            question = questions.get(item["question_id"], {})
            rows.append({
                "result_id": result.id,
                "session_id": session.id,
                "course_id": session.course_id,
                "student": {
                    "id": student.id, "display_name": student.display_name,
                    "student_number": student.student_number,
                },
                "question": {
                    "id": item["question_id"],
                    "stem": question.get("stem", ""),
                    "reference_answer": question.get("reference_answer", ""),
                    "rubric": question.get("rubric", []),
                },
                "answer": result.submitted_answers.get(item["question_id"]),
                "score": item["score"], "max_score": item["max_score"],
                "why": item["why"], "per_point": item.get("per_point", []),
            })
    return rows


@router.get("/{result_id}/answers")
def submitted_answers(result_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> dict:
    result = store.results.get(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "grading_not_found", "message": "批改结果不存在"})
    _check_teacher_access(store.quiz_sessions.get(result.session_id).course_id if store.quiz_sessions.get(result.session_id) else None, user, store)
    return {"result_id": result_id, "answers": result.submitted_answers}


@router.post("/{result_id}/review", response_model=GradingResultOut)
def review(result_id: str, payload: ReviewGradeRequest, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> GradingResultOut:
    result = store.results.get(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "grading_not_found", "message": "批改结果不存在"})
    session = store.quiz_sessions.get(result.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "quiz_not_found", "message": "测验不存在"})
    _check_teacher_access(session.course_id, user, store)
    pending = next((item for item in result.per_question if item["question_id"] == payload.question_id and item["method"] == "manual_pending"), None)
    if pending is None:
        raise HTTPException(status_code=409, detail={"code": "no_review_needed", "message": "没有待复核的主观题"})
    if payload.score > pending["max_score"]:
        raise HTTPException(status_code=422, detail={"code": "score_exceeds_max", "message": "得分不能超过题目满分"})
    with store.lock:
        previous_score = pending["score"]
        pending["score"] = payload.score
        pending["method"] = "teacher"
        pending["why"] = payload.reason
        result.total = sum(item["score"] for item in result.per_question)
        result.status = "graded" if not any(item["method"] == "manual_pending" for item in result.per_question) else "needs_review"
        result.graded_by = "teacher"
        result.graded_at = datetime.now(timezone.utc)
        session.status = result.status
        store.rebuild_mastery()
    store.audit("grading_review", user.id, {
        "result_id": result_id, "session_id": session.id, "student_id": session.user_id,
        "course_id": session.course_id, "question_id": payload.question_id,
        "previous_score": previous_score, "new_score": payload.score, "reason": payload.reason,
    })
    return grading_out(result)


def _session_visible_to_teacher(session, user: User, store: Store) -> bool:
    return session is not None and (
        user.role == "admin"
        or session.course_id is None
        or store.can_access_course(user, session.course_id, teaching=True)
    )


def _check_teacher_access(course_id: str | None, user: User, store: Store) -> None:
    if user.role == "admin" or course_id is None:
        return
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程批改结果"})
