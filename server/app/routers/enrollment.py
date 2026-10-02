from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import get_store, require_roles
from ..models import User
from ..schemas.api import (
    ApproveRegistrationRequest, PendingRegistrationOut,
    RejectRegistrationRequest, UserOut,
)
from ..store import Store
from .helpers import user_out

router = APIRouter(prefix="/api/enrollment", tags=["enrollment"])


@router.get("/pending", response_model=list[PendingRegistrationOut])
def pending(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> list[PendingRegistrationOut]:
    rows = store.pending_registrations_for(user)
    courses = store.courses
    return [
        PendingRegistrationOut(
            id=item.id, username=item.username, display_name=item.display_name,
            phone=item.phone, status=item.status,
            student_number=item.student_number,
            requested_course_id=item.requested_course_id,
            requested_course_name=courses[item.requested_course_id].name if item.requested_course_id in courses else "",
            requested_class_name=item.requested_class_name,
            requested_at=item.requested_at,
        )
        for item in rows
    ]


@router.post("/{user_id}/approve", response_model=UserOut)
def approve(
    user_id: str,
    payload: ApproveRegistrationRequest | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> UserOut:
    target = store.users.get(user_id)
    if target is None or target.status != "pending":
        raise HTTPException(status_code=404, detail={"code": "registration_not_found", "message": "该注册申请不存在或已处理"})
    if not store.can_manage_registration(user, target):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权审核该课程的注册申请"})
    class_name = payload.class_name if payload else None
    approved = store.approve_registration(user_id, class_name)
    if approved is None:
        raise HTTPException(status_code=409, detail={"code": "registration_conflict", "message": "该申请已被处理"})
    store.audit("register_approved", user.id, {"user_id": user_id, "course_id": approved.requested_course_id})
    return user_out(approved)


@router.post("/{user_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject(
    user_id: str,
    payload: RejectRegistrationRequest | None = None,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> None:
    target = store.users.get(user_id)
    if target is None or target.status != "pending":
        raise HTTPException(status_code=404, detail={"code": "registration_not_found", "message": "该注册申请不存在或已处理"})
    if not store.can_manage_registration(user, target):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权审核该课程的注册申请"})
    reason = (payload.reason if payload else "") or ""
    store.reject_registration(user_id)
    store.audit("register_rejected", user.id, {"user_id": user_id, "reason": reason})
