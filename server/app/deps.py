from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from .models import User
from .store import Store


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_current_user(request: Request, store: Store = Depends(get_store)) -> User:
    sid = request.cookies.get("sid")
    session = store.get_session(sid or "")
    user = store.users.get(session[0] if session else "")
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "unauthorized", "message": "请先登录"})
    return user


def require_roles(*roles: str) -> Callable:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "forbidden", "message": "当前角色无权执行此操作"})
        return user

    return dependency
