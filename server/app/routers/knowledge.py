from fastapi import APIRouter, Depends

from ..deps import get_store, require_roles
from ..models import User
from ..store import Store

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/status")
def status(user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> dict:
    info = store.knowledge.stats()
    info["source"] = "local-bm25"
    return info
