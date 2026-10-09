import asyncio
import time
from collections import deque
import weakref
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import re
from threading import Condition, Lock
from time import monotonic
from typing import Any

import httpx

from ..config import LLMProvider, Settings

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class _ProviderGate:
    def __init__(self, capacity: int, queue_size: int):
        self.capacity = capacity
        self.queue_size = queue_size
        self.active = 0
        self.waiting: deque[object] = deque()
        self.condition = Condition()

    def tighten(self, capacity: int, queue_size: int) -> None:
        with self.condition:
            self.capacity = min(self.capacity, capacity)
            self.queue_size = min(self.queue_size, queue_size)
            self.condition.notify_all()

    def acquire(self, timeout: float) -> bool:
        deadline = monotonic() + max(timeout, 0)
        token = object()
        with self.condition:
            if self.active < self.capacity and not self.waiting:
                self.active += 1
                return True
            if len(self.waiting) >= self.queue_size:
                return False
            self.waiting.append(token)
            while self.active >= self.capacity or self.waiting[0] is not token:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self.waiting.remove(token)
                    self.condition.notify_all()
                    return False
                self.condition.wait(remaining)
            self.waiting.popleft()
            self.active += 1
            self.condition.notify_all()
            return True

    def release(self) -> None:
        with self.condition:
            self.active -= 1
            self.condition.notify_all()


_GATE_LOCK = Lock()
_GATES: dict[str, _ProviderGate] = {}
_COOLDOWN_LOCK = Lock()
_COOLDOWNS: dict[str, tuple[float, str]] = {}


def _gate_for(provider: LLMProvider) -> _ProviderGate:
    with _GATE_LOCK:
        gate = _GATES.get(provider.quota_group)
        if gate is None:
            gate = _ProviderGate(provider.max_concurrency, provider.queue_size)
            _GATES[provider.quota_group] = gate
        else:
            gate.tighten(provider.max_concurrency, provider.queue_size)
        return gate


class _QuotaLimiter:
    """Lazy RPM/TPM token buckets shared by every provider in one quota group."""

    def __init__(self, rpm: int, tpm: int):
        self.rpm = max(0, int(rpm))
        self.tpm = max(0, int(tpm))
        self._condition = Condition()
        self._rpm_tokens = float(self.rpm)
        self._tpm_tokens = float(self.tpm)
        self._updated = monotonic()

    def tighten(self, rpm: int, tpm: int) -> None:
        with self._condition:
            # 0 表示"该维度未配置约束",不能把已配置的限额收紧成 0(0 会被 acquire 解释为不限额)。
            if rpm > 0:
                self.rpm = min(self.rpm, int(rpm)) if self.rpm else int(rpm)
                self._rpm_tokens = min(self._rpm_tokens, float(self.rpm))
            if tpm > 0:
                self.tpm = min(self.tpm, int(tpm)) if self.tpm else int(tpm)
                self._tpm_tokens = min(self._tpm_tokens, float(self.tpm))
            self._condition.notify_all()

    def _refill_locked(self) -> None:
        now = monotonic()
        elapsed = now - self._updated
        self._updated = now
        if elapsed <= 0:
            return
        if self.rpm:
            self._rpm_tokens = min(float(self.rpm), self._rpm_tokens + elapsed * self.rpm / 60)
        if self.tpm:
            self._tpm_tokens = min(float(self.tpm), self._tpm_tokens + elapsed * self.tpm / 60)

    def acquire(self, tpm_estimate: int, timeout: float) -> bool:
        deadline = monotonic() + max(timeout, 0)
        with self._condition:
            while True:
                self._refill_locked()
                need = min(tpm_estimate, self.tpm) if self.tpm else 0
                if (not self.rpm or self._rpm_tokens >= 1) and (not self.tpm or self._tpm_tokens >= need):
                    if self.rpm:
                        self._rpm_tokens -= 1
                    if self.tpm:
                        self._tpm_tokens -= need
                    return True
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                waits = [remaining]
                if self.rpm and self._rpm_tokens < 1:
                    waits.append((1 - self._rpm_tokens) * 60 / self.rpm)
                if self.tpm and self._tpm_tokens < need:
                    waits.append((need - self._tpm_tokens) * 60 / self.tpm)
                self._condition.wait(max(min(waits), 0.005))

    def settle(self, actual: int, estimate: int) -> None:
        """Reconcile the TPM bucket with the tokens actually billed."""
        if not self.tpm or actual <= 0:
            return
        with self._condition:
            self._refill_locked()
            reserved = min(estimate, self.tpm)
            self._tpm_tokens = max(
                min(float(self.tpm), self._tpm_tokens - (actual - reserved)), -float(self.tpm)
            )
            self._condition.notify_all()

    def refund(self, estimate: int) -> None:
        """Return a TPM reservation after an attempt that was not billed."""
        if not self.tpm or estimate <= 0:
            return
        with self._condition:
            self._refill_locked()
            self._tpm_tokens = min(float(self.tpm), self._tpm_tokens + min(estimate, self.tpm))
            self._condition.notify_all()


_QUOTA_LOCK = Lock()
_QUOTAS: dict[str, _QuotaLimiter] = {}


def _quota_for(provider: LLMProvider) -> _QuotaLimiter | None:
    if provider.rpm_limit <= 0 and provider.tpm_limit <= 0:
        return None
    with _QUOTA_LOCK:
        quota = _QUOTAS.get(provider.quota_group)
        if quota is None:
            quota = _QuotaLimiter(provider.rpm_limit, provider.tpm_limit)
            _QUOTAS[provider.quota_group] = quota
        else:
            quota.tighten(provider.rpm_limit, provider.tpm_limit)
        return quota


def _estimate_tokens(messages: list[dict[str, str]], output_estimate: int) -> int:
    # Rough pre-flight guess for admission only (CJK text runs near one token per
    # two characters); reconciled against real usage after every response.
    prompt = sum(len(str(message.get("content") or "")) + 8 for message in messages)
    return max(prompt // 2, 16) + max(output_estimate, 0)


def _provider_key(provider: LLMProvider) -> str:
    return f"{provider.name}\0{provider.base_url}\0{provider.model}\0{provider.channel}"


def _cooldown_key(provider: LLMProvider, reason: str | None = None) -> str:
    if reason == "rate_limited":
        return f"quota:{provider.quota_group}"
    return f"provider:{_provider_key(provider)}"


def _cooling(provider: LLMProvider) -> tuple[float, str] | None:
    with _COOLDOWN_LOCK:
        for key in (_cooldown_key(provider, "rate_limited"), _cooldown_key(provider)):
            state = _COOLDOWNS.get(key)
            if state is None:
                continue
            remaining = state[0] - monotonic()
            if remaining <= 0:
                _COOLDOWNS.pop(key, None)
                continue
            return remaining, state[1]
        return None


def _set_cooldown(provider: LLMProvider, seconds: float, reason: str) -> None:
    if seconds <= 0:
        return
    key = _cooldown_key(provider, reason)
    with _COOLDOWN_LOCK:
        until = monotonic() + min(seconds, 300)
        previous = _COOLDOWNS.get(key)
        if previous is None or until > previous[0]:
            _COOLDOWNS[key] = (until, reason)


def _clear_cooldown(provider: LLMProvider) -> None:
    with _COOLDOWN_LOCK:
        _COOLDOWNS.pop(_cooldown_key(provider), None)


def _refresh_cooldown(provider: LLMProvider) -> None:
    """成功响应只清除 provider 自身的冷却;组级 429 冷却保留至到期,遵守 Retry-After。

    旧实现无条件清空 quota:{group},使并发场景下较晚收到的成功响应
    抹掉其他 in-flight 请求刚设置的组级限流冷却,继续放大 429。
    """
    with _COOLDOWN_LOCK:
        _COOLDOWNS.pop(_cooldown_key(provider), None)


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "").strip()
    if value:
        try:
            return max(float(value), 1)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 1)
            except (TypeError, ValueError, OverflowError):
                pass
    return 2


def _status_error(response: httpx.Response) -> str:
    if response.status_code == 429:
        return "rate_limited"
    if response.status_code >= 500:
        return "provider_error"
    return "http_error"


def _status_cooldown(response: httpx.Response) -> float:
    if response.status_code == 429:
        return _retry_after(response)
    if response.status_code in {401, 403}:
        return 30
    if response.status_code >= 500:
        return 2
    return 0


def _request_id(response: httpx.Response) -> str:
    return response.headers.get("x-request-id") or response.headers.get("request-id") or "unknown"


def _token_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _response_content(data: Any) -> tuple[str | None, str | None, dict[str, int]]:
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens = {
        "prompt_tokens": _token_count(usage.get("prompt_tokens", usage.get("input_tokens", 0))) if isinstance(usage, dict) else 0,
        "completion_tokens": _token_count(usage.get("completion_tokens", usage.get("output_tokens", 0))) if isinstance(usage, dict) else 0,
    }
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return None, "invalid_response", tokens
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            part["text"] for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str):
        return None, "empty_response" if content is None else "invalid_response", tokens
    return (content, None, tokens) if content.strip() else (None, "empty_response", tokens)


def _add_usage(total: dict[str, int], current: dict[str, int]) -> None:
    for key in total:
        total[key] += current.get(key, 0)


def _json_mode_unsupported_text(buffer: str) -> bool:
    lowered = buffer[:400].lower()
    return any(term in lowered for term in ("response_format", "json_object")) and '"questions"' not in lowered


def _combined_error(errors: list[str]) -> str:
    if not errors:
        return "unavailable"
    actual_failures = [error for error in errors if error not in {"busy", "cooling_down"}]
    if actual_failures:
        return actual_failures[-1]
    if "rate_limited" in errors:
        return "rate_limited"
    if "cooling_down" in errors:
        return "cooling_down"
    if all(error == "busy" for error in errors):
        return "busy"
    return errors[-1]


_ASYNC_CLIENTS: "weakref.WeakKeyDictionary[Any, httpx.AsyncClient]" = weakref.WeakKeyDictionary()
_ASYNC_CLIENT_LOCK = Lock()

_SYNC_CLIENT: httpx.Client | None = None
_SYNC_CLIENT_LOCK = Lock()
_SYNC_CLIENT_SOURCE: Any = None  # 建缓存时的 httpx.Client 属性对象;测试 patch 后对象变化,缓存自动失效


def _shared_sync_client() -> httpx.Client:
    """进程级复用同步 Client:避免批改循环里每次尝试重建 SSL 上下文(约 0.5~2s/次)。

    单次请求超时通过 per-request 传参覆盖,不受 Client 默认超时约束。
    测试通过 patch app.services.llm_client.httpx.Client 注入 MockTransport:
    属性对象与建缓存时不一致(被替换/已恢复)即重建,保证注入的 transport 生效。
    """
    global _SYNC_CLIENT, _SYNC_CLIENT_SOURCE
    factory = httpx.Client
    with _SYNC_CLIENT_LOCK:
        if _SYNC_CLIENT is None or _SYNC_CLIENT.is_closed or _SYNC_CLIENT_SOURCE is not factory:
            _SYNC_CLIENT = factory()
            _SYNC_CLIENT_SOURCE = factory
        return _SYNC_CLIENT


def _shared_async_client() -> httpx.AsyncClient:
    """按事件循环复用 AsyncClient:避免每次请求重建 SSL 上下文(GIL 串行,约 2s/次)。"""
    loop = asyncio.get_running_loop()
    with _ASYNC_CLIENT_LOCK:
        client = _ASYNC_CLIENTS.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient()
            _ASYNC_CLIENTS[loop] = client
        return client


class LLMClient:
    """OpenAI-compatible client with shared per-quota concurrency and failover."""

    def __init__(self, settings: Settings, channel: str = "student"):
        self.settings = settings
        self.channel = channel
        self.last_error: str | None = None
        self.last_request_id: str | None = None
        self.last_model: str | None = None
        self.last_provider: str | None = None
        self.last_request_sent = False
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def _providers(self) -> tuple[LLMProvider, ...]:
        providers = list(self.settings.llm_providers)
        if not providers and self.settings.llm_base_url and self.settings.llm_api_key and self.settings.llm_model:
            providers.append(LLMProvider(
                name="legacy-shared", channel="shared", base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key, model=self.settings.llm_model,
                max_concurrency=self.settings.llm_max_concurrency,
                queue_size=self.settings.llm_queue_size, priority=100,
                quota_group=f"legacy:{self.settings.llm_base_url}",
            ))

        if self.channel == "grading":
            selected = [provider for provider in providers if provider.channel == "grading"]
            if not selected:
                selected = [provider for provider in providers if provider.channel == "student"]
        elif self.channel == "shared":
            selected = [provider for provider in providers if provider.channel == "shared"]
        else:
            selected = [provider for provider in providers if provider.channel == self.channel]
        if self.channel != "shared":
            selected.extend(provider for provider in providers if provider.channel == "shared")
        return tuple(sorted(selected, key=lambda provider: (provider.priority, provider.name)))

    @property
    def configured(self) -> bool:
        return bool(self._providers())

    @staticmethod
    def _completion_url(provider: LLMProvider) -> str:
        base_url = provider.base_url.rstrip("/")
        return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

    def _payload(
        self, provider: LLMProvider, messages: list[dict[str, str]], temperature: float, json_mode: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.settings.llm_reasoning_effort:
            payload["reasoning_effort"] = self.settings.llm_reasoning_effort
        if self.settings.llm_max_tokens > 0:
            payload["max_tokens"] = self.settings.llm_max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _json_mode_unsupported(response: httpx.Response) -> bool:
        if response.status_code not in {400, 422}:
            return False
        body = response.text.lower()
        return any(term in body for term in ("response_format", "json_object", "json mode"))

    def _reset_call(self) -> None:
        self.last_error = None
        self.last_request_id = None
        self.last_model = None
        self.last_provider = None
        self.last_request_sent = False
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def _note_http_failure(self, provider: LLMProvider, response: httpx.Response) -> str:
        error = _status_error(response)
        self.last_request_id = _request_id(response)
        self.last_model = provider.model
        retry_after = response.headers.get("retry-after", "unknown")
        logger.warning(
            "LLM provider %s returned HTTP %s (retry_after=%s, request_id=%s)",
            provider.name, response.status_code, retry_after, self.last_request_id,
        )
        _set_cooldown(provider, _status_cooldown(response), error)
        return error

    def _finish(self, errors: list[str]) -> None:
        self.last_error = _combined_error(errors)
        logger.warning(
            "LLM request exhausted provider pool (error=%s, request_id=%s)",
            self.last_error, self.last_request_id or "unknown",
        )

    def _complete_sync(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        json_mode: bool,
        structured: bool,
    ) -> str | dict[str, Any] | None:
        self._reset_call()
        providers = self._providers()
        if not providers:
            self.last_error = "not_configured"
            return None
        started = monotonic()
        queue_deadline = started + max(self.settings.llm_queue_timeout_seconds, 0)
        request_deadline = started + max(self.settings.llm_total_timeout_seconds, 0)
        errors = []
        waited_for_cooldown = False
        for provider in providers[: self.settings.llm_max_attempts]:
            cooldown = _cooling(provider)
            if cooldown and not waited_for_cooldown:
                # 限流冷却通常只有几秒:等待一次而不是立刻放弃,把冷却窗口内的请求救回来。
                budget = min(cooldown[0], 60.0, request_deadline - monotonic())
                if budget > 0.05:
                    logger.info("LLM channel cooling %.1fs; waiting once", cooldown[0])
                    time.sleep(budget)
                    waited_for_cooldown = True
                    cooldown = _cooling(provider)
            if cooldown:
                errors.append("cooling_down")
                continue
            gate = _gate_for(provider)
            quota = _quota_for(provider)
            tpm_estimate = 0
            quota_reserved = False
            if monotonic() >= request_deadline:
                errors.append("timeout")
                break
            gate_timeout = max(min(queue_deadline, request_deadline) - monotonic(), 0)
            if not gate.acquire(gate_timeout):
                errors.append("busy")
                continue
            try:
                cooldown = _cooling(provider)
                if cooldown:
                    errors.append("cooling_down")
                    continue
                if quota is not None:
                    tpm_estimate = _estimate_tokens(messages, self.settings.llm_token_output_estimate)
                    quota_timeout = max(min(queue_deadline, request_deadline) - monotonic(), 0)
                    if not quota.acquire(tpm_estimate, quota_timeout):
                        tpm_estimate = 0
                        errors.append("rate_limited")
                        logger.warning("LLM provider %s local quota (rpm/tpm) exhausted; failing over", provider.name)
                        continue
                    quota_reserved = True
                payload = self._payload(provider, messages, temperature, json_mode)
                try:
                    remaining = request_deadline - monotonic()
                    if remaining <= 0:
                        raise httpx.TimeoutException("LLM total timeout exceeded")
                    timeout = min(self.settings.llm_timeout_seconds, remaining)
                    client = _shared_sync_client()
                    if True:
                        self.last_request_sent = True
                        response = client.post(
                            self._completion_url(provider),
                            headers={"Authorization": f"Bearer {provider.api_key}"},
                            json=payload,
                            timeout=timeout,
                        )
                        if json_mode and self._json_mode_unsupported(response):
                            logger.info("LLM provider %s does not support response_format; retrying without it", provider.name)
                            payload.pop("response_format", None)
                            remaining = request_deadline - monotonic()
                            if remaining <= 0:
                                raise httpx.TimeoutException("LLM total timeout exceeded")
                            timeout = min(self.settings.llm_timeout_seconds, remaining)
                            self.last_request_sent = True
                            response = client.post(
                                self._completion_url(provider),
                                headers={"Authorization": f"Bearer {provider.api_key}"},
                                json=payload,
                                timeout=timeout,
                            )
                except httpx.TimeoutException:
                    self.last_model = provider.model
                    self.last_request_id = None
                    errors.append("timeout")
                    logger.warning("LLM provider %s timed out", provider.name)
                    continue
                except httpx.HTTPError as error:
                    self.last_model = provider.model
                    errors.append("transport_error")
                    logger.warning("LLM provider %s transport failed (%s)", provider.name, type(error).__name__)
                    continue

                self.last_request_id = _request_id(response)
                self.last_model = provider.model
                if monotonic() > request_deadline:
                    errors.append("timeout")
                    continue
                if response.status_code >= 400:
                    errors.append(self._note_http_failure(provider, response))
                    continue
                _refresh_cooldown(provider)
                try:
                    data = response.json()
                except (ValueError, TypeError):
                    errors.append("invalid_response")
                    logger.warning("LLM provider %s returned non-JSON (request_id=%s)", provider.name, self.last_request_id)
                    continue
                text, error, usage = _response_content(data)
                _add_usage(self.last_usage, usage)
                if quota is not None:
                    billed = usage["prompt_tokens"] + usage["completion_tokens"]
                    if billed:
                        quota.settle(billed, tpm_estimate)
                    else:
                        quota.refund(tpm_estimate)
                    tpm_estimate = 0
                    quota_reserved = False
                if error:
                    errors.append(error)
                    logger.warning(
                        "LLM provider %s response was unusable (%s, request_id=%s)",
                        provider.name, error, self.last_request_id,
                    )
                    continue
                if structured:
                    result = _extract_json(text or "")
                    if result is None:
                        errors.append("invalid_json")
                        logger.warning("LLM provider %s returned invalid JSON (request_id=%s)", provider.name, self.last_request_id)
                        continue
                    self.last_provider = provider.name
                    return result
                self.last_provider = provider.name
                return text
            finally:
                if quota is not None and quota_reserved and tpm_estimate:
                    quota.refund(tpm_estimate)
                gate.release()
        self._finish(errors)
        return None

    async def _acquire_async(self, gate: _ProviderGate, timeout: float) -> bool:
        task = asyncio.create_task(asyncio.to_thread(gate.acquire, timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            def release_if_acquired(done: asyncio.Task) -> None:
                try:
                    if done.result():
                        gate.release()
                except Exception:
                    pass
            task.add_done_callback(release_if_acquired)
            raise

    async def _acquire_quota_async(self, quota: _QuotaLimiter, tpm_estimate: int, timeout: float) -> bool:
        task = asyncio.create_task(asyncio.to_thread(quota.acquire, tpm_estimate, timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            def refund_if_acquired(done: asyncio.Task) -> None:
                try:
                    if done.result():
                        quota.refund(tpm_estimate)
                except Exception:
                    pass
            task.add_done_callback(refund_if_acquired)
            raise

    async def _complete_async(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        json_mode: bool,
        structured: bool,
    ) -> str | dict[str, Any] | None:
        self._reset_call()
        providers = self._providers()
        if not providers:
            self.last_error = "not_configured"
            return None
        started = monotonic()
        queue_deadline = started + max(self.settings.llm_queue_timeout_seconds, 0)
        request_deadline = started + max(self.settings.llm_total_timeout_seconds, 0)
        errors = []
        waited_for_cooldown = False
        for provider in providers[: self.settings.llm_max_attempts]:
            cooldown = _cooling(provider)
            if cooldown and not waited_for_cooldown:
                # 限流冷却通常只有几秒:等待一次而不是立刻放弃,把冷却窗口内的请求救回来。
                budget = min(cooldown[0], 60.0, request_deadline - monotonic())
                if budget > 0.05:
                    logger.info("LLM channel cooling %.1fs; waiting once", cooldown[0])
                    await asyncio.sleep(budget)
                    waited_for_cooldown = True
                    cooldown = _cooling(provider)
            if cooldown:
                errors.append("cooling_down")
                continue
            gate = _gate_for(provider)
            quota = _quota_for(provider)
            tpm_estimate = 0
            quota_reserved = False
            if monotonic() >= request_deadline:
                errors.append("timeout")
                break
            gate_timeout = max(min(queue_deadline, request_deadline) - monotonic(), 0)
            if not await self._acquire_async(gate, gate_timeout):
                errors.append("busy")
                continue
            try:
                cooldown = _cooling(provider)
                if cooldown:
                    errors.append("cooling_down")
                    continue
                if quota is not None:
                    tpm_estimate = _estimate_tokens(messages, self.settings.llm_token_output_estimate)
                    quota_timeout = max(min(queue_deadline, request_deadline) - monotonic(), 0)
                    if not await self._acquire_quota_async(quota, tpm_estimate, quota_timeout):
                        tpm_estimate = 0
                        errors.append("rate_limited")
                        logger.warning("LLM provider %s local quota (rpm/tpm) exhausted; failing over", provider.name)
                        continue
                    quota_reserved = True
                payload = self._payload(provider, messages, temperature, json_mode)
                try:
                    remaining = request_deadline - monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    timeout = min(self.settings.llm_timeout_seconds, remaining)
                    client = _shared_async_client()
                    if True:
                        self.last_request_sent = True
                        response = await asyncio.wait_for(
                            client.post(
                                self._completion_url(provider),
                                headers={"Authorization": f"Bearer {provider.api_key}"},
                                json=payload,
                                timeout=timeout,
                            ),
                            timeout=remaining,
                        )
                        if json_mode and self._json_mode_unsupported(response):
                            logger.info("LLM provider %s does not support response_format; retrying without it", provider.name)
                            payload.pop("response_format", None)
                            remaining = request_deadline - monotonic()
                            if remaining <= 0:
                                raise asyncio.TimeoutError
                            timeout = min(self.settings.llm_timeout_seconds, remaining)
                            self.last_request_sent = True
                            response = await asyncio.wait_for(
                                client.post(
                                    self._completion_url(provider),
                                    headers={"Authorization": f"Bearer {provider.api_key}"},
                                    json=payload,
                                    timeout=timeout,
                                ),
                                timeout=remaining,
                            )
                except (httpx.TimeoutException, asyncio.TimeoutError):
                    self.last_model = provider.model
                    self.last_request_id = None
                    errors.append("timeout")
                    logger.warning("LLM provider %s timed out", provider.name)
                    continue
                except httpx.HTTPError as error:
                    self.last_model = provider.model
                    errors.append("transport_error")
                    logger.warning("LLM provider %s transport failed (%s)", provider.name, type(error).__name__)
                    continue

                self.last_request_id = _request_id(response)
                self.last_model = provider.model
                if monotonic() > request_deadline:
                    errors.append("timeout")
                    continue
                if response.status_code >= 400:
                    errors.append(self._note_http_failure(provider, response))
                    continue
                _refresh_cooldown(provider)
                try:
                    data = response.json()
                except (ValueError, TypeError):
                    errors.append("invalid_response")
                    logger.warning("LLM provider %s returned non-JSON (request_id=%s)", provider.name, self.last_request_id)
                    continue
                text, error, usage = _response_content(data)
                _add_usage(self.last_usage, usage)
                if quota is not None:
                    billed = usage["prompt_tokens"] + usage["completion_tokens"]
                    if billed:
                        quota.settle(billed, tpm_estimate)
                    else:
                        quota.refund(tpm_estimate)
                    tpm_estimate = 0
                    quota_reserved = False
                if error:
                    errors.append(error)
                    logger.warning(
                        "LLM provider %s response was unusable (%s, request_id=%s)",
                        provider.name, error, self.last_request_id,
                    )
                    continue
                if structured:
                    result = _extract_json(text or "")
                    if result is None:
                        errors.append("invalid_json")
                        logger.warning("LLM provider %s returned invalid JSON (request_id=%s)", provider.name, self.last_request_id)
                        continue
                    self.last_provider = provider.name
                    return result
                self.last_provider = provider.name
                return text
            finally:
                if quota is not None and quota_reserved and tpm_estimate:
                    quota.refund(tpm_estimate)
                gate.release()
        self._finish(errors)
        return None

    def complete(
        self, messages: list[dict[str, str]], temperature: float = 0.3, json_mode: bool = False
    ) -> str | None:
        result = self._complete_sync(messages, temperature, json_mode, structured=False)
        return result if isinstance(result, str) else None

    async def complete_async(
        self, messages: list[dict[str, str]], temperature: float = 0.3, json_mode: bool = False
    ) -> str | None:
        result = await self._complete_async(messages, temperature, json_mode, structured=False)
        return result if isinstance(result, str) else None

    def complete_json(self, messages: list[dict[str, str]], temperature: float = 0.2) -> dict[str, Any] | None:
        result = self._complete_sync(messages, temperature, True, structured=True)
        return result if isinstance(result, dict) else None

    async def stream_json_async(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        on_delta=None,
    ) -> dict[str, Any] | None:
        """流式 JSON 补全:逐 chunk 回调 on_delta(累计文本),完成后返回解析对象。

        与 complete_json_async 共用通道选择/闸门/限流/冷却逻辑;失败时 last_error
        语义一致,调用方可回退非流式。
        """
        self._reset_call()
        providers = self._providers()
        if not providers:
            self.last_error = "not_configured"
            return None
        started = monotonic()
        queue_deadline = started + max(self.settings.llm_queue_timeout_seconds, 0)
        request_deadline = started + max(self.settings.llm_total_timeout_seconds, 0)
        errors = []
        waited_for_cooldown = False
        for provider in providers[: self.settings.llm_max_attempts]:
            cooldown = _cooling(provider)
            if cooldown and not waited_for_cooldown:
                budget = min(cooldown[0], 60.0, request_deadline - monotonic())
                if budget > 0.05:
                    logger.info("LLM channel cooling %.1fs; waiting once", cooldown[0])
                    await asyncio.sleep(budget)
                    waited_for_cooldown = True
                    cooldown = _cooling(provider)
            if cooldown:
                errors.append("cooling_down")
                continue
            gate = _gate_for(provider)
            quota = _quota_for(provider)
            tpm_estimate = 0
            if monotonic() >= request_deadline:
                errors.append("timeout")
                break
            gate_timeout = max(min(queue_deadline, request_deadline) - monotonic(), 0)
            if not await self._acquire_async(gate, gate_timeout):
                errors.append("busy")
                continue
            response = None
            quota_reserved = False
            settled = False
            try:
                if quota is not None:
                    tpm_estimate = _estimate_tokens(messages, self.settings.llm_token_output_estimate)
                    quota_timeout = max(min(queue_deadline, request_deadline) - monotonic(), 0)
                    if not await self._acquire_quota_async(quota, tpm_estimate, quota_timeout):
                        tpm_estimate = 0
                        errors.append("rate_limited")
                        continue
                    quota_reserved = True
                payload = self._payload(provider, messages, temperature, True)
                payload["stream"] = True
                remaining = request_deadline - monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                # 单次读超时必须进 build_request:send(stream=True) 不接受 timeout 参数,
                # 不传则整个流式请求落在 httpx 默认 5 秒上,中继偶发卡顿会被误判 transport_error。
                timeout = min(self.settings.llm_timeout_seconds, remaining)
                client = _shared_async_client()
                self.last_request_sent = True
                request = client.build_request(
                    "POST", self._completion_url(provider),
                    headers={"Authorization": f"Bearer {provider.api_key}"},
                    json=payload,
                    timeout=timeout,
                )
                response = await asyncio.wait_for(client.send(request, stream=True), timeout=remaining)
                if response.status_code >= 400:
                    error = self._note_http_failure(provider, response)
                    errors.append(error)
                    continue
                buffer = ""
                usage_acc: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
                async for line in response.aiter_lines():
                    if monotonic() > request_deadline:
                        raise asyncio.TimeoutError
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    usage_chunk = chunk.get("usage")
                    if isinstance(usage_chunk, dict):
                        _add_usage(usage_acc, {
                            "prompt_tokens": _token_count(usage_chunk.get("prompt_tokens", 0)),
                            "completion_tokens": _token_count(usage_chunk.get("completion_tokens", 0)),
                        })
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        buffer += piece
                        if on_delta:
                            try:
                                on_delta(buffer)
                            except Exception:
                                pass
                self.last_usage = usage_acc
                self.last_model = provider.model
                self.last_request_id = "stream"
                if _json_mode_unsupported_text(buffer):
                    errors.append("http_error")
                    logger.warning("LLM provider %s stream rejected response_format", provider.name)
                    continue
                result = _extract_json(buffer)
                if result is None:
                    errors.append("invalid_json")
                    logger.warning("LLM provider %s stream produced invalid JSON", provider.name)
                    continue
                _refresh_cooldown(provider)
                if quota is not None:
                    billed = usage_acc["prompt_tokens"] + usage_acc["completion_tokens"]
                    if billed:
                        quota.settle(billed, tpm_estimate)
                    else:
                        quota.refund(tpm_estimate)
                    settled = True
                self.last_provider = provider.name
                return result
            except (httpx.TimeoutException, asyncio.TimeoutError):
                self.last_model = provider.model
                errors.append("timeout")
                continue
            except httpx.HTTPError as error:
                self.last_model = provider.model
                errors.append("transport_error")
                continue
            finally:
                if response is not None:
                    try:
                        await response.aclose()
                    except Exception:
                        pass
                # 只有确实预占过且未结算的尝试才退款;失败/取消路径同样适用。
                if quota is not None and quota_reserved and not settled and tpm_estimate:
                    quota.refund(tpm_estimate)
                gate.release()
        self._finish(errors)
        return None

    async def complete_json_async(
        self, messages: list[dict[str, str]], temperature: float = 0.2
    ) -> dict[str, Any] | None:
        result = await self._complete_async(messages, temperature, True, structured=True)
        return result if isinstance(result, dict) else None
