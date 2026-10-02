import json
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import get_store, require_roles
from ..models import PracticeSession, User
from ..schemas.api import (
    PracticeDetailOut, PracticeGenerateRequest, PracticeListItemOut,
    PracticeQuestionOut, PracticeSubmitRequest,
)
from ..services.llm_client import LLMClient
from ..store import Store

router = APIRouter(prefix="/api/practice", tags=["practice"])

PRACTICE_SCORES = {"single_choice": 10, "multi_choice": 15, "judge": 10, "fill": 10}
MODEL_FAILURE_REASONS = {
    "not_configured": "model_unconfigured",
    "http_error": "model_http_error",
    "provider_error": "model_http_error",
    "rate_limited": "model_rate_limited",
    "busy": "model_busy",
    "cooling_down": "model_cooling_down",
    "timeout": "model_timeout",
    "transport_error": "model_transport_error",
    "invalid_response": "model_invalid_response",
    "invalid_json": "model_invalid_response",
    "empty_response": "model_empty_response",
}


def _source_questions(store: Store, user: User, course_id: str | None, limit: int) -> list[str]:
    seen: set[str] = set()
    asked: list[str] = []
    for item in reversed(store.chat_history):
        if item.get("user_id") != user.id:
            continue
        if course_id is not None and item.get("course_id") != course_id:
            continue
        question = str(item.get("question") or "").strip()
        if not question or question in seen:
            continue
        seen.add(question)
        asked.append(question[:400])
        if len(asked) >= limit:
            break
    return list(reversed(asked))


def _normalize_questions(raw: object, count: int) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    items = raw.get("questions")
    if not isinstance(items, list):
        return []
    normalized: list[dict] = []
    for item in items:
        if len(normalized) >= count:
            break
        if not isinstance(item, dict):
            continue
        qtype = item.get("type")
        stem = str(item.get("stem") or "").strip()
        options = item.get("options")
        answer = item.get("answer")
        if qtype not in PRACTICE_SCORES or not stem:
            continue
        if qtype in {"single_choice", "multi_choice"}:
            if not isinstance(options, list) or not 2 <= len(options) <= 6:
                continue
            options = [str(option) for option in options if str(option).strip()]
            if len(options) < 2:
                continue
            if qtype == "single_choice":
                if not isinstance(answer, int) or not 0 <= answer < len(options):
                    continue
            elif not isinstance(answer, list) or not answer or not all(
                isinstance(index, int) and 0 <= index < len(options) for index in answer
            ) or len(set(answer)) != len(answer):
                continue
        elif qtype == "judge":
            options = ["正确", "错误"]
            if not isinstance(answer, bool):
                continue
        else:  # fill
            options = []
            answer = str(answer or "").strip()
            if not answer:
                continue
        knowledge = [str(point) for point in item.get("knowledge_points") or [] if str(point).strip()][:4]
        normalized.append({
            "id": f"p{uuid4().hex[:12]}",
            "type": qtype,
            "stem": stem[:500],
            "options": options,
            "answer": answer,
            "reference_answer": str(item.get("reference_answer") or "").strip()[:600],
            "knowledge_points": knowledge,
            "score": PRACTICE_SCORES[qtype],
        })
    return normalized


def _build_prompt(source: list[str], count: int) -> list[dict[str, str]]:
    listing = "\n".join(f"- {question}" for question in source)
    return [
        {"role": "system", "content": "你是审计学命题助手,只输出一个 JSON 对象,不要输出任何解释或代码块以外的内容。"},
        {"role": "user", "content": (
            f"学生最近在学习答疑中向 AI 提问过:\n{listing}\n\n"
            f"请围绕这些问题反映的知识点,生成 {count} 道审计学练习题。"
            "只允许以下题型与答案格式:\n"
            'single_choice:options 恰好 4 个选项字符串,answer 为正确选项的下标整数;\n'
            'multi_choice:options 恰好 4 个选项字符串,answer 为正确选项下标数组;\n'
            'judge:answer 为 true 或 false;\n'
            'fill:answer 为简短标准答案字符串。\n'
            '输出格式:{"questions":[{"type":"...","stem":"题干","options":[...],"answer":...,'
            '"reference_answer":"解析要点","knowledge_points":["知识点"]}]}'
        )},
    ]


def _is_correct(qtype: str, given: object, answer: object) -> bool:
    if qtype == "single_choice":
        return isinstance(given, int) and given == answer
    if qtype == "multi_choice":
        if not isinstance(given, list) or not isinstance(answer, list):
            return False
        try:
            return sorted(given) == sorted(answer)
        except TypeError:
            return False
    if qtype == "judge":
        return isinstance(given, bool) and given == answer
    if qtype == "fill":
        return isinstance(given, str) and given.strip().casefold() == str(answer).strip().casefold()
    return False


def _grade(questions: list[dict], answers: dict) -> dict:
    per_question = []
    total = 0.0
    max_total = 0.0
    for question in questions:
        max_score = question["score"]
        max_total += max_score
        given = answers.get(question["id"])
        correct = _is_correct(question["type"], given, question["answer"])
        score = max_score if correct else 0
        total += score
        per_question.append({
            "question_id": question["id"],
            "score": score,
            "max_score": max_score,
            "correct": correct,
            "your_answer": given,
            "answer": question["answer"],
            "reference_answer": question.get("reference_answer", ""),
            "why": "回答正确" if correct else "与标准答案不一致,请参考解析",
        })
    return {
        "per_question": per_question,
        "total": total,
        "max_total": max_total,
        "status": "graded",
        "graded_at": datetime.now(timezone.utc).isoformat(),
    }


def _detail(practice) -> PracticeDetailOut:
    return PracticeDetailOut(
        id=practice.id,
        status=practice.status,
        course_id=practice.course_id,
        created_at=practice.created_at,
        source_questions=practice.source_questions,
        questions=[PracticeQuestionOut(
            id=question["id"], type=question["type"], stem=question["stem"],
            options=question["options"], score=question["score"],
            knowledge_points=question["knowledge_points"],
        ) for question in practice.questions],
        answers=practice.answers if practice.status == "graded" else {},
        result=practice.result,
    )


def _owned_practice(store: Store, practice_id: str, user: User):
    practice = store.practices.get(practice_id)
    if practice is None or practice.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "practice_not_found", "message": "练习不存在"})
    return practice


@router.post("/generate", response_model=PracticeDetailOut)
async def generate(
    payload: PracticeGenerateRequest,
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> PracticeDetailOut:
    course_id = payload.course_id
    if course_id is not None:
        if course_id not in store.courses:
            raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
        if not store.can_access_course(user, course_id):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权使用该课程生成练习"})
    source_mode = "single" if (payload.source_question or "").strip() else "history"
    if source_mode == "single":
        source = [payload.source_question.strip()[:400]]
    else:
        source = _source_questions(store, user, course_id, limit=payload.count + 2)
        if not source:
            raise HTTPException(status_code=409, detail={
                "code": "no_chat_history",
                "message": "还没有答疑记录:先去「学习答疑」向 AI 提问,再回来生成练习",
            })
    if not store.reserve_chat_call(user.id):
        store.audit("chat_quota_exceeded", user.id, {"intent": "practice"})
        raise HTTPException(status_code=429, detail={
            "code": "chat_quota_exceeded",
            "message": f"今日 AI 使用次数已达上限({store.settings.chat_daily_limit} 次),请明天再试",
        })

    started = perf_counter()
    client = LLMClient(store.settings, channel="student")
    result = await client.complete_json_async(_build_prompt(source, payload.count), temperature=0.2)
    questions = _normalize_questions(result, payload.count)
    if result is None or not questions:
        if not client.last_request_sent:
            store.release_chat_call(user.id)
        outcome = f"model_{client.last_error or 'unavailable'}"
        store.record_usage(
            user.id, "practice", client.last_model or store.settings.llm_model or "unconfigured",
            int((perf_counter() - started) * 1000), outcome, course_id=course_id,
        )
        raise HTTPException(status_code=503, detail={
            "code": MODEL_FAILURE_REASONS.get(client.last_error or "", "model_unavailable"),
            "message": "本次练习生成失败,请稍后重试",
        })
    store.record_usage(
        user.id, "practice", client.last_model or store.settings.llm_model or "unconfigured",
        int((perf_counter() - started) * 1000), "ok", course_id=course_id,
        prompt_tokens=client.last_usage["prompt_tokens"],
        completion_tokens=client.last_usage["completion_tokens"],
    )
    practice = PracticeSession(
        id=str(uuid4()), user_id=user.id, course_id=course_id,
        source_questions=source, questions=questions,
    )
    with store.lock:
        store.practices[practice.id] = practice
    store.audit("practice_generate", user.id, {
        "practice_id": practice.id, "count": len(questions), "course_id": course_id,
        "source_mode": source_mode,
    })
    return _detail(practice)


@router.get("", response_model=list[PracticeListItemOut])
def list_practices(
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> list[PracticeListItemOut]:
    items = [practice for practice in store.practices.values() if practice.user_id == user.id]
    items.sort(key=lambda practice: practice.created_at, reverse=True)
    return [PracticeListItemOut(
        id=practice.id, created_at=practice.created_at, course_id=practice.course_id,
        status=practice.status, question_count=len(practice.questions),
        total=practice.result.get("total") if practice.result else None,
        max_total=practice.result.get("max_total", 0) if practice.result else sum(q["score"] for q in practice.questions),
        source_questions=practice.source_questions,
    ) for practice in items]


@router.get("/{practice_id}", response_model=PracticeDetailOut)
def get_practice(
    practice_id: str,
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> PracticeDetailOut:
    return _detail(_owned_practice(store, practice_id, user))


@router.post("/{practice_id}/submit", response_model=PracticeDetailOut)
async def submit(
    practice_id: str, payload: PracticeSubmitRequest,
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> PracticeDetailOut:
    practice = _owned_practice(store, practice_id, user)
    unknown = set(payload.answers).difference(question["id"] for question in practice.questions)
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_question", "message": "提交中包含不属于该练习的题目"})
    with store.lock:
        if practice.status != "ongoing":
            raise HTTPException(status_code=409, detail={"code": "practice_already_submitted", "message": "该练习已经提交过"})
        practice.answers = dict(payload.answers)
        practice.result = _grade(practice.questions, practice.answers)
        practice.status = "graded"
        practice.graded_at = datetime.now(timezone.utc)
    store.audit("practice_submit", user.id, {
        "practice_id": practice.id, "total": practice.result["total"],
    })
    return _detail(practice)


@router.delete("/{practice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_practice(
    practice_id: str,
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> None:
    practice = _owned_practice(store, practice_id, user)
    with store.lock:
        store.practices.pop(practice.id, None)
    store.audit("practice_delete", user.id, {"practice_id": practice.id})


@router.delete("")
def clear_practices(
    user: User = Depends(require_roles("student")),
    store: Store = Depends(get_store),
) -> dict:
    with store.lock:
        doomed = [key for key, practice in store.practices.items() if practice.user_id == user.id]
        for key in doomed:
            store.practices.pop(key, None)
    store.audit("practice_clear", user.id, {"deleted": len(doomed)})
    return {"deleted": len(doomed)}
