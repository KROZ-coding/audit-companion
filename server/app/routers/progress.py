from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_current_user, get_store, require_roles
from ..models import User
from ..store import Store

router = APIRouter(prefix="/api/progress", tags=["progress"])


@router.get("/me")
def my_progress(course_id: str | None = None, user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> dict:
    if course_id is not None and not store.can_access_course(user, course_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程学情"})
    rows = [
        {"course_id": mastery_course_id, "knowledge_point": point, **value}
        for (user_id, mastery_course_id, point), value in store.mastery.items()
        if user_id == user.id and (course_id is None or mastery_course_id == course_id)
    ]
    return {
        "user_id": user.id,
        "mastery": rows,
        "mastered": [row for row in rows if row["attempts"] >= 2 and row["mastery"] >= 80],
        "weak": [row for row in rows if row["attempts"] >= 2 and row["mastery"] < 60],
        "insufficient": [row for row in rows if row["attempts"] < 2],
        "report_status": "ready" if user.id in store.reports else "not_generated",
        "latest_report": store.reports.get(user.id),
    }


@router.get("/history")
def history(
    limit: int = 20,
    course_id: str | None = None,
    user: User = Depends(get_current_user),
    store: Store = Depends(get_store),
) -> list[dict]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail={"code": "invalid_limit", "message": "limit 必须在 1 到 100 之间"})
    if course_id is not None and not store.can_access_course(user, course_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程历史"})
    return _history_rows(store, user.id, limit, course_id)


def _history_rows(store: Store, user_id: str, limit: int, course_id: str | None = None) -> list[dict]:
    results_by_session = {result.session_id: result for result in store.results.values()}
    sessions = sorted(
        (
            session for session in store.quiz_sessions.values()
            if session.user_id == user_id and session.submitted_at is not None
            and session.id in results_by_session
            and (course_id is None or session.course_id == course_id)
        ),
        key=lambda session: session.started_at,
        reverse=True,
    )
    history_rows: list[dict] = []
    for session in sessions[:limit]:
        result = results_by_session[session.id]
        snapshots = {question["id"]: question for question in session.questions}
        wrong_questions = []
        for item in result.per_question:
            if item["method"] == "manual_pending" or item["score"] >= item["max_score"]:
                continue
            question = snapshots.get(item["question_id"], {})
            wrong_questions.append({
                "question_id": item["question_id"],
                "stem": question.get("stem", ""),
                "type": question.get("type", ""),
                "score": item["score"],
                "max_score": item["max_score"],
                "why": item["why"],
                "submitted_answer": result.submitted_answers.get(item["question_id"]),
                "knowledge_points": item.get("knowledge_points", []),
            })
        history_rows.append({
            "session_id": session.id,
            "course_id": session.course_id,
            "course_name": store.courses[session.course_id].name if session.course_id in store.courses else "通用",
            "title": session.title,
            "status": result.status,
            "started_at": session.started_at.isoformat(),
            "submitted_at": session.submitted_at.isoformat() if session.submitted_at else None,
            "total": result.total,
            "max_total": result.max_total,
            "wrong_questions": wrong_questions,
        })
    return history_rows


@router.get("/students/{student_id}")
def student_progress(
    student_id: str,
    course_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程学情"})
    student = store.users.get(student_id)
    if student is None or student.role != "student" or (course_id, student_id) not in store.enrollments:
        raise HTTPException(status_code=404, detail={"code": "student_not_found", "message": "该课程中未找到此学生"})
    mastery = sorted(
        [
            {"knowledge_point": point, **value}
            for (uid, cid, point), value in store.mastery.items()
            if uid == student_id and cid in {course_id, None}
        ],
        key=lambda item: item["mastery"],
    )
    history_rows = _history_rows(store, student_id, 20, course_id)
    classroom = [item for item in reversed(store.roll_calls) if item["student_id"] == student_id and item["course_id"] == course_id][:10]
    chats = [
        {
            "question": item.get("question", ""),
            "answer": (item.get("response") or {}).get("answer_markdown", ""),
            "created_at": item.get("created_at"),
            "important": bool(item.get("important", False)),
        }
        for item in reversed(store.chat_history)
        if item.get("user_id") == student_id and item.get("course_id") == course_id
    ][:10]
    return {
        "student": {
            "id": student.id, "username": student.username,
            "display_name": student.display_name, "student_number": student.student_number,
            "class_name": store.enrollments[(course_id, student_id)],
        },
        "course_id": course_id,
        "mastery": mastery,
        "mastered_count": sum(item["attempts"] >= 2 and float(item["mastery"]) >= 80 for item in mastery),
        "weak_count": sum(item["attempts"] >= 2 and float(item["mastery"]) < 60 for item in mastery),
        "insufficient_count": sum(item["attempts"] < 2 for item in mastery),
        "history": history_rows,
        "classroom": classroom,
        "chats": chats,
    }


@router.post("/report")
def create_report(user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> dict:
    course_id = None
    rows = sorted(
        [
            {"course_id": mastery_course_id, "knowledge_point": point, **value}
            for (user_id, mastery_course_id, point), value in store.mastery.items()
            if user_id == user.id and (course_id is None or mastery_course_id == course_id)
        ],
        key=lambda row: row["mastery"],
    )
    weak = [row for row in rows if row["mastery"] < 60]
    recent = [
        {
            "result_id": result.id, "total": result.total,
            "max_total": result.max_total, "status": result.status,
        }
        for result in reversed(list(store.results.values()))
        if (session := store.quiz_sessions.get(result.session_id)) and session.user_id == user.id
    ][:5]
    advice = [f"复习知识点：{row['knowledge_point']}" for row in weak[:5]]
    if not advice:
        advice.append("当前没有低于 60 分的知识点，继续保持练习。")
    markdown = "\n".join([
        "# 学情报告",
        "",
        "## 掌握情况",
        *(f"- {row['knowledge_point']}：{row['mastery']:.2f} 分，练习 {row['attempts']} 次" for row in rows),
        "",
        "## 学习建议",
        *(f"- {item}" for item in advice),
    ])
    source = "rule"
    llm_advice = _llm_advice(store, user, rows, recent)
    if llm_advice:
        markdown = llm_advice
        source = f"llm:{store.settings.llm_model}"
    report = {
        "status": "ready", "source": source,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markdown": markdown, "mastery": rows, "recent_results": recent,
    }
    store.reports[user.id] = report
    store.audit("progress_report_request", user.id)
    return {"user_id": user.id, "report": report}


def _llm_advice(store: Store, user: User, rows: list[dict], recent: list[dict]) -> str | None:
    from ..services.llm_client import LLMClient

    client = LLMClient(store.settings, channel="teacher" if user.role in {"teacher", "admin"} else "student")
    if not client.configured or not rows:
        return None
    mastery_lines = "\n".join(
        f"- {row['knowledge_point']}：{row['mastery']:.1f} 分（练习 {row['attempts']} 次）" for row in rows
    )
    prompt = (
        f"掌握度数据：\n{mastery_lines}\n"
        f"最近测验：{recent}\n\n"
        "请写一份 Markdown 学情报告，包含：## 掌握情况（逐知识点点评）、## 薄弱环节（优先列出低于 60 分的）、"
        "## 学习建议（3~5 条具体可执行的动作）。只输出 Markdown 正文。"
    )
    text = client.complete([{"role": "user", "content": prompt}], temperature=0.4)
    return text.strip() if text and text.strip() else None


@router.get("/class")
def class_progress(course_id: str | None = None, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> dict:
    if course_id is not None:
        if course_id not in store.courses:
            raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
        if not store.can_access_course(user, course_id, teaching=True):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程学情"})
        student_ids = {student.id for student in store.students_for_course(course_id)}
    else:
        if user.role == "admin":
            student_ids = {student.id for student in store.users.values() if student.role == "student"}
        else:
            student_ids = {
                student.id
                for course in store.courses_for_user(user)
                for student in store.students_for_course(course.id)
            }
    course_ids = {course_id} if course_id is not None else {
        course.id for course in store.courses_for_user(user)
    }
    rows = [
        {"user_id": user_id, "course_id": mastery_course_id, "knowledge_point": point, **value}
        for (user_id, mastery_course_id, point), value in store.mastery.items()
        if user_id in student_ids and (mastery_course_id is None or mastery_course_id in course_ids)
    ]
    grouped: dict[str, list[float]] = {}
    for row in rows:
        if row.get("attempts", 0) < 2:
            continue
        grouped.setdefault(row["knowledge_point"], []).append(row["mastery"])
    weak_points = sorted(
        [
            {"knowledge_point": point, "average_mastery": round(sum(values) / len(values), 2), "student_count": len(values)}
            for point, values in grouped.items()
        ],
        key=lambda item: item["average_mastery"],
    )
    return {
        "course_id": course_id,
        "student_count": len(student_ids),
        "students_with_data": len({row["user_id"] for row in rows}),
        "mastery": rows,
        "weak_points": weak_points,
    }
