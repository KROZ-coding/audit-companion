from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from ..deps import get_store, require_roles
from ..schemas.api import DocumentOut
from ..store import Store
from ..models import User
from ..utils.files import validate_uploaded_content
from ..services.llm_client import LLMClient

router = APIRouter(prefix="/api/docs", tags=["docs"])
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".md")
ALLOWED_KBS = {"audit_textbook", "audit_standards", "audit_cases", "cpa_question_bank"}


@router.post("/upload", response_model=DocumentOut, status_code=202)
async def upload(
    file: UploadFile = File(...),
    target_kb: str = Form(...),
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> DocumentOut:
    filename = file.filename or ""
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=415, detail={"code": "unsupported_file", "message": "仅支持 PDF、DOCX、MD"})
    if target_kb not in ALLOWED_KBS:
        raise HTTPException(status_code=422, detail={"code": "invalid_target_kb", "message": "目标知识库无效"})
    content = await file.read(store.settings.max_document_bytes + 1)
    if len(content) > store.settings.max_document_bytes:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "资料不能超过 50MB"})
    document_id = str(uuid4())
    suffix = Path(filename).suffix.lower()
    try:
        validate_uploaded_content(suffix, content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_file_content", "message": "文件内容与扩展名不匹配"}) from exc
    file_key = f"documents/{document_id}{suffix}"
    path = Path(store.settings.data_dir) / file_key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail={"code": "storage_write_failed", "message": "资料保存失败"}) from exc
    document = {
        "id": document_id, "name": filename, "target_kb": target_kb,
        "size": len(content), "vector_status": "deferred", "file_key": file_key,
        "uploaded_by": user.id, "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    with store.lock:
        store.documents[document["id"]] = document
    store.audit("document_upload", user.id, {"document_id": document["id"], "target_kb": target_kb})
    return DocumentOut(**document)


@router.get("", response_model=list[DocumentOut])
def list_documents(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> list[DocumentOut]:
    with store.lock:
        return [
            DocumentOut(**item) for item in store.documents.values()
            if user.role == "admin" or item.get("uploaded_by") == user.id or item.get("shared")
        ]


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> None:
    document = store.documents.get(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "message": "资料不存在"})
    _check_document_access(document, user)
    with store.lock:
        document = store.documents.pop(document_id, None)
    if document is None:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "message": "资料不存在"})
    if document.get("file_key"):
        (Path(store.settings.data_dir) / document["file_key"]).unlink(missing_ok=True)
    store.audit("document_delete", user.id, {"document_id": document_id})


@router.get("/{document_id}/summary")
def document_summary(
    document_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    document = store.documents.get(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "message": "资料不存在"})
    _check_document_access(document, user)
    if document.get("summary"):
        return {"id": document_id, "summary": document["summary"], "topics": document.get("topics", [])}

    source_path = document.get("source_path") or document.get("name", "")
    chunks = [
        item["text"] for item in store.knowledge.chunks
        if item.get("src") == source_path
    ] if store.knowledge.ensure_loaded() else []
    context = "\n\n".join(chunks[:3])[:6000]
    if not context:
        context = f"资料文件：{document.get('name', '')}"

    summary = ""
    topics: list[str] = []
    llm = LLMClient(store.settings, channel="teacher")
    if llm.configured:
        result = llm.complete_json([
            {"role": "system", "content": "你是审计学资料管理员，只输出 JSON：{\"summary\":\"150字以内摘要\",\"topics\":[\"3-8个主题词\"]}。"},
            {"role": "user", "content": f"资料名：{document.get('name', '')}\n资料内容：\n{context}"},
        ]) or {}
        summary = str(result.get("summary") or "").strip()
        topics = [str(item) for item in result.get("topics") or []][:8]
    if not summary:
        summary = context.replace("\n", " ").strip()[:220] or "暂未生成摘要。"
        topics = topics or [document.get("target_kb", "课程资料")]

    document["summary"] = summary
    document["topics"] = topics
    store.audit("document_summary_generate", user.id, {"document_id": document_id, "llm": llm.configured})
    if not store.save():
        raise HTTPException(status_code=503, detail={"code": "persistence_failed", "message": "资料摘要保存失败"})
    return {"id": document_id, "summary": summary, "topics": topics}


def _check_document_access(document: dict, user: User) -> None:
    if user.role != "admin" and document.get("uploaded_by") != user.id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权操作该资料"})
