import asyncio
from concurrent.futures import ThreadPoolExecutor
import secrets
from threading import BoundedSemaphore

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from ..config import settings

_hasher = PasswordHasher()
_hash_slots = BoundedSemaphore(settings.password_hash_concurrency)
_hash_executor = ThreadPoolExecutor(
    max_workers=settings.password_hash_concurrency, thread_name_prefix="password-hash"
)


def hash_password(password: str) -> str:
    with _hash_slots:
        return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        with _hash_slots:
            return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


async def verify_password_async(password: str, password_hash: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_hash_executor, verify_password, password, password_hash)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)
