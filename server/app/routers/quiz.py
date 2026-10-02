import asyncio
from datetime import datetime, timezone
from copy import deepcopy
import random
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import get_current_user, get_store, require_roles
from ..models import Question, QuizSession, User
from ..schemas.api import QuizAssignRequest, QuizDraftRequest, QuizSessionOut, QuizQuestionOut, QuizStartRequest, QuizSubmitRequest, QuizSubmitResponse, StudentQuizSubmitResponse
from ..services.grading_service import apply_grading_result, grade_session
from ..store import Store
from .helpers import grading_out, student_grading_out

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


def _snapshot(question: Question) -> dict:
    return {
        "id": question.id, "code": question.code, "type": question.type,
        "stem": question.stem, "options": deepcopy(question.options), "answer": deepcopy(question.answer),
        "reference_answer": question.reference_answer, "rubric": deepcopy(question.rubric),
        "knowledge_points": deepcopy(question.knowledge_points), "difficulty": question.difficulty,
        "status": question.status, "course_id": question.course_id, "chapter": question.chapter,
        "version": question.version,
        "score": {"single_choice": 10, "multi_choice": 15, "judge": 10, "fill": 10, "short_answer": 25, "case": 30}.get(question.type, 10),
    }


def _public(question: dict) -> QuizQuestionOut:
    return QuizQuestionOut(
        id=question["id"], type=question["type"], stem=question["stem"],
        options=question["options"], score=question["score"],
        knowledge_points=question.get("knowledge_points", []), chapter=question.get("chapter"),
    )


def _bank_version(store: Store, course_id: str | None) -> str:
    versions = [
        question.version for question in store.questions.values()
        if question.status == "published" and (course_id is None or question.course_id == course_id)
    ]
    return f"{course_id or 'global'}-v{max(versions, default=1)}"


def _select_questions(
    store: Store, course_id: str | None, chapter: str | None, difficulty: str | None, requested: dict[str, int]
) -> list[dict]:
    selected: list[dict] = []
    for kind, count in requested.items():
        if count <= 0:
            continue
        candidates = [
            q for q in store.questions.values()
            if q.type == kind and q.status == "published"
            and (course_id is None or q.course_id == course_id)
            and (chapter is None or q.chapter == chapter)
            and (difficulty is None or q.difficulty == difficulty)
        ]
        if len(candidates) < count:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "question_bank_insufficient", "message": f"题库缺少 {kind} 题目"})
        selected.extend(_snapshot(q) for q in random.sample(candidates, count))
    return selected


def _random_answer(question: dict) -> object:
    qtype = question["type"]
    correct = question.get("answer")
    options = question.get("options") or []
    if qtype == "single_choice":
        if isinstance(correct, int) and options and random.random() < 0.7:
            return correct
        return random.randrange(len(options)) if options else 0
    if qtype == "multi_choice":
        if isinstance(correct, list) and correct and random.random() < 0.6:
            return list(correct)
        pool = list(range(len(options))) or [0]
        picked = random.sample(pool, random.randint(1, len(pool)))
        return picked
    if qtype == "judge":
        if isinstance(correct, bool) and random.random() < 0.75:
            return correct
        return not bool(correct)
    if qtype == "fill":
        answers = correct if isinstance(correct, list) else [correct]
        good = [item for item in answers if isinstance(item, str) and item.strip()]
        if good and random.random() < 0.7:
            return good[0]
        return "不确定"
    rubric = question.get("rubric") or []
    parts = [str(entry.get("point", "")).strip() for entry in rubric if isinstance(entry, dict) and str(entry.get("point", "")).strip()]
    if parts:
        kept = [part for part in parts if random.random() < 0.7]
        return "；".join(kept) if kept else "暂不理解该知识点"
    reference = str(question.get("reference_answer") or "").strip()
    if reference and random.random() < 0.6:
        return reference
    return "凭印象作答，要点不完整。"


@router.post("/start", response_model=QuizSessionOut)
def start(payload: QuizStartRequest, user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> QuizSessionOut:
    chapter = payload.chapter.strip() if payload.chapter else None
    course_id = payload.course_id
    if user.role in {"student", "teacher"} and course_id is None:
        courses = store.courses_for_user(user)
        if len(courses) != 1:
            raise HTTPException(status_code=400, detail={"code": "course_required", "message": "测验必须指定课程"})
        course_id = courses[0].id
    if course_id is not None:
        if course_id not in store.courses:
            raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
        if not store.can_access_course(user, course_id):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权参加该课程测验"})
    requested = {
        "single_choice": payload.counts.single,
        "multi_choice": payload.counts.multi,
        "judge": payload.counts.judge,
        "fill": payload.counts.fill,
        "short_answer": payload.counts.short,
        "case": payload.counts.case,
    }
    selected = _select_questions(store, course_id, chapter, payload.difficulty, requested)
    if not selected:
        raise HTTPException(status_code=400, detail={"code": "empty_quiz", "message": "至少选择一道题"})
    session = QuizSession(
        id=str(uuid4()), user_id=user.id, course_id=course_id,
        questions=selected, bank_version=_bank_version(store, course_id),
    )
    with store.lock:
        store.quiz_sessions[session.id] = session
    store.audit("quiz_start", user.id, {"session_id": session.id})
    return QuizSessionOut(
        id=session.id, status=session.status, course_id=session.course_id,
        bank_version=session.bank_version, title=session.title, assignment_id=session.assignment_id,
        questions=[_public(q) for q in selected],
    )


@router.post("/assign")
def assign_quiz(
    payload: QuizAssignRequest,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    """Teacher selects published questions to build an assignment for students."""
    if payload.course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, payload.course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权在该课程组卷"})
    questions = []
    for qid in payload.question_ids:
        q = store.questions.get(qid)
        if q is None or q.status != "published":
            raise HTTPException(status_code=422, detail={"code": "question_not_published", "message": f"题目 {qid[:8]} 不存在或未发布"})
        if q.course_id and q.course_id != payload.course_id:
            raise HTTPException(status_code=403, detail={"code": "course_mismatch", "message": f"题目 {qid[:8]} 不属于该课程"})
        questions.append(_snapshot(q))
    if not questions:
        raise HTTPException(status_code=422, detail={"code": "empty_assignment", "message": "至少选择一道题目"})

    students = store.students_for_course(payload.course_id)
    if payload.student_ids is not None:
        selected_ids = set(payload.student_ids)
        if len(selected_ids) != len(payload.student_ids):
            raise HTTPException(status_code=422, detail={"code": "duplicate_student", "message": "学生名单包含重复项"})
        if not selected_ids.issubset({item.id for item in students}):
            raise HTTPException(status_code=422, detail={"code": "student_not_enrolled", "message": "所选学生不属于当前课程"})
        students = [item for item in students if item.id in selected_ids]
    elif payload.class_name:
        students = [
            item for item in students
            if store.enrollments.get((payload.course_id, item.id)) == payload.class_name
        ]
    if not students:
        raise HTTPException(status_code=409, detail={"code": "no_students", "message": "课程暂无学生，请先审核注册"})
    if payload.due_at and payload.due_at.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_due_at", "message": "截止时间必须包含时区"})
    assignment_id = str(uuid4())
    session_ids = []
    with store.lock:
        for student in students:
            session = QuizSession(
                id=str(uuid4()), user_id=student.id, course_id=payload.course_id,
                questions=questions, bank_version=_bank_version(store, payload.course_id),
                title=payload.title, assigned=True, assignment_id=assignment_id,
            )
            store.quiz_sessions[session.id] = session
            session_ids.append(session.id)
        store.assignments[assignment_id] = {
            "id": assignment_id,
            "course_id": payload.course_id,
            "title": payload.title,
            "created_by": user.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "due_at": payload.due_at.isoformat() if payload.due_at else None,
            "class_name": payload.class_name,
            "question_count": len(questions),
            "student_ids": [item.id for item in students],
            "session_ids": session_ids,
        }
    store.audit("quiz_assign", user.id, {
        "course_id": payload.course_id, "title": payload.title,
        "question_count": len(questions), "student_count": len(students),
        "session_ids": session_ids,
    })
    return {
        "status": "assigned", "title": payload.title,
        "assignment_id": assignment_id,
        "question_count": len(questions), "student_count": len(students),
        "session_ids": session_ids,
    }


@router.get("/assigned")
def teacher_assignments(
    course_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> list[dict]:
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程作业"})
    users = store.users
    results_by_session = {item.session_id: item for item in store.results.values()}
    rows = []
    for assignment in store.assignments.values():
        if assignment["course_id"] != course_id:
            continue
        roster = []
        for index, session_id in enumerate(assignment["session_ids"]):
            session = store.quiz_sessions.get(session_id)
            if session is None:
                target_ids = assignment.get("student_ids", [])
                roster.append({
                    "student_id": target_ids[index] if index < len(target_ids) else "",
                    "display_name": "学生记录已删除", "student_number": "", "class_name": "",
                    "session_id": session_id, "status": "record_deleted", "submitted_at": None,
                    "total": None, "max_total": None,
                })
                continue
            student = users.get(session.user_id)
            if student is None:
                continue
            result = results_by_session.get(session.id)
            state = result.status if result else session.status
            roster.append({
                "student_id": student.id,
                "display_name": student.display_name,
                "student_number": student.student_number,
                "class_name": store.enrollments.get((course_id, student.id), assignment.get("class_name") or ""),
                "session_id": session.id,
                "status": state,
                "late": bool(
                    assignment.get("due_at")
                    and datetime.fromisoformat(assignment["due_at"]) < (session.submitted_at or datetime.now(timezone.utc))
                ),
                "submitted_at": session.submitted_at.isoformat() if session.submitted_at else None,
                "total": result.total if result else None,
                "max_total": result.max_total if result else None,
            })
        roster.sort(key=lambda item: (item["display_name"].casefold(), item["student_number"]))
        rows.append({
            "id": assignment["id"], "title": assignment["title"],
            "created_at": assignment["created_at"], "due_at": assignment.get("due_at"),
            "class_name": assignment.get("class_name"),
            "question_count": assignment["question_count"],
            "target_count": len(assignment.get("student_ids", roster)),
            "submitted_count": sum(item["status"] in {"graded", "needs_review"} for item in roster),
            "pending_review_count": sum(item["status"] == "needs_review" for item in roster),
            "roster": roster,
        })
    return sorted(rows, key=lambda item: item["created_at"], reverse=True)


@router.get("/assignments")
def list_assignments(
    course_id: str | None = None,
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> list[dict]:
    if course_id is not None and not store.can_access_course(user, course_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程试卷"})
    sessions = sorted(
        (
            session for session in store.quiz_sessions.values()
            if session.user_id == user.id and session.assigned
            and (course_id is None or session.course_id == course_id)
        ),
        key=lambda session: session.started_at,
        reverse=True,
    )
    return [
        {
            "id": session.id,
            "title": session.title,
            "status": session.status,
            "course_id": session.course_id,
            "question_count": len(session.questions),
            "due_at": store.assignments.get(session.assignment_id, {}).get("due_at"),
            "started_at": session.started_at.isoformat(),
            "submitted_at": session.submitted_at.isoformat() if session.submitted_at else None,
        }
        for session in sessions
    ]


@router.get("/resumable")
def resumable_quizzes(
    course_id: str | None = None,
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> list[dict]:
    if course_id is not None and not store.can_access_course(user, course_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权恢复该课程测验"})
    return [
        {
            "id": session.id,
            "title": session.title,
            "course_id": session.course_id,
            "question_count": len(session.questions),
            "started_at": session.started_at.isoformat(),
        }
        for session in sorted(store.quiz_sessions.values(), key=lambda item: item.started_at, reverse=True)
        if session.user_id == user.id and not session.assigned and session.status == "ongoing"
        and (session.submitted_answers or session.draft_answers) and (course_id is None or session.course_id == course_id)
    ]


@router.get("/{session_id}", response_model=QuizSessionOut)
def get_quiz(session_id: str, user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> QuizSessionOut:
    session = store.quiz_sessions.get(session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "quiz_not_found", "message": "测验不存在"})
    return QuizSessionOut(
        id=session.id, status=session.status, course_id=session.course_id,
        bank_version=session.bank_version, title=session.title, assignment_id=session.assignment_id,
        questions=[_public(q) for q in session.questions],
        saved_answers=session.submitted_answers,
        draft_answers=session.draft_answers,
        draft_updated_at=session.draft_updated_at,
    )


@router.post("/{session_id}/draft")
async def save_draft(
    session_id: str, payload: QuizDraftRequest,
    user: User = Depends(get_current_user), store: Store = Depends(get_store),
) -> dict:
    session = store.quiz_sessions.get(session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "quiz_not_found", "message": "测验不存在"})
    unknown = set(payload.answers).difference(question["id"] for question in session.questions)
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_question", "message": "草稿中包含不属于该测验的题目"})
    with store.lock:
        if session.status != "ongoing":
            raise HTTPException(status_code=409, detail={"code": "quiz_already_submitted", "message": "测验已经提交"})
        if session.draft_answers == payload.answers:
            updated_at = session.draft_updated_at
            return {"session_id": session_id, "saved": False, "draft_updated_at": _iso_optional(updated_at)}
        previous_answers = session.draft_answers
        previous_at = session.draft_updated_at
        session.draft_answers = dict(payload.answers)
        session.draft_updated_at = datetime.now(timezone.utc)
    if not await store.save_async():
        with store.lock:
            session.draft_answers = previous_answers
            session.draft_updated_at = previous_at
        raise HTTPException(status_code=503, detail={"code": "draft_persist_failed", "message": "草稿暂未可靠保存，请稍后重试"})
    return {"session_id": session_id, "saved": True, "draft_updated_at": _iso_optional(session.draft_updated_at)}


def _iso_optional(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.post("/{session_id}/submit", response_model=None)
async def submit(
    session_id: str, payload: QuizSubmitRequest,
    user: User = Depends(get_current_user), store: Store = Depends(get_store),
) -> QuizSubmitResponse | StudentQuizSubmitResponse:
    session = store.quiz_sessions.get(session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "quiz_not_found", "message": "测验不存在"})
    unknown = set(payload.answers).difference(question["id"] for question in session.questions)
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_question", "message": "提交中包含不属于该测验的题目"})
    with store.lock:
        if session.status != "ongoing":
            store.audit("quiz_submit_rejected", user.id, {"session_id": session_id, "status": session.status})
            raise HTTPException(status_code=409, detail={"code": "quiz_already_submitted", "message": "测验已经提交"})
        previous_answers = session.submitted_answers
        session.status = "grading"
        session.submitted_at = datetime.now(timezone.utc)
        session.submitted_answers = dict(payload.answers)
    if not await store.save_async():
        with store.lock:
            session.status = "ongoing"
            session.submitted_at = None
            session.submitted_answers = previous_answers
        raise HTTPException(status_code=503, detail={"code": "submission_persist_failed", "message": "答案暂未可靠保存，请稍后重试"})
    try:
        result = await asyncio.to_thread(grade_session, session, payload.answers, store.settings)
    except Exception as error:
        with store.lock:
            session.status = "ongoing"
            session.submitted_at = None
        raise HTTPException(status_code=503, detail={"code": "grading_failed", "message": "批改暂时失败，请重新提交"}) from error
    with store.lock:
        apply_grading_result(store, session, result)
        session.submitted_answers = {}
        session.draft_answers = {}
        session.draft_updated_at = None
        store.results[result.id] = result
    store.audit("quiz_submit", user.id, {"session_id": session.id, "result_id": result.id})
    if user.role == "student":
        return StudentQuizSubmitResponse(status=result.status, session_id=session.id, result=student_grading_out(result))
    return QuizSubmitResponse(status=result.status, session_id=session.id, result=grading_out(result))


@router.post("/demo-random")
def demo_random(
    payload: QuizStartRequest,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    """Teacher preview: grade a generated answer without changing student records."""
    course_id = payload.course_id
    if course_id is None:
        courses = store.courses_for_user(user)
        if len(courses) != 1:
            raise HTTPException(status_code=400, detail={"code": "course_required", "message": "请指定课程"})
        course_id = courses[0].id
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权操作该课程"})
    students = store.students_for_course(course_id)
    if not students:
        raise HTTPException(status_code=409, detail={"code": "no_students", "message": "该课程还没有学生，请先添加选课"})
    student = random.choice(students)
    chapter = payload.chapter.strip() if payload.chapter else None
    requested = {
        "single_choice": payload.counts.single, "multi_choice": payload.counts.multi,
        "judge": payload.counts.judge, "fill": payload.counts.fill,
        "short_answer": payload.counts.short, "case": payload.counts.case,
    }
    selected = _select_questions(store, course_id, chapter, payload.difficulty, requested)
    if not selected:
        raise HTTPException(status_code=400, detail={"code": "empty_quiz", "message": "至少选择一道题"})
    session = QuizSession(
        id=str(uuid4()), user_id=student.id, course_id=course_id,
        questions=selected, bank_version=_bank_version(store, course_id),
    )
    answers = {question["id"]: _random_answer(question) for question in selected}
    session.submitted_at = datetime.now(timezone.utc)
    result = grade_session(session, answers, store.settings)
    store.audit("quiz_demo_random", user.id, {"session_id": session.id, "student_id": student.id})
    return {
        "student": {"id": student.id, "display_name": student.display_name},
        "session_id": session.id,
        "demo": True,
        "questions": [_public(question) for question in selected],
        "result": grading_out(result),
    }
