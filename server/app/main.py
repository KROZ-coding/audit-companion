from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings
from .routers import admin, auth, bank, chat, courses, docs, enrollment, grading, graph, knowledge, progress, quiz
from .store import Store
from .services.excel_export import run_snapshot_scheduler
from .utils.security import verify_password

FRONTEND_DIR = Path(__file__).resolve().parents[2]
FRONTEND_FILES = {
    "智能学伴演示页.html": "智能学伴演示页.html",
    "课程思政地图.html": "课程思政地图.html",
}
VENDOR_DIR = FRONTEND_DIR / "vendor"


def _frontend_route(file_name: str):
    def serve() -> FileResponse:
        return FileResponse(
            FRONTEND_DIR / file_name,
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    return serve


def create_app() -> FastAPI:
    production = settings.app_env.lower() == "production"
    application = FastAPI(
        title=settings.app_name, version="0.1.0", description="经管数智审计业务后端",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    application.state.store = Store(settings)
    if production:
        if not application.state.store.persistence_enabled:
            raise RuntimeError("production requires PERSISTENCE_ENABLED=true")
        if not settings.cookie_secure:
            raise RuntimeError("production requires COOKIE_SECURE=true")
        if not settings.allowed_hosts:
            raise RuntimeError("production requires ALLOWED_HOSTS")
        demo_passwords = {"admin": "admin123*", "teacher01": "teach123*", "stu001": "stu123*"}
        if any(
            (account := application.state.store.find_user_by_username(username)) is not None
            and verify_password(password, account.password_hash)
            for username, password in demo_passwords.items()
        ):
            raise RuntimeError("replace development demo passwords before production")
    application.state.excel_export_thread = None
    application.state.excel_export_stop = None

    def start_excel_scheduler() -> None:
        if not application.state.store.persistence_enabled or application.state.excel_export_thread is not None:
            return
        stop_event = Event()
        thread = Thread(
            target=run_snapshot_scheduler,
            args=(application.state.store, stop_event),
            name="excel-snapshot-scheduler",
            daemon=True,
        )
        application.state.excel_export_stop = stop_event
        application.state.excel_export_thread = thread
        thread.start()

    def stop_excel_scheduler() -> None:
        stop_event = application.state.excel_export_stop
        thread = application.state.excel_export_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=2)
        application.state.excel_export_thread = None
        application.state.excel_export_stop = None

    application.router.add_event_handler("startup", start_excel_scheduler)
    application.router.add_event_handler("shutdown", stop_excel_scheduler)
    cors_origins = list(settings.cors_origins)
    if settings.app_env.lower() in {"development", "test"}:
        cors_origins.append("null")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # SAMEORIGIN（而非 DENY）：跨站仍不能嵌套，同源的课程思政地图 iframe 可正常加载
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.app_env.lower() == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @application.middleware("http")
    async def persist_state(request: Request, call_next):
        response = await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            saved = await application.state.store.save_async()
            if not saved:
                return JSONResponse(status_code=503, content={"code": "persistence_failed", "message": "业务数据暂未可靠保存，请联系管理员"})
        return response

    @application.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            allowed_origins = set(settings.cors_origins)
            if settings.app_env.lower() in {"development", "test"}:
                allowed_origins.add("null")
            try:
                origin_allowed = bool(origin) and (
                    urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()
                    or origin in allowed_origins
                )
            except ValueError:
                origin_allowed = False
            if (fetch_site == "cross-site" and not origin_allowed) or (origin and not origin_allowed):
                return JSONResponse(status_code=403, content={"code": "cross_site_request", "message": "拒绝跨站修改请求"})
        return await call_next(request)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) and "code" in exc.detail else {
            "code": "http_error", "message": str(exc.detail),
        }
        return JSONResponse(status_code=exc.status_code, content=detail)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"code": "validation_error", "message": "请求参数校验失败", "errors": jsonable_encoder(exc.errors())})

    for router in (auth.router, chat.router, quiz.router, grading.router, progress.router, bank.router, docs.router, graph.router, knowledge.router, courses.router, enrollment.router, admin.router):
        application.include_router(router)

    if VENDOR_DIR.is_dir():
        application.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")

    @application.get("/health", tags=["system"])
    def health() -> dict:
        knowledge = application.state.store.knowledge.stats()
        def llm_channel_configured(channel: str) -> bool:
            return settings.llm_configured_for(channel)
        return {
            "status": "ok",
            "environment": settings.app_env,
            "storage": "json" if application.state.store.persistence_enabled else "memory",
            "integrations": {
                "maxkb": bool(settings.maxkb_url and application.state.store.maxkb_api_keys()),
                "llm": settings.llm_configured,
                "llm_channels": {channel: llm_channel_configured(channel) for channel in ("student", "teacher", "grading")},
            },
            "knowledge": {"loaded": knowledge["loaded"], "chunks": knowledge["chunks"], "built_at": knowledge["built_at"]},
        }

    @application.get("/ready", tags=["system"])
    def ready() -> dict:
        if settings.app_env.lower() == "production" and not application.state.store.persistence_enabled:
            raise HTTPException(status_code=503, detail={"code": "storage_not_persistent", "message": "生产环境必须启用持久化"})
        if settings.app_env.lower() == "production" and not settings.maxkb_url:
            raise HTTPException(status_code=503, detail={"code": "maxkb_not_ready", "message": "生产 MaxKB 尚未配置"})
        if settings.app_env.lower() == "production" and not application.state.store.maxkb_api_keys():
            raise HTTPException(status_code=503, detail={"code": "maxkb_key_not_ready", "message": "生产 MaxKB Key 尚未配置"})
        return {
            "status": "ready",
            "storage": "json" if application.state.store.persistence_enabled else "memory",
            "message": "本地持久化已就绪" if application.state.store.persistence_enabled else "开发环境就绪",
        }

    @application.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(
            FRONTEND_DIR / FRONTEND_FILES["智能学伴演示页.html"],
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    for url_name, file_name in FRONTEND_FILES.items():
        if url_name == "智能学伴演示页.html":
            continue

        application.add_api_route(
            f"/{url_name}", _frontend_route(file_name), methods=["GET"], include_in_schema=False
        )

    return application


app = create_app()
