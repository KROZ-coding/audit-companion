import csv
import io
import json
import posixpath
from typing import Literal
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree as ET
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import ValidationError

from ..deps import get_store, require_roles
from ..models import Question, User
from ..schemas.api import (
    AiQuestionGenerateRequest, AiQuestionGenerateResponse,
    QuestionCreate, QuestionDetailOut, QuestionOut, ReviewRequest,
)
from ..services.llm_client import LLMClient
from ..store import Store
from .helpers import question_out

router = APIRouter(prefix="/api/bank", tags=["bank"])


@router.get("", response_model=list[QuestionOut])
def list_questions(
    question_status: Literal["draft", "reviewing", "published", "retired", "rejected"] | None = Query(default=None, alias="status"),
    course_id: str | None = None,
    question_type: Literal["single_choice", "multi_choice", "judge", "fill", "short_answer", "case"] | None = Query(default=None, alias="type"),
    difficulty: Literal["easy", "medium", "hard"] | None = None,
    chapter: str | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> list[QuestionOut]:
    if user.role == "teacher" and course_id is not None and not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程题库"})
    return [
        question_out(q) for q in store.questions.values()
        if (question_status is None or q.status == question_status)
        and (course_id is None or q.course_id == course_id)
        and (question_type is None or q.type == question_type)
        and (difficulty is None or q.difficulty == difficulty)
        and (chapter is None or q.chapter == chapter)
        and (user.role == "admin" or q.course_id is None or store.can_access_course(user, q.course_id, teaching=True))
    ]


@router.get("/knowledge-points")
def knowledge_points(
    course_id: str | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict[str, list[str]]:
    if course_id is not None:
        if course_id not in store.courses:
            raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
        if not store.can_access_course(user, course_id, teaching=True):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程知识点"})
    points = {
        point.strip()
        for question in store.questions.values()
        if question.status == "published"
        and (course_id is None or question.course_id == course_id)
        for point in question.knowledge_points
        if point.strip()
    }
    points.update(
        node["name"] for node in store.graph_nodes.values()
        if node.get("graph") == "knowledge" and node.get("name")
    )
    return {"knowledge_points": sorted(points)}


@router.post("/generate", response_model=AiQuestionGenerateResponse, status_code=201)
def generate_questions(
    payload: AiQuestionGenerateRequest,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> AiQuestionGenerateResponse:
    if payload.course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, payload.course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权向该课程题库出题"})
    llm = LLMClient(store.settings, channel="teacher")
    if not llm.configured:
        raise HTTPException(status_code=503, detail={"code": "llm_not_configured", "message": "请先在 server/.env 配置大模型"})

    selected_points = [point.strip() for point in payload.knowledge_points if point.strip()]
    available_points = {
        point.strip()
        for question in store.questions.values()
        if question.status == "published"
        and (question.course_id is None or question.course_id == payload.course_id)
        for point in question.knowledge_points
        if point.strip()
    }
    available_points.update(
        node["name"] for node in store.graph_nodes.values()
        if node.get("graph") == "knowledge" and node.get("name")
    )
    invalid_points = [point for point in selected_points if point not in available_points]
    if invalid_points:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_knowledge_point", "message": f"知识点不存在：{'、'.join(invalid_points)}"},
        )
    records = store.knowledge.search(" ".join(selected_points), top_k=8)
    if not records:
        raise HTTPException(status_code=409, detail={"code": "knowledge_not_found", "message": "本地知识库没有检索到所选知识点，请换一个知识点"})
    context = "\n\n".join(
        f"[来源：{record['name']}]\n{record['text']}" for record in records
    )
    prompt = (
        "你是高校审计学题库教师。请严格依据给定课程资料，为指定知识点生成题目。"
        "只能输出 JSON，不要输出 Markdown 代码围栏。格式为："
        "{\"questions\":[{\"stem\":\"题干\",\"options\":[\"选项\"],"
        "\"answer\":0,\"reference_answer\":\"参考答案\","
        "\"rubric\":[{\"point\":\"评分点\",\"score\":5}],"
        "\"explanation\":\"解析\"}]}。"
        "single_choice/multi_choice 的 answer 使用从 0 开始的选项下标；"
        "judge 使用 true/false；fill 使用字符串或字符串数组；"
        "short_answer/case 必须有 reference_answer 和 rubric。"
        f"一次生成 {payload.count} 道 {payload.question_type}，难度为 {payload.difficulty}。"
        f"知识点只能围绕：{selected_points}。\n\n课程资料：\n{context}"
    )
    generated = llm.complete_json([{"role": "user", "content": prompt}], temperature=0.4)
    raw_questions = generated.get("questions") if isinstance(generated, dict) else None
    if not isinstance(raw_questions, list) or not raw_questions:
        raise HTTPException(status_code=502, detail={"code": "llm_invalid_output", "message": "模型没有返回合法题目，请重试"})

    created: list[QuestionOut] = []
    source = f"AI生成 · 本地知识库 · {records[0]['name']}"
    for item in raw_questions[:payload.count]:
        if not isinstance(item, dict):
            continue
        try:
            answer = _normalize_answer(payload.question_type, item.get("answer"), item.get("options") or [])
            reference_answer = str(item.get("reference_answer") or item.get("explanation") or "").strip()
            rubric = item.get("rubric") if isinstance(item.get("rubric"), list) else []
            question_payload = QuestionCreate(
                type=payload.question_type,
                stem=str(item.get("stem") or ""),
                options=[str(option).strip() for option in item.get("options") or []],
                answer=answer,
                reference_answer=reference_answer,
                rubric=rubric,
                knowledge_points=selected_points,
                difficulty=payload.difficulty,
                course_id=payload.course_id,
                chapter=None,
                source=source,
            )
        except (TypeError, ValueError):
            continue
        question = Question(
            id=str(uuid4()), code=f"AI-{str(uuid4())[:8].upper()}",
            type=question_payload.type, stem=question_payload.stem,
            options=question_payload.options, answer=question_payload.answer,
            reference_answer=question_payload.reference_answer, rubric=question_payload.rubric,
            knowledge_points=question_payload.knowledge_points, difficulty=question_payload.difficulty,
            status="draft", course_id=question_payload.course_id, created_by=user.id,
            source=source,
        )
        store.questions[question.id] = question
        created.append(question_out(question))

    if not created:
        raise HTTPException(status_code=502, detail={"code": "llm_invalid_output", "message": "模型题目未通过校验，请重试"})
    store.audit("question_ai_generate", user.id, {
        "course_id": payload.course_id, "count": len(created),
        "knowledge_points": selected_points,
    })
    return AiQuestionGenerateResponse(
        status="draft", source_count=len(records),
        questions=[item.model_dump() for item in created],
    )


def _normalize_answer(question_type: str, answer: object, options: list[object]) -> object:
    def index(value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("invalid choice answer")
        if isinstance(value, int):
            return value
        text = str(value).strip().upper().rstrip(".").rstrip("．")
        if len(text) == 1 and "A" <= text <= "Z":
            return ord(text) - ord("A")
        raise ValueError("choice answer must be an index or letter")

    if question_type == "single_choice":
        return index(answer)
    if question_type == "multi_choice":
        if not isinstance(answer, list) or not answer:
            raise ValueError("multi answer must be a list")
        values = [index(item) for item in answer]
        if len(values) != len(set(values)):
            raise ValueError("duplicate multi answer")
        return values
    if question_type == "judge":
        if isinstance(answer, bool):
            return answer
        text = str(answer).strip().lower()
        if text in {"true", "正确", "是", "对"}:
            return True
        if text in {"false", "错误", "否", "错"}:
            return False
        raise ValueError("invalid judge answer")
    return answer


@router.post("", response_model=QuestionOut, status_code=201)
def create_question(payload: QuestionCreate, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> QuestionOut:
    if payload.course_id is not None:
        if payload.course_id not in store.courses:
            raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
        if not store.can_access_course(user, payload.course_id, teaching=True):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权向该课程题库添加题目"})
    question = Question(
        id=str(uuid4()), code=f"CPA-AUD-{str(uuid4())[:8].upper()}", type=payload.type,
        stem=payload.stem, options=payload.options, answer=payload.answer,
        reference_answer=payload.reference_answer, rubric=payload.rubric,
        knowledge_points=payload.knowledge_points, difficulty=payload.difficulty,
        status="draft", course_id=payload.course_id, chapter=payload.chapter, created_by=user.id,
        quick_response=payload.quick_response, source=payload.source,
    )
    with store.lock:
        store.add_question(question)
    store.audit("question_create", user.id, {"question_id": question.id})
    return question_out(question)


@router.get("/{question_id}/detail", response_model=QuestionDetailOut)
def question_detail(
    question_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> QuestionDetailOut:
    question = _get_question(question_id, store)
    _check_question_access(question, user, store)
    return QuestionDetailOut(
        **question_out(question).model_dump(),
        answer=question.answer,
    )


@router.post("/import")
async def import_questions(
    file: UploadFile = File(...),
    course_id: str | None = Form(default=None),
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    filename = file.filename or ""
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix not in {"csv", "xlsx"}:
        raise HTTPException(status_code=415, detail={"code": "unsupported_file", "message": "仅支持 CSV 或 XLSX"})
    if course_id is not None and not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权向该课程题库导入题目"})
    content = await file.read(store.settings.max_document_bytes + 1)
    if len(content) > store.settings.max_document_bytes:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "导入文件不能超过 50MB"})
    if suffix == "xlsx":
        try:
            parsed_rows = _xlsx_rows(content)
        except (BadZipFile, ET.ParseError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_xlsx", "message": "XLSX 文件格式无效"}) from exc
        fieldnames = list(parsed_rows[0]) if parsed_rows else []
    else:
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_encoding", "message": "CSV 必须使用 UTF-8 编码"}) from exc
        fieldnames = reader.fieldnames or []
        parsed_rows = list(reader)
    if not fieldnames or "type" not in fieldnames or "stem" not in fieldnames:
        raise HTTPException(status_code=422, detail={"code": "invalid_csv_header", "message": "CSV 至少需要 type 和 stem 列"})

    rows: list[dict] = []
    errors: list[dict] = []
    codes: set[str] = set()
    for line, raw in enumerate(parsed_rows, start=2):
        try:
            code = (raw.get("code") or f"CPA-IMPORT-{str(uuid4())[:8].upper()}").strip()
            if code in codes or store.question_code_exists(code):
                raise ValueError(f"题目编码已存在: {code}")
            row_course_id = (raw.get("course_id") or course_id or "").strip() or None
            if row_course_id is not None and not store.can_access_course(user, row_course_id, teaching=True):
                raise ValueError("无权操作该课程题目")
            payload = QuestionCreate(
                type=(raw.get("type") or "").strip(),
                stem=raw.get("stem") or "",
                options=_list_value(raw.get("options"), []),
                answer=_answer_value((raw.get("type") or "").strip(), raw.get("answer") or ""),
                reference_answer=raw.get("reference_answer") or "",
                rubric=_list_value(raw.get("rubric"), []),
                knowledge_points=_list_value(raw.get("knowledge_points"), []),
                difficulty=(raw.get("difficulty") or "medium").strip(),
                course_id=row_course_id,
                chapter=(raw.get("chapter") or "").strip() or None,
                quick_response=(raw.get("quick_response") or "").strip().lower() in {"1", "true", "yes"},
                source=raw.get("source") or "",
            )
            codes.add(code)
            rows.append({"code": code, **payload.model_dump()})
        except (ValueError, ValidationError) as exc:
            errors.append({"row": line, "message": str(exc)})

    preview_id = str(uuid4())
    store.import_previews[preview_id] = {
        "id": preview_id, "filename": filename, "user_id": user.id,
        "rows": rows, "errors": errors,
    }
    store.audit("question_import_preview", user.id, {
        "preview_id": preview_id, "filename": filename,
        "valid": len(rows), "invalid": len(errors),
    })
    return {
        "status": "preview", "preview_id": preview_id, "filename": filename,
        "valid": len(rows), "invalid": len(errors), "rows": rows, "errors": errors,
    }


@router.post("/import/{preview_id}/confirm", status_code=201)
def confirm_import(
    preview_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    preview = store.import_previews.get(preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail={"code": "import_preview_not_found", "message": "导入预览不存在"})
    if preview["user_id"] != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权确认该导入预览"})
    if not preview["rows"]:
        raise HTTPException(status_code=409, detail={"code": "empty_import", "message": "没有可导入的有效题目"})
    if any(store.question_code_exists(row["code"]) for row in preview["rows"]):
        raise HTTPException(status_code=409, detail={"code": "duplicate_question_code", "message": "题目编码已存在，请重新生成预览"})

    created: list[QuestionOut] = []
    with store.lock:
        for row in preview["rows"]:
            question = Question(
                id=str(uuid4()), code=row["code"], type=row["type"], stem=row["stem"],
                options=row["options"], answer=row["answer"], reference_answer=row["reference_answer"],
                rubric=row["rubric"], knowledge_points=row["knowledge_points"], difficulty=row["difficulty"],
                status="draft", course_id=row["course_id"], chapter=row.get("chapter"), created_by=user.id,
                quick_response=row["quick_response"], source=row["source"],
            )
            store.add_question(question)
            created.append(question_out(question))
        del store.import_previews[preview_id]
    store.audit("question_import_confirm", user.id, {"preview_id": preview_id, "count": len(created)})
    return {"status": "created", "count": len(created), "questions": [item.model_dump() for item in created]}


@router.get("/quick-question")
def quick_question(
    course_id: str | None = None,
    chapter: str | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    """Teacher classroom roll-call: a random published quick-response question with the answer."""
    import random

    if course_id is None:
        courses = store.courses_for_user(user)
        if user.role != "teacher" or len(courses) != 1:
            raise HTTPException(status_code=400, detail={"code": "course_required", "message": "请指定要抽题的课程"})
        course_id = courses[0].id
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程题目"})
    candidates = [
        q for q in store.questions.values()
        if q.status == "published"
        and (q.course_id is None or q.course_id == course_id)
        and (chapter is None or q.chapter == chapter)
    ]
    preferred = [q for q in candidates if q.quick_response] or [q for q in candidates if q.type in {"single_choice", "multi_choice", "judge", "fill"}]
    if not preferred:
        raise HTTPException(status_code=404, detail={"code": "no_quick_question", "message": "题库暂无可用于课堂点名的已发布题目"})
    question = random.choice(preferred)
    if question.type in {"single_choice", "multi_choice"} and isinstance(question.answer, (int, list)):
        indexes = question.answer if isinstance(question.answer, list) else [question.answer]
        answer_display = "；".join(question.options[i] for i in indexes if isinstance(i, int) and 0 <= i < len(question.options))
    elif question.type == "judge":
        answer_display = "正确" if question.answer is True else "错误"
    else:
        answer_display = question.reference_answer or str(question.answer or "")
    return {
        "id": question.id, "type": question.type, "stem": question.stem,
        "options": question.options, "answer_display": answer_display,
        "reference_answer": question.reference_answer, "chapter": question.chapter,
    }


@router.post("/{question_id}/submit", response_model=QuestionOut)
def submit_question(question_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> QuestionOut:
    question = _get_question(question_id, store)
    _check_question_edit_access(question, user, store)
    if question.status != "draft":
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": "只有 draft 题目可以送审"})
    question.status = "reviewing"
    question.review_comment = ""
    store.audit("question_submit", user.id, {"question_id": question_id})
    return question_out(question)


@router.post("/{question_id}/review", response_model=QuestionOut)
def review_question(question_id: str, payload: ReviewRequest, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> QuestionOut:
    question = _get_question(question_id, store)
    _check_question_edit_access(question, user, store)
    if question.status != "reviewing":
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": "只有 reviewing 题目可以审核"})
    if payload.decision == "reject" and not payload.comment.strip():
        raise HTTPException(status_code=422, detail={"code": "review_comment_required", "message": "拒绝题目必须填写理由"})
    question.status = "published" if payload.decision == "pass" else "draft"
    question.reviewed_by = user.id
    question.review_comment = payload.comment
    store.audit("question_review", user.id, {"question_id": question_id, "decision": payload.decision, "comment": payload.comment})
    return question_out(question)


@router.post("/{question_id}/retire", response_model=QuestionOut)
def retire_question(question_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> QuestionOut:
    question = _get_question(question_id, store)
    _check_question_edit_access(question, user, store)
    if question.status != "published":
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": "只有 published 题目可以下架"})
    question.status = "retired"
    store.audit("question_retire", user.id, {"question_id": question_id})
    return question_out(question)


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> None:
    question = _get_question(question_id, store)
    _check_question_edit_access(question, user, store)
    if question.status != "draft":
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": "只有 draft 题目可以直接删除"})
    with store.lock:
        del store.questions[question_id]
    store.audit("question_delete", user.id, {"question_id": question_id})


def _get_question(question_id: str, store: Store) -> Question:
    question = store.questions.get(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail={"code": "question_not_found", "message": "题目不存在"})
    return question


def _check_question_access(question: Question, user: User, store: Store) -> None:
    if user.role != "admin" and question.course_id is not None and not store.can_access_course(user, question.course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权查看该课程题目"})


def _check_question_edit_access(question: Question, user: User, store: Store) -> None:
    if user.role == "admin":
        return
    if question.course_id is None:
        if question.created_by != user.id:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "共享题目只能由创建者或管理员维护"})
    elif not store.can_access_course(user, question.course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权操作该课程题目"})


def _list_value(raw: str | None, default: list) -> list:
    if not raw or not raw.strip():
        return default
    value = json.loads(raw) if raw.lstrip().startswith(("[", "{")) else [item.strip() for item in raw.split("|")]
    if not isinstance(value, list):
        raise ValueError("字段必须是数组")
    return value


def _answer_value(question_type: str, raw: str) -> object:
    if question_type == "single_choice":
        return int(raw)
    if question_type == "multi_choice":
        values = _list_value(raw, [])
        return [int(value) for value in values]
    if question_type == "judge":
        normalized = raw.strip().lower()
        if normalized in {"true", "1", "yes", "正确", "对"}:
            return True
        if normalized in {"false", "0", "no", "错误", "错"}:
            return False
        raise ValueError("判断题答案必须是 true 或 false")
    if question_type == "fill":
        return raw.strip()
    return raw


def _xlsx_rows(content: bytes) -> list[dict[str, str]]:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
            raise ValueError("xlsx expands beyond limit")
        if any(item.file_size and item.file_size / max(item.compress_size, 1) > 1000 for item in entries):
            raise ValueError("xlsx compression ratio exceeds limit")
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relation_targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships
            if item.attrib.get("Type", "").endswith("/worksheet")
        }
        sheet = workbook.find("main:sheets/main:sheet", namespace)
        if sheet is None:
            raise ValueError("worksheet missing")
        relation_id = sheet.attrib.get(f"{{{namespace['rel']}}}id")
        target = relation_targets.get(relation_id or "")
        if not target:
            raise ValueError("worksheet relation missing")
        target = target.lstrip("/")
        worksheet_path = target if target.startswith("xl/") else posixpath.join("xl", target)
        worksheet = ET.fromstring(archive.read(worksheet_path))
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = ["".join(item.itertext()) for item in shared.findall("main:si", namespace)]

        rows: list[dict[int, str]] = []
        for row_index, row in enumerate(worksheet.findall("main:sheetData/main:row", namespace)):
            if row_index >= 10001:
                raise ValueError("xlsx contains too many rows")
            values: dict[int, str] = {}
            for fallback, cell in enumerate(row.findall("main:c", namespace)):
                reference = cell.attrib.get("r", "")
                column = _xlsx_column_index(reference) if reference else fallback
                value = cell.findtext("main:v", default="", namespaces=namespace)
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    value = "".join(cell.find("main:is", namespace).itertext()) if cell.find("main:is", namespace) is not None else ""
                elif cell_type == "s":
                    value = shared_strings[int(value)]
                elif cell_type == "b":
                    value = "true" if value == "1" else "false"
                values[column] = value
            if values:
                rows.append(values)
    if not rows:
        return []
    headers = {column: value.strip() for column, value in rows[0].items() if value.strip()}
    if not headers or len(headers) > 32 or len(headers) != len(set(headers.values())):
        raise ValueError("invalid headers")
    return [
        {header: values.get(column, "") for column, header in headers.items()}
        for values in rows[1:]
    ]


def _xlsx_column_index(reference: str) -> int:
    letters = ""
    for character in reference:
        if not character.isalpha():
            break
        letters += character.upper()
    if not letters:
        raise ValueError("invalid cell reference")
    index = 0
    for character in letters:
        index = index * 26 + ord(character) - ord("A") + 1
    return index - 1
