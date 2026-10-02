from typing import Any
from uuid import uuid4
import math

from ..config import Settings
from ..models import GradingResult, QuizSession
from .llm_client import LLMClient


def _same_multi(left: Any, right: Any) -> bool:
    if not isinstance(left, list) or not isinstance(right, list):
        return False
    if any(type(item) is not int for item in left + right):
        return False
    return sorted(left) == sorted(right)


def _same_fill(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not left.strip():
        return False
    answers = right if isinstance(right, list) else [right]
    return any(isinstance(item, str) and left.strip().casefold() == item.strip().casefold() for item in answers)


def _rubric_grade(answer: Any, rubric: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]], bool]:
    if not isinstance(answer, str) or not answer.strip() or not rubric:
        return 0.0, [], False
    answer_lower = answer.casefold()
    per_point: list[dict[str, Any]] = []
    for entry in rubric:
        point = str(entry.get("point", "")).strip()
        try:
            max_score = max(float(entry.get("score", 0)), 0.0)
        except (TypeError, ValueError):
            max_score = 0.0
        matched = bool(point) and point.casefold() in answer_lower
        per_point.append({
            "point": point, "score": max_score if matched else 0.0,
            "max_score": max_score, "matched": matched,
        })
    return sum(item["score"] for item in per_point), per_point, bool(per_point) and all(item["matched"] for item in per_point)


def _llm_rubric_grade(
    client: LLMClient,
    question: dict[str, Any],
    answer: str,
    rubric: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]], str] | None:
    points = [
        {"point": str(entry.get("point", "")).strip(), "max_score": float(entry.get("score", 0) or 0)}
        for entry in rubric
    ]
    if not points or not answer.strip():
        return None
    prompt = (
        "你是审计学课程的阅卷教师。请只依据给定评分要点给学生答案打分，不得增加要点、不得放宽标准。\n"
        "输出 JSON：{\"points\":[{\"point\":\"要点原文\",\"score\":数字,\"matched\":true/false}],\"why\":\"简短评语\"}。\n"
        "每个 score 不得超过该要点 max_score；答对给满分，答错给 0，部分正确按比例给分。\n\n"
        f"题目：{question.get('stem', '')}\n"
        f"参考答案：{question.get('reference_answer', '')}\n"
        f"评分要点：{points}\n"
        f"学生答案：{answer}"
    )
    result = client.complete_json([{"role": "user", "content": prompt}])
    if result is None:
        return None
    graded = result.get("points")
    if not isinstance(graded, list) or len(graded) != len(points):
        return None
    per_point: list[dict[str, Any]] = []
    for expected, entry in zip(points, graded):
        if not isinstance(entry, dict):
            return None
        try:
            score = min(max(float(entry.get("score", 0)), 0.0), expected["max_score"])
        except (TypeError, ValueError):
            return None
        matched = bool(entry.get("matched"))
        per_point.append({
            "point": expected["point"], "score": score,
            "max_score": expected["max_score"], "matched": matched,
        })
    why = str(result.get("why") or "模型按评分要点批改")
    return sum(item["score"] for item in per_point), per_point, why[:200]


def grade_session(session: QuizSession, answers: dict[str, Any], settings: Settings | None = None) -> GradingResult:
    client = LLMClient(settings, channel="grading") if settings is not None else None
    items: list[dict[str, Any]] = []
    total = 0.0
    max_total = 0.0
    needs_review = False
    llm_used = False
    for question in session.questions:
        question_id = question["id"]
        max_score = float(question.get("score", 10))
        max_total += max_score
        answer = answers.get(question_id)
        qtype = question["type"]
        if qtype == "single_choice":
            ok = type(answer) is int and type(question.get("answer")) is int and answer == question.get("answer")
            score = max_score if ok else 0.0
            method = "rule"
            why = "答案匹配正确" if ok else "答案与参考答案不一致"
        elif qtype == "multi_choice":
            ok = _same_multi(answer, question.get("answer"))
            score = max_score if ok else 0.0
            method = "rule"
            why = "选项集合匹配正确" if ok else "选项集合与参考答案不一致"
        elif qtype == "judge":
            ok = type(answer) is bool and type(question.get("answer")) is bool and answer == question.get("answer")
            score = max_score if ok else 0.0
            method = "rule"
            why = "判断正确" if ok else "判断与参考答案不一致"
        elif qtype == "fill":
            ok = _same_fill(answer, question.get("answer"))
            score = max_score if ok else 0.0
            method = "rule"
            why = "填空答案匹配正确" if ok else "填空答案与参考答案不一致"
        else:
            graded = None
            if client is not None and client.configured and isinstance(answer, str) and answer.strip():
                graded = _llm_rubric_grade(client, question, answer, question.get("rubric", []))
            if graded is not None:
                score, per_point, why = graded
                method = "manual_pending"
                llm_used = True
                needs_review = True
            else:
                score, per_point, _ = _rubric_grade(answer, question.get("rubric", []))
                method = "manual_pending"
                why = "规则初评仅供参考，等待教师复核"
                needs_review = True
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            score = min(max(score, 0.0), max_score) if math.isfinite(score) else 0.0
            remaining = max_score
            for point in per_point:
                point_score = point.get("score", 0)
                try:
                    point_score = float(point_score)
                except (TypeError, ValueError):
                    point_score = 0.0
                point["score"] = min(max(point_score, 0.0), remaining) if math.isfinite(point_score) else 0.0
                remaining = max(0.0, remaining - point["score"])
        total += score
        items.append({
            "question_id": question_id, "score": score, "max_score": max_score,
            "method": method, "why": why,
            "knowledge_points": question.get("knowledge_points", []),
            "per_point": per_point if qtype in {"short_answer", "case"} else [],
        })
    return GradingResult(
        id=str(uuid4()), session_id=session.id, per_question=items,
        total=total, max_total=max_total,
        status="needs_review" if needs_review else "graded",
        submitted_answers=answers,
        graded_by="llm" if llm_used else "rule",
    )


def apply_grading_result(store: Any, session: QuizSession, result: GradingResult) -> None:
    session.status = "needs_review" if result.status == "needs_review" else "graded"
    points: dict[str, list[float]] = {}
    for item in result.per_question:
        if item["method"] == "manual_pending":
            continue
        rate = item["score"] / item["max_score"] if item["max_score"] else 0
        for point in item["knowledge_points"]:
            points.setdefault(point, []).append(rate)
    for point, rates in points.items():
        store.update_mastery(session.user_id, session.course_id, point, sum(rates) / len(rates))
