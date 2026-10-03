import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_current_user, get_store, require_roles
from ..schemas.api import AiInsightRequest
from ..models import User
from ..services.llm_client import LLMClient
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


def _insight_snapshot(store: Store, user: User, course_id: str) -> dict:
    """按权限聚合课程数据快照(纯代码取数,模型只读摘要)。"""
    course = store.courses[course_id]
    students = store.students_for_course(course_id)
    roster = []
    for student in students:
        sessions = [s for s in store.quiz_sessions.values() if s.user_id == student.id and s.course_id == course_id]
        graded = [s for s in sessions if s.status in {"graded", "needs_review"}]
        results = [store.results[s.id] for s in graded if s.id in store.results]
        total_score = sum(r.total for r in results)
        max_score = sum(r.max_total for r in results)
        pending = sum(1 for r in results if r.status == "needs_review")
        asks = [c for c in store.chat_history if c.get("user_id") == student.id and c.get("course_id") == course_id]
        last_ask = asks[-1]["created_at"][:16].replace("T", " ") if asks else None
        recent_questions = [
            {"time": c.get("created_at", "")[:16].replace("T", " "), "question": str(c.get("question") or "")[:60]}
            for c in asks[-3:]
        ]
        roster.append({
            "name": student.display_name, "username": student.username,
            "quiz_count": len(graded),
            "score_ratio": round(total_score / max_score, 2) if max_score else None,
            "pending_review": pending,
            "ask_count": len(asks),
            "last_ask": last_ask,
            "recent_questions": recent_questions,
        })
    mastery: dict[str, list[float]] = {}
    for (uid, mid, point), value in store.mastery.items():
        if mid == course_id and value.get("attempts", 0) >= 2:
            mastery.setdefault(point, []).append(value["mastery"])
    weak_points = sorted(
        ((point, round(sum(v) / len(v), 1)) for point, v in mastery.items() if sum(v) / len(v) < 60),
        key=lambda item: item[1],
    )[:6]
    strong_points = sorted(
        ((point, round(sum(v) / len(v), 1)) for point, v in mastery.items() if sum(v) / len(v) >= 80),
        key=lambda item: -item[1],
    )[:6]
    return {
        "course": f"{course.name} {course.term}",
        "student_count": len(roster),
        "students": roster,
        "weak_points": weak_points,
        "strong_points": strong_points,
    }


@router.post("/ai-insight")
async def ai_insight(
    payload: AiInsightRequest,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    """教师 AI 学情问答:预查数据 + 模型解读。教师只能问自己课程。"""
    course_id = payload.course_id
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程学情"})
    if not store.reserve_chat_call(user.id):
        store.audit("chat_quota_exceeded", user.id, {"intent": "ai_insight"})
        raise HTTPException(status_code=429, detail={"code": "chat_quota_exceeded", "message": "今日 AI 次数已达上限"})
    snapshot = _insight_snapshot(store, user, course_id)
    if not snapshot["student_count"]:
        raise HTTPException(status_code=409, detail={"code": "no_students", "message": "课程里还没有学生"})
    prompt = (
        f"你是《审计学》课程的教学助教。课程:{snapshot['course']},共 {snapshot['student_count']} 名学生。\n"
        f"学生数据(JSON,含每人测验完成数、得分率、待复核数、答疑次数与最近提问时间):\n"
        f"{json.dumps(snapshot['students'], ensure_ascii=False)}\n\n"
        f"薄弱知识点(均分<60):{snapshot['weak_points'] or '无'}\n"
        f"掌握较好知识点(均分≥80):{snapshot['strong_points'] or '无'}\n\n"
        f"教师提问:{payload.question}\n\n"
        "请直接回答教师的问题。要求:引用具体学生姓名和数据时必须来自上面 JSON,不得编造;"
        "回答用简洁的中文,可用 Markdown 列表;不超过 400 字。"
    )
    client = LLMClient(store.settings, channel="teacher")
    text = client.complete([{"role": "user", "content": prompt}], temperature=0.3)
    usage = getattr(client, "last_usage", {"prompt_tokens": 0, "completion_tokens": 0})
    model = getattr(client, "last_model", None) or store.settings.llm_model or "unconfigured"
    if not text or not text.strip():
        outcome = f"model_{getattr(client, 'last_error', None) or 'unavailable'}"
        store.record_usage(user.id, "ai_insight", model, 0, outcome, course_id=course_id)
        raise HTTPException(status_code=503, detail={"code": "model_unavailable", "message": "本次解读生成失败,请稍后重试"})
    store.record_usage(user.id, "ai_insight", model, 0, "ok", course_id=course_id,
                       prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
    store.audit("ai_insight", user.id, {"course_id": course_id, "question": payload.question[:120]})
    return {"answer_markdown": text.strip(), "snapshot_students": snapshot["student_count"]}
