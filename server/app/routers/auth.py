from datetime import datetime, timezone
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..deps import get_current_user, get_store
from ..models import User
from ..schemas.api import (
    CourseOut, LoginRequest, LoginResponse, MeResponse, PasswordChangeRequest,
    RegisterRequest, RegisterResponse,
)
from ..store import Store
from ..utils.security import hash_password, new_session_id, verify_password, verify_password_async
from .helpers import user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])

ROLE_LABELS = {"student": "学生", "teacher": "教师", "admin": "管理员"}


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, response: Response, request: Request, store: Store = Depends(get_store)) -> LoginResponse:
    username = payload.username.strip()
    client_ip = request.client.host if request.client else None
    if store.login_lock_remaining(username) > 0:
        store.audit("login_locked", None, {"username": username}, ip=client_ip)
        raise HTTPException(status_code=429, detail={"code": "login_locked", "message": "登录失败次数过多，请稍后再试"})

    user = store.find_user_by_username(username)
    password_valid = await verify_password_async(payload.password, user.password_hash) if user is not None else False
    if not password_valid:
        attempts, locked = store.record_login_failure(username)
        store.audit("login_failed", None, {
            "username": username,
            "attempts": attempts,
            "locked": locked,
        }, ip=client_ip)
        if locked:
            raise HTTPException(status_code=429, detail={"code": "login_locked", "message": "登录失败次数过多，请稍后再试"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "login_failed", "message": "账号或密码错误"})

    # 密码正确后才给出具体原因，避免泄露账号是否存在
    if user.status == "pending":
        store.audit("login_pending", user.id, {"username": username}, ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "pending_approval", "message": "注册申请正在等待教师审核，通过后即可登录"},
        )
    if user.status != "active":
        store.audit("login_disabled", user.id, {"username": username}, ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_disabled", "message": "账号已被禁用，请联系管理员"},
        )
    if payload.role is not None and payload.role != user.role:
        store.audit("login_role_mismatch", user.id, {"username": username, "selected": payload.role}, ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "role_mismatch",
                "message": f"该账号是{ROLE_LABELS.get(user.role, user.role)}账号，请在上方选择「{ROLE_LABELS.get(user.role, user.role)}」身份后再登录",
            },
        )

    store.clear_login_failures(username)
    sid = new_session_id()
    store.create_session(sid, user.id, time.time() + store.settings.session_ttl_seconds)
    user.last_login_at = datetime.now(timezone.utc)
    store.audit("login_success", user.id, ip=client_ip)
    response.set_cookie(
        "sid", sid, httponly=True, secure=store.settings.cookie_secure,
        samesite="lax", max_age=store.settings.session_ttl_seconds,
    )
    return LoginResponse(user=user_out(user))


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_202_ACCEPTED)
def register(payload: RegisterRequest, request: Request, store: Store = Depends(get_store)) -> RegisterResponse:
    client_ip = request.client.host if request.client else None
    allowed, retry_after = store.registration_allowed(client_ip or "")
    if not allowed:
        store.audit("register_rate_limited", None, {"username": payload.username}, ip=client_ip)
        raise HTTPException(
            status_code=429,
            detail={"code": "register_rate_limited", "message": f"注册过于频繁，请 {max(retry_after // 60, 1)} 分钟后再试"},
        )

    username = payload.username.strip()
    student_number = payload.student_number.strip()
    with store.lock:
        duplicate = (
            store.find_user_by_username(username) is not None
            or store.find_user_by_phone(payload.phone) is not None
            or bool(student_number and any(item.student_number.casefold() == student_number.casefold() for item in store.users.values()))
        )
        if duplicate:
            store.audit("register_duplicate", None, {"username": username}, ip=client_ip)
            raise HTTPException(status_code=409, detail={"code": "registration_conflict", "message": "账号或注册信息已使用，请联系任课教师核对"})

        course = store.find_course_by_class_code(payload.class_code)
        if course is None:
            store.audit("register_bad_class_code", None, {"username": username}, ip=client_ip)
            raise HTTPException(status_code=404, detail={"code": "class_code_not_found", "message": "课堂代码无效，请向任课教师确认"})

        display_name = payload.display_name.strip() or username
        user = store.register_student(
            username, display_name, student_number, payload.password, payload.phone, course,
        )
    store.audit(
        "register_submitted", user.id,
        {"username": username, "course_id": course.id, "phone_tail": payload.phone[-4:]},
        ip=client_ip,
    )
    return RegisterResponse(
        status="pending",
        message=f"注册申请已提交，等待「{course.name}」任课教师审核",
        course_name=course.name,
        course_id=course.id,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, request: Request, store: Store = Depends(get_store)) -> None:
    sid = request.cookies.get("sid")
    session = store.pop_session(sid or "")
    if session:
        purged = store.purge_practices(session[0])
        if purged:
            store.audit("practice_purge_on_logout", session[0], {"deleted": purged})
    store.audit("logout", session[0] if session else None, ip=request.client.host if request.client else None)
    response.delete_cookie("sid")


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    store: Store = Depends(get_store),
) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "invalid_password", "message": "当前密码错误"})
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=422, detail={"code": "same_password", "message": "新密码不能与当前密码相同"})
    user.password_hash = hash_password(payload.new_password)
    store.delete_user_sessions(user.id)
    store.audit("password_change", user.id)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> MeResponse:
    return MeResponse(
        **user_out(user).model_dump(),
        courses=[
            CourseOut(
                id=course.id, name=course.name, term=course.term,
                teacher_id=course.teacher_id, class_code=course.class_code,
            )
            for course in store.courses_for_user(user)
        ],
    )
