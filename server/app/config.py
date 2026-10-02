from dataclasses import dataclass, field
import json
import os
from pathlib import Path


def _load_env_file() -> None:
    """Load server/.env into os.environ without overriding existing variables.

    Set SKIP_ENV_FILE=1 (tests do) to ignore the file entirely.
    """
    if os.getenv("SKIP_ENV_FILE"):
        return
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_env_file()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _cookie_secure_default() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() not in {"development", "test"}


def _course_dataset_ids() -> tuple[tuple[str, tuple[str, ...]], ...]:
    raw = os.getenv("MAXKB_COURSE_DATASETS", "").strip()
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, dict):
        return ()
    return tuple(
        (str(course_id), tuple(item for item in dataset_ids if isinstance(item, str) and item.strip()))
        for course_id, dataset_ids in value.items()
        if isinstance(dataset_ids, list) and dataset_ids
    )


@dataclass(frozen=True, slots=True)
class LLMProvider:
    name: str
    channel: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    max_concurrency: int
    queue_size: int
    priority: int
    quota_group: str
    rpm_limit: int = 0
    tpm_limit: int = 0


def _llm_providers() -> tuple[LLMProvider, ...]:
    raw = os.getenv("LLM_PROVIDERS_JSON", "").strip()
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("LLM_PROVIDERS_JSON must be valid JSON") from error
    if not isinstance(values, list):
        raise ValueError("LLM_PROVIDERS_JSON must be a JSON array")
    providers = []
    names = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"LLM_PROVIDERS_JSON entry {index} must be an object")
        name = str(value.get("name") or "").strip()
        channel = str(value.get("channel") or "").strip().lower()
        base_url = str(value.get("base_url") or "").strip().rstrip("/")
        model = str(value.get("model") or "").strip()
        key_env = str(value.get("api_key_env") or "").strip()
        if not name or name in names or channel not in {"student", "teacher", "grading", "shared"}:
            raise ValueError(f"LLM_PROVIDERS_JSON entry {index} has an invalid name or channel")
        if not base_url or not model or not key_env.isidentifier():
            raise ValueError(f"LLM_PROVIDERS_JSON entry {index} requires base_url, model and api_key_env")
        api_key = os.getenv(key_env, "")
        if not api_key:
            raise ValueError(f"LLM provider {name!r} is missing environment variable {key_env}")
        try:
            concurrency = int(value.get("max_concurrency", os.getenv("LLM_MAX_CONCURRENCY", "32")))
            queue_size = int(value.get("queue_size", os.getenv("LLM_QUEUE_SIZE", "60")))
            priority = int(value.get("priority", 100))
            rpm_limit = int(value.get("rpm_limit", os.getenv("LLM_RPM_LIMIT", "0")))
            tpm_limit = int(value.get("tpm_limit", os.getenv("LLM_TPM_LIMIT", "0")))
        except (TypeError, ValueError) as error:
            raise ValueError(f"LLM provider {name!r} has invalid numeric limits") from error
        if not 1 <= concurrency <= 256 or not 0 <= queue_size <= 1000:
            raise ValueError(f"LLM provider {name!r} limits are outside supported bounds")
        if not 0 <= rpm_limit <= 1_000_000 or not 0 <= tpm_limit <= 10_000_000:
            raise ValueError(f"LLM provider {name!r} rate limits are outside supported bounds")
        providers.append(LLMProvider(
            name=name, channel=channel, base_url=base_url, api_key=api_key, model=model,
            max_concurrency=concurrency, queue_size=queue_size, priority=priority,
            quota_group=str(value.get("quota_group") or name).strip() or name,
            rpm_limit=rpm_limit, tpm_limit=tpm_limit,
        ))
        names.add(name)
    return tuple(providers)


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "审计智能学伴 API")
    app_env: str = os.getenv("APP_ENV", "development")
    maxkb_url: str = os.getenv("MAXKB_URL", "").rstrip("/")
    maxkb_api_key: str = os.getenv("MAXKB_API_KEY", "")
    maxkb_dataset_ids: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("MAXKB_DATASET_IDS", "").split(",")
        if item.strip()
    )
    maxkb_course_dataset_ids: tuple[tuple[str, tuple[str, ...]], ...] = _course_dataset_ids()
    maxkb_top_k: int = 4
    maxkb_timeout_seconds: float = 8.0
    data_dir: str = os.getenv("DATA_DIR", "./data")
    persistence_enabled: bool = _bool("PERSISTENCE_ENABLED", False)
    cookie_secure: bool = _bool("COOKIE_SECURE", _cookie_secure_default())
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    login_failure_limit: int = 5
    login_lock_seconds: int = 15 * 60
    registration_rate_limit: int = int(os.getenv("REGISTRATION_RATE_LIMIT", "100"))
    password_hash_concurrency: int = max(1, min(32, int(os.getenv("PASSWORD_HASH_CONCURRENCY", "8"))))
    chat_daily_limit: int = int(os.getenv("CHAT_DAILY_LIMIT", "50"))
    log_max_entries: int = int(os.getenv("LOG_MAX_ENTRIES", "5000"))
    registration_window_seconds: int = int(os.getenv("REGISTRATION_WINDOW_SECONDS", str(60 * 60)))
    cors_origins: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "CORS_ORIGINS",
            "" if os.getenv("APP_ENV", "development").strip().lower() == "production" else "http://localhost:5173",
        ).split(",")
        if item.strip()
    )
    allowed_hosts: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "ALLOWED_HOSTS",
            "" if os.getenv("APP_ENV", "development").strip().lower() == "production" else "*",
        ).split(",")
        if item.strip()
    )
    llm_base_url: str = os.getenv("LLM_BASE_URL", "").rstrip("/")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    llm_total_timeout_seconds: float = float(os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", "360"))
    llm_max_attempts: int = max(1, min(10, int(os.getenv("LLM_MAX_ATTEMPTS", "3"))))
    llm_max_concurrency: int = max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "32")))
    llm_queue_size: int = max(0, int(os.getenv("LLM_QUEUE_SIZE", "60")))
    llm_queue_timeout_seconds: float = float(os.getenv("LLM_QUEUE_TIMEOUT_SECONDS", "240"))
    llm_token_output_estimate: int = max(0, int(os.getenv("LLM_TOKEN_OUTPUT_ESTIMATE", "512")))
    llm_providers: tuple[LLMProvider, ...] = _llm_providers()
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "").strip()
    knowledge_dir: str = os.getenv("KNOWLEDGE_DIR", "")
    knowledge_index_path: str = os.getenv("KNOWLEDGE_INDEX_PATH", "")
    knowledge_top_k: int = int(os.getenv("KNOWLEDGE_TOP_K", "4"))
    max_document_bytes: int = 50 * 1024 * 1024
    max_backup_bytes: int = 700 * 1024 * 1024

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_providers or (self.llm_base_url and self.llm_api_key and self.llm_model))

    def llm_configured_for(self, channel: str) -> bool:
        if not self.llm_providers:
            return bool(self.llm_base_url and self.llm_api_key and self.llm_model)
        channels = {channel, "shared"}
        if channel == "grading":
            channels.add("student")
        return any(provider.channel in channels for provider in self.llm_providers)


settings = Settings()
