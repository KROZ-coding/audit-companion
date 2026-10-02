import json
from uuid import uuid4
from pathlib import Path
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse

from ..deps import get_store, require_roles
from ..models import Course, User
from ..schemas.api import AdminPasswordResetRequest, AdminUserCreate, AdminUserStatusUpdate, CourseCreate, CourseOut, CourseUpdate, ExcelScheduleUpdate, ExcelSnapshotRequest, HistoryCleanupRequest, UserOut
from ..store import Store
from ..services.excel_export import cleanup_counts, cleanup_history, save_snapshot, snapshot_path
from ..utils.security import hash_password
from .helpers import user_out

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
def list_users(user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> list[UserOut]:
    return [user_out(item) for item in store.users.values()]


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: AdminUserCreate, user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> UserOut:
    username = payload.username.strip()
    if not username or not payload.display_name.strip():
        raise HTTPException(status_code=422, detail={"code": "invalid_user", "message": "用户名和显示名称不能为空"})
    if store.find_user_by_username(username):
        raise HTTPException(status_code=409, detail={"code": "username_exists", "message": "用户名已存在"})
    student_number = payload.student_number.strip()
    if payload.role == "student" and student_number and any(item.student_number == student_number for item in store.users.values()):
        raise HTTPException(status_code=409, detail={"code": "student_number_taken", "message": "该学号已被使用"})
    created = User(
        id=str(uuid4()), username=username,
        display_name=payload.display_name.strip(), password_hash=hash_password(payload.password), role=payload.role,
        student_number=student_number,
    )
    store.users[created.id] = created
    store.audit("user_create", user.id, {"created_user_id": created.id})
    return user_out(created)


@router.patch("/users/{user_id}/status", response_model=UserOut)
def update_user_status(
    user_id: str,
    payload: AdminUserStatusUpdate,
    user: User = Depends(require_roles("admin")),
    store: Store = Depends(get_store),
) -> UserOut:
    target = store.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "用户不存在"})
    if target.status == "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "pending_registration", "message": "该账号是待审核的注册申请，请在「注册审核」中通过或驳回"},
        )
    if target.id == user.id and payload.status == "disabled":
        raise HTTPException(status_code=409, detail={"code": "self_disable_forbidden", "message": "不能禁用当前管理员"})
    if target.status == payload.status:
        return user_out(target)
    target.status = payload.status
    if payload.status == "disabled":
        store.delete_user_sessions(target.id)
    store.audit("user_status_update", user.id, {"target_user_id": user_id, "status": payload.status})
    return user_out(target)


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_password(
    user_id: str,
    payload: AdminPasswordResetRequest,
    user: User = Depends(require_roles("admin")),
    store: Store = Depends(get_store),
) -> None:
    target = store.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "用户不存在"})
    if target.id == user.id:
        raise HTTPException(status_code=409, detail={"code": "self_reset_forbidden", "message": "请使用已登录账号的修改密码功能"})
    target.password_hash = hash_password(payload.new_password)
    store.delete_user_sessions(target.id)
    store.audit("admin_password_reset", user.id, {"target_user_id": target.id})


@router.get("/courses", response_model=list[CourseOut])
def list_courses(user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> list[CourseOut]:
    return [_course_out(course) for course in store.courses.values()]


@router.post("/courses", response_model=CourseOut, status_code=201)
def create_course(payload: CourseCreate, user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> CourseOut:
    if payload.id in store.courses:
        raise HTTPException(status_code=409, detail={"code": "course_exists", "message": "课程编号已存在"})
    if not payload.name.strip() or not payload.term.strip():
        raise HTTPException(status_code=422, detail={"code": "invalid_course", "message": "课程名称和学期不能为空"})
    _check_teacher(payload.teacher_id, store)
    course = Course(
        payload.id, payload.name.strip(), payload.term.strip(), payload.teacher_id,
        class_code=store.new_class_code(),
    )
    store.courses[course.id] = course
    store.audit("course_create", user.id, {"course_id": course.id})
    return _course_out(course)


@router.put("/courses/{course_id}", response_model=CourseOut)
def update_course(course_id: str, payload: CourseUpdate, user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> CourseOut:
    course = store.courses.get(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not payload.name.strip() or not payload.term.strip():
        raise HTTPException(status_code=422, detail={"code": "invalid_course", "message": "课程名称和学期不能为空"})
    _check_teacher(payload.teacher_id, store)
    course.name = payload.name.strip()
    course.term = payload.term.strip()
    course.teacher_id = payload.teacher_id
    store.audit("course_update", user.id, {"course_id": course_id})
    return _course_out(course)


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(course_id: str, user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> None:
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if any(key[0] == course_id for key in store.enrollments):
        raise HTTPException(status_code=409, detail={"code": "course_has_students", "message": "课程仍有学生，不能删除"})
    if any(question.course_id == course_id for question in store.questions.values()):
        raise HTTPException(status_code=409, detail={"code": "course_has_questions", "message": "课程仍有题目，不能删除"})
    if any(session.course_id == course_id for session in store.quiz_sessions.values()):
        raise HTTPException(status_code=409, detail={"code": "course_has_quizzes", "message": "课程仍有测验记录，不能删除"})
    del store.courses[course_id]
    store.audit("course_delete", user.id, {"course_id": course_id})


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, user: User = Depends(require_roles("admin")), store: Store = Depends(get_store)) -> None:
    target = store.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "用户不存在"})
    if target.id == user.id:
        raise HTTPException(status_code=409, detail={"code": "self_delete_forbidden", "message": "不能删除当前管理员"})
    if target.role == "teacher" and any(course.teacher_id == target.id for course in store.courses.values()):
        raise HTTPException(status_code=409, detail={"code": "teacher_has_courses", "message": "该教师仍负责课程，不能直接删除"})
    files_to_delete: list[Path] = []
    with store.lock:
        del store.users[user_id]
        store.enrollments = {
            key: class_name for key, class_name in store.enrollments.items()
            if key[1] != user_id
        }
        session_ids = {
            session_id for session_id, session in store.quiz_sessions.items()
            if session.user_id == user_id
        }
        for session_id in session_ids:
            del store.quiz_sessions[session_id]
        store.results = {
            result_id: result for result_id, result in store.results.items()
            if result.session_id not in session_ids
        }
        store.chat_history[:] = [item for item in store.chat_history if item.get("user_id") != user_id]
        store.roll_calls[:] = [item for item in store.roll_calls if item.get("student_id") != user_id]
        store.chat_quota.pop(user_id, None)
        store.usage_logs[:] = [item for item in store.usage_logs if item.get("user_id") != user_id]
        store.mastery = {
            key: value for key, value in store.mastery.items() if key[0] != user_id
        }
        store.reports.pop(user_id, None)
        store.excel_schedules.pop(user_id, None)
        owned_snapshots = [item for item in store.excel_snapshots if item.get("owner_id") == user_id]
        for snapshot in owned_snapshots:
            file_key = snapshot.get("file_key")
            if file_key and store._safe_relative_path(file_key):
                files_to_delete.append(Path(store.settings.data_dir) / file_key)
        store.excel_snapshots[:] = [item for item in store.excel_snapshots if item.get("owner_id") != user_id]
        store.import_previews = {
            preview_id: preview for preview_id, preview in store.import_previews.items()
            if preview.get("user_id") != user_id
        }
        for question in store.questions.values():
            if question.created_by == user_id:
                question.created_by = None
            if question.reviewed_by == user_id:
                question.reviewed_by = None
        removed_nodes = {
            node_id for node_id, node in store.graph_nodes.items()
            if node.get("created_by") == user_id
        }
        for node_id in removed_nodes:
            del store.graph_nodes[node_id]
        for node in store.graph_nodes.values():
            if node.get("map_from") in removed_nodes:
                node["map_from"] = None
        for document_id, document in list(store.documents.items()):
            if document.get("uploaded_by") == user_id:
                if document.get("file_key"):
                    files_to_delete.append(Path(store.settings.data_dir) / document["file_key"])
                del store.documents[document_id]
    for path in files_to_delete:
        path.unlink(missing_ok=True)
    store.delete_user_sessions(user_id)
    store.clear_login_failures(target.username)
    store.audit("user_delete", user.id, {"deleted_user_id": user_id})


@router.get("/audit-logs")
def audit_logs(
    action: str | None = None, actor: str | None = None,
    user: User = Depends(require_roles("admin")), store: Store = Depends(get_store),
) -> list[dict]:
    return [
        item for item in store.audit_logs
        if (action is None or item["action"] == action) and (actor is None or item["actor_id"] == actor)
    ]


@router.get("/backup")
def backup(
    format: str = Query(default="json", pattern="^(json|archive)$"),
    user: User = Depends(require_roles("admin")),
    store: Store = Depends(get_store),
) -> Response:
    store.audit("backup_export", user.id)
    if not store.save():
        raise HTTPException(status_code=503, detail={"code": "persistence_failed", "message": "备份审计记录保存失败"})
    if format == "archive":
        return Response(
            content=store.export_archive(),
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=audit-companion-backup.zip",
                "Cache-Control": "no-store",
            },
        )
    return Response(
        content=store.export_json(),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=audit-companion-backup.json",
            "Cache-Control": "no-store",
        },
    )


@router.post("/excel/snapshot")
def excel_snapshot(
    payload: ExcelSnapshotRequest,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> Response:
    _excel_course_scope(user, store, payload.course_id)
    try:
        snapshot, content = save_snapshot(store, user, payload.course_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"code": "snapshot_failed", "message": "Excel 快照保存失败"}) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{snapshot["filename"]}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/excel/snapshots")
def excel_snapshots(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> list[dict]:
    return [
        {key: item.get(key) for key in ("id", "filename", "created_at", "bytes", "course_ids")}
        for item in reversed(store.excel_snapshots)
        if user.role == "admin" or item.get("owner_id") == user.id
    ]


@router.get("/excel/snapshots/{snapshot_id}/download")
def download_excel_snapshot(
    snapshot_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> FileResponse:
    item = next((row for row in store.excel_snapshots if row.get("id") == snapshot_id), None)
    if item is None or (user.role != "admin" and item.get("owner_id") != user.id):
        raise HTTPException(status_code=404, detail={"code": "snapshot_not_found", "message": "快照不存在"})
    try:
        path = snapshot_path(store, item)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "snapshot_not_found", "message": "快照路径无效"}) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "snapshot_file_missing", "message": "快照文件不存在"})
    return FileResponse(
        path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=item.get("filename") or "audit_records.xlsx",
    )


@router.get("/excel/schedule")
def get_excel_schedule(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> dict:
    schedule = store.excel_schedules.get(user.id, {"enabled": False, "interval_hours": 24, "next_at": None})
    last = next((item["created_at"] for item in store.excel_snapshots if item.get("owner_id") == user.id), None)
    return {"enabled": schedule.get("enabled", False), "interval_hours": schedule.get("interval_hours", 24), "last_saved_at": schedule.get("last_saved_at") or last, "next_at": schedule.get("next_at")}


@router.put("/excel/schedule")
def update_excel_schedule(
    payload: ExcelScheduleUpdate,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    if payload.enabled and not store.persistence_enabled:
        raise HTTPException(status_code=409, detail={"code": "persistence_required", "message": "定期保存需要启用 JSON 持久化"})
    now = datetime.now(timezone.utc)
    previous = store.excel_schedules.get(user.id, {})
    schedule = {
        "enabled": payload.enabled,
        "interval_hours": payload.interval_hours,
        "last_saved_at": previous.get("last_saved_at"),
        "next_at": (now + timedelta(hours=payload.interval_hours)).isoformat() if payload.enabled else None,
    }
    store.excel_schedules[user.id] = schedule
    store.audit("excel_schedule_update", user.id, {"enabled": payload.enabled, "interval_hours": payload.interval_hours})
    return schedule


@router.get("/history/cleanup-preview")
def preview_history_cleanup(
    before: datetime | None = None,
    all_records: bool = False,
    categories: list[str] = Query(min_length=1),
    course_id: str | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    if all_records:
        before = datetime.now(timezone.utc) + timedelta(seconds=1)
    elif before is None or before.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "timezone_required", "message": "请选择有效截止时间"})
    elif before > datetime.now(timezone.utc) + timedelta(days=1):
        raise HTTPException(status_code=422, detail={"code": "invalid_cutoff", "message": "清理截止时间必须早于当前时间"})
    selected = set(categories)
    if not selected or len(selected) != len(categories) or selected.difference({"quiz", "chat", "classroom", "usage"}):
        raise HTTPException(status_code=422, detail={"code": "invalid_category", "message": "清理类型无效"})
    course_ids = _excel_course_scope(user, store, course_id)
    counts = cleanup_counts(store, course_ids, before, selected)
    return {"before": before.isoformat(), "all_records": all_records, "course_id": course_id, "counts": counts, "total": sum(counts.values())}


@router.post("/history/cleanup")
def clean_history(
    payload: HistoryCleanupRequest,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    if not store.persistence_enabled:
        raise HTTPException(status_code=409, detail={"code": "persistence_required", "message": "清理历史需要启用持久化存储"})
    if not payload.confirmed:
        raise HTTPException(status_code=422, detail={"code": "confirmation_required", "message": "请先预览并确认清理范围"})
    before = datetime.now(timezone.utc) + timedelta(seconds=1) if payload.all_records else payload.before
    if before is None or (not payload.all_records and before > datetime.now(timezone.utc) + timedelta(days=1)):
        raise HTTPException(status_code=422, detail={"code": "invalid_cutoff", "message": "清理截止时间必须早于当前时间"})
    course_ids = _excel_course_scope(user, store, payload.course_id)
    categories = set(payload.categories)
    counts = cleanup_counts(store, course_ids, before, categories)
    if not sum(counts.values()):
        return {"counts": counts, "total": 0, "snapshot_id": None}
    try:
        snapshot, _ = save_snapshot(store, user, payload.course_id)
    except (OSError, ValueError, PermissionError) as exc:
        raise HTTPException(status_code=503, detail={"code": "snapshot_required", "message": "清理前的 Excel 快照未能保存，未执行清理"}) from exc
    removed = cleanup_history(store, user, course_ids, before, categories)
    return {"counts": removed, "total": sum(removed.values()), "snapshot_id": snapshot["id"]}


@router.post("/restore")
async def restore(
    file: UploadFile = File(...),
    user: User = Depends(require_roles("admin")),
    store: Store = Depends(get_store),
) -> dict:
    content = await file.read(store.settings.max_backup_bytes + 1)
    if len(content) > store.settings.max_backup_bytes:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "备份文件不能超过 700MB"})
    try:
        if content.startswith(b"PK"):
            store.restore_archive(content)
        else:
            store.restore_bytes(content)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_backup", "message": "备份文件无效"}) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"code": "restore_persist_failed", "message": "备份恢复保存失败"}) from exc
    store.audit("backup_restore", user.id, {"filename": file.filename or "backup.json"})
    return {"status": "restored", "message": "备份已恢复，当前登录会话将在下一次请求时重新校验"}


def _check_teacher(teacher_id: str, store: Store) -> None:
    teacher = store.users.get(teacher_id)
    if teacher is None or teacher.status != "active":
        raise HTTPException(status_code=404, detail={"code": "teacher_not_found", "message": "教师不存在或已禁用"})
    if teacher.role != "teacher":
        raise HTTPException(status_code=422, detail={"code": "not_teacher", "message": "课程负责人必须是教师"})


def _excel_course_scope(user: User, store: Store, course_id: str | None) -> set[str]:
    allowed = {course.id for course in store.courses_for_user(user)}
    if course_id is None:
        return allowed
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if course_id not in allowed:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权操作该课程数据"})
    return {course_id}


def _course_out(course: Course) -> CourseOut:
    return CourseOut(
        id=course.id, name=course.name, term=course.term,
        teacher_id=course.teacher_id, class_code=course.class_code,
    )
