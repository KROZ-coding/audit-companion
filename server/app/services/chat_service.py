from typing import Any
import logging

from ..config import Settings
from ..services.llm_client import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是《审计学》课程的中文智能助教，面向本科学生。回答必须准确、结构化、可教学。"
    "只输出 JSON，不要输出多余文字。格式："
    "{\"answer_markdown\":\"完整回答（Markdown）\","
    "\"sections\":{\"conclusion\":\"知识点结论\",\"standards\":\"准则/教材依据，无则空字符串\","
    "\"case\":\"案例示例，无则空字符串\",\"ideology\":\"思政启示（诚信、独立性、职业怀疑等），无则空字符串\"},"
    "\"mind_map\":[\"根节点\",\"├─ 一级要点\",\"└─ 一级要点\"]}。"
    "mind_map 字段必须输出且不允许为空列表,固定给 5~8 条树状缩进文本。"
)


def _context_block(records: list[dict[str, Any]]) -> str:
    parts = []
    for index, record in enumerate(records, start=1):
        name = record.get("name") or "未命名资料"
        text = str(record.get("text") or "").strip()
        if text:
            parts.append(f"[资料{index}｜{name}]\n{text}")
    return "\n\n".join(parts)


def sources_from(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": str(record.get("name") or "未命名资料"), "score": record.get("score")} for record in records]


async def build_answer(
    settings: Settings,
    question: str,
    records: list[dict[str, Any]],
    channel: str = "student",
) -> tuple[dict[str, Any] | None, str | None, str | None, dict[str, int], bool]:
    """Return a structured answer and a safe failure category, if any.

    `records` may be empty (knowledge base unavailable) — the model then answers
    from general audit knowledge and the result is marked degraded.
    """
    client = LLMClient(settings, channel=channel)
    if not client.configured:
        return None, "not_configured", None, {"prompt_tokens": 0, "completion_tokens": 0}, False
    context = _context_block(records)
    if context:
        user_prompt = (
            "以下是知识库检索到的课程资料，优先依据它们回答，并在 standards 中写明资料出处：\n"
            f"{context}\n\n学生问题：{question}"
        )
    else:
        user_prompt = f"知识库暂无相关资料，请依据审计学通用知识回答。\n\n学生问题：{question}"
    result = await client.complete_json_async(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
    )
    model = getattr(client, "last_model", None) or settings.llm_model or None
    usage = getattr(client, "last_usage", {"prompt_tokens": 0, "completion_tokens": 0})
    request_sent = bool(getattr(client, "last_request_sent", False))
    if result is None:
        logger.warning("LLM returned no structured answer")
        return (
            None, getattr(client, "last_error", None) or "invalid_response", model,
            usage, request_sent,
        )
    answer = str(result.get("answer_markdown") or "").strip()
    sections = result.get("sections")
    if not answer and isinstance(sections, dict):
        answer = str(sections.get("conclusion") or "").strip()
    if not answer:
        logger.warning("LLM structured response did not include answer content")
        return None, "invalid_response", model, usage, request_sent
    if not isinstance(sections, dict):
        sections = {}
    mind_map = result.get("mind_map")
    return {
        "answer_markdown": answer,
        "sections": {key: str(sections.get(key) or "") for key in ("conclusion", "standards", "case", "ideology")},
        "mind_map": [str(item) for item in mind_map] if isinstance(mind_map, list) else [],
        "sources": sources_from(records),
        "degraded": not records,
        "degraded_reason": "no_knowledge" if not records else None,
    }, None, model, usage, request_sent
