from asyncio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from ..deps import get_current_user, get_store, require_roles
from ..models import User
from ..schemas.api import ChatAskRequest, ChatResponse
from ..services.chat_service import build_answer, build_answer_stream
from ..services.maxkb_client import MaxKBClient
from ..store import Store

router = APIRouter(prefix="/api/chat", tags=["chat"])

CHAT_HISTORY_PER_USER = 200  # 单用户答疑历史上限,长期运行防膨胀

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


@router.post("/ask", response_model=ChatResponse)
async def ask(
    payload: ChatAskRequest,
    user: User = Depends(get_current_user),
    store: Store = Depends(get_store),
) -> ChatResponse:
    course_id = payload.course_id
    if course_id is None and user.role != "admin":
        courses = store.courses_for_user(user)
        if len(courses) != 1:
            raise HTTPException(
                status_code=400,
                detail={"code": "course_required", "message": "请指定要使用的课程"},
            )
        course_id = courses[0].id
    if course_id is not None:
        if course_id not in store.courses:
            raise HTTPException(
                status_code=404,
                detail={"code": "course_not_found", "message": "课程不存在"},
            )
        if not store.can_access_course(user, course_id):
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": "无权使用该课程答疑"},
            )
    if not store.reserve_chat_call(user.id):
        store.audit("chat_quota_exceeded", user.id, {"daily_limit": store.settings.chat_daily_limit})
        raise HTTPException(
            status_code=429,
            detail={"code": "chat_quota_exceeded", "message": f"今日答疑次数已达上限（{store.settings.chat_daily_limit} 次），请明天再试"},
        )
    maxkb = MaxKBClient(store.settings, api_keys=store.maxkb_api_keys())
    started = perf_counter()
    records = await maxkb.retrieve(payload.question, course_id) or []
    retrieval_source = "maxkb" if records else "none"
    if not records:
        # BM25 检索是纯 CPU 的同步操作，放到线程池里，避免 60 人并发时卡住事件循环。
        records = await to_thread(
            store.knowledge.search, payload.question, top_k=store.settings.knowledge_top_k
        )
        if records:
            retrieval_source = "local"
    channel = "teacher" if user.role in {"teacher", "admin"} else "student"
    result, model_error, model, token_usage, request_sent = await build_answer(
        store.settings, payload.question, records, channel=channel
    )
    if result is None:
        if not request_sent:
            store.release_chat_call(user.id)
        unconfigured = model_error == "not_configured"
        outcome = f"model_{model_error or 'unavailable'}"
        answer = "大模型尚未配置，当前无法提供回答。请联系管理员检查模型服务配置。" if unconfigured else "模型服务已配置，但本次未返回可用回答。"
        response = ChatResponse(
            answer_markdown=answer,
            sections={"conclusion": answer, "standards": "", "case": "", "ideology": ""},
            mind_map=[], sources=[], degraded=True,
            degraded_reason=MODEL_FAILURE_REASONS.get(model_error or "", "model_unavailable"),
        )
    else:
        response = ChatResponse(**result)
        outcome = "ok" if records else "degraded_no_kb"
    store.record_usage(
        user.id,
        "chat",
        model or store.settings.llm_model or "unconfigured",
        int((perf_counter() - started) * 1000),
        outcome,
        course_id=course_id,
        prompt_tokens=token_usage["prompt_tokens"],
        completion_tokens=token_usage["completion_tokens"],
    )
    with store.lock:
        mine = [item for item in store.chat_history if item["user_id"] == user.id]
        if len(mine) >= CHAT_HISTORY_PER_USER:
            drop_ids = {item["id"] for item in mine[:len(mine) - CHAT_HISTORY_PER_USER + 1]}
            store.chat_history[:] = [item for item in store.chat_history if item["id"] not in drop_ids]
        store.chat_history.append({
            "id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "important": False,
            "user_id": user.id,
            "course_id": course_id,
            "question": payload.question,
            "response": response.model_dump(),
        })
    store.audit("chat_ask", user.id, {"degraded": response.degraded, "retrieval": retrieval_source})
    return response


@router.get("/history")
def history(limit: int = Query(default=50, ge=1, le=100), user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> list[dict]:
    with store.lock:
        return [item for item in reversed(store.chat_history) if item["user_id"] == user.id][:limit]


@router.delete("/history", status_code=204)
def clear_history(user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> None:
    with store.lock:
        store.chat_history[:] = [item for item in store.chat_history if item["user_id"] != user.id]
    store.audit("chat_history_clear", user.id)


@router.get("/export")
def export_history(user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> dict:
    with store.lock:
        items = [item for item in store.chat_history if item["user_id"] == user.id]
    return {"user_id": user.id, "items": items}


@router.get("/monitor")
def monitor(
    limit: int = Query(default=100, ge=1, le=500),
    course_id: str | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> list[dict]:
    allowed_courses = None if user.role == "admin" else {course.id for course in store.courses_for_user(user)}
    if course_id is not None and course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if course_id is not None and allowed_courses is not None and course_id not in allowed_courses:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程答疑"})
    with store.lock:
        users = {item.id: item for item in store.users.values()}
        items = [
            item for item in reversed(store.chat_history)
            if (allowed_courses is None or item.get("course_id") in allowed_courses)
            and (course_id is None or item.get("course_id") == course_id)
        ][:limit]
    return [
        {
            **item,
            "id": item.setdefault("id", str(uuid4())),
            "created_at": item.get("created_at"),
            "important": bool(item.get("important", False)),
            "user_id": item["user_id"],
            "display_name": users.get(item["user_id"]).display_name if users.get(item["user_id"]) else "未知用户",
            "student_number": users.get(item["user_id"]).student_number if users.get(item["user_id"]) else "",
            "course_name": store.courses[item["course_id"]].name if item.get("course_id") in store.courses else "",
        }
        for item in items
    ]


@router.patch("/monitor/{chat_id}/important")
def set_important(
    chat_id: str,
    payload: dict,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    important = payload.get("important")
    if not isinstance(important, bool):
        raise HTTPException(status_code=422, detail={"code": "invalid_important", "message": "重点状态必须为布尔值"})
    item = next((row for row in store.chat_history if row.get("id") == chat_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "chat_not_found", "message": "答疑记录不存在"})
    course_id = item.get("course_id")
    if user.role != "admin" and (course_id is None or not store.can_access_course(user, course_id, teaching=True)):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权标记该课程答疑"})
    item["important"] = important
    store.audit("chat_mark_important", user.id, {"chat_id": chat_id, "important": important})
    return {"id": chat_id, "important": important}


@router.post("/ask/stream")
async def ask_stream(
    payload: ChatAskRequest,
    user: User = Depends(get_current_user),
    store: Store = Depends(get_store),
):
    """SSE 流式答疑:delta(累计原文) → done(完整结构化结果) / error。

    前端回退:收到 error 且尚未有内容时,可改用非流式 /ask 重试。
    """
    import json as _json

    from fastapi.responses import StreamingResponse

    course_id = payload.course_id
    if course_id is None and user.role != "admin":
        courses = store.courses_for_user(user)
        if len(courses) != 1:
            raise HTTPException(
                status_code=400,
                detail={"code": "course_required", "message": "请指定要使用的课程"},
            )
        course_id = courses[0].id
    if course_id is not None:
        if course_id not in store.courses:
            raise HTTPException(
                status_code=404,
                detail={"code": "course_not_found", "message": "课程不存在"},
            )
        if not store.can_access_course(user, course_id):
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": "无权使用该课程答疑"},
            )
    if not store.reserve_chat_call(user.id):
        store.audit("chat_quota_exceeded", user.id, {"daily_limit": store.settings.chat_daily_limit})
        raise HTTPException(
            status_code=429,
            detail={"code": "chat_quota_exceeded", "message": f"今日答疑次数已达上限（{store.settings.chat_daily_limit} 次），请明天再试"},
        )
    maxkb = MaxKBClient(store.settings, api_keys=store.maxkb_api_keys())
    started = perf_counter()
    records = await maxkb.retrieve(payload.question, course_id) or []
    retrieval_source = "maxkb" if records else "none"
    if not records:
        records = await to_thread(
            store.knowledge.search, payload.question, top_k=store.settings.knowledge_top_k
        )
        if records:
            retrieval_source = "local"
    channel = "teacher" if user.role in {"teacher", "admin"} else "student"

    async def event_stream():
        try:
            yield f"data: {_json.dumps({'type': 'retrieval', 'sources': [{'name': r.get('name')} for r in records], 'source': retrieval_source}, ensure_ascii=False)}\n\n"

            def on_delta(buffer: str) -> None:
                pass  # 原始 JSON 累计文本不适合直接展示;流式感由下方阶段事件承担

            result, model_error, model, token_usage, request_sent = await build_answer_stream(
                store.settings, payload.question, records, channel=channel, on_delta=on_delta
            )
            if result is None:
                if not request_sent:
                    store.release_chat_call(user.id)
                outcome = f"model_{model_error or 'unavailable'}"
                store.record_usage(
                    user.id, "chat", model or store.settings.llm_model or "unconfigured",
                    int((perf_counter() - started) * 1000), outcome, course_id=course_id,
                )
                failure = MODEL_FAILURE_REASONS.get(model_error or "", "model_unavailable")
                yield f"data: {_json.dumps({'type': 'error', 'degraded_reason': failure, 'message': '模型服务已配置，但本次未返回可用回答。' if failure != 'model_unconfigured' else '大模型尚未配置，当前无法提供回答。'}, ensure_ascii=False)}\n\n"
                return
            outcome = "ok" if records else "degraded_no_kb"
            store.record_usage(
                user.id, "chat", model or store.settings.llm_model or "unconfigured",
                int((perf_counter() - started) * 1000), outcome, course_id=course_id,
                prompt_tokens=token_usage["prompt_tokens"],
                completion_tokens=token_usage["completion_tokens"],
            )
            with store.lock:
                store.chat_history.append({
                    "id": str(uuid4()),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "important": False,
                    "user_id": user.id,
                    "course_id": course_id,
                    "question": payload.question,
                    "response": result,
                })
            store.audit("chat_ask", user.id, {"degraded": result["degraded"], "retrieval": retrieval_source, "stream": True})
            yield f"data: {_json.dumps({'type': 'done', 'result': result}, ensure_ascii=False)}\n\n"
        except Exception as error:  # 流中异常也要以 error 事件结束,避免前端悬挂
            yield f"data: {_json.dumps({'type': 'error', 'degraded_reason': 'model_unavailable', 'message': str(error)[:120]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
