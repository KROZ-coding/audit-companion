"""Send minimal real completions against the configured LLM channels.

Usage (from the server directory):
    .\\.venv\\Scripts\\python.exe verify_llm_channels.py
    .\\.venv\\Scripts\\python.exe verify_llm_channels.py --channel student
    # Failover drill: sabotage the primary key, then confirm the pool lands on the backup.
    .\\.venv\\Scripts\\python.exe verify_llm_channels.py --pool student --set-env LLM_STUDENT_PRIMARY_KEY=sk-invalid

Each check sends one tiny real completion and therefore consumes a small amount of quota.
"""

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROBE_MESSAGES = [{"role": "user", "content": "请只回复两个字：正常"}]


def verify_provider(settings, provider) -> tuple[bool, str]:
    from app.services.llm_client import LLMClient

    client = LLMClient(replace(settings, llm_providers=(provider,)), channel=provider.channel)
    started = time.monotonic()
    text = client.complete(PROBE_MESSAGES, temperature=0)
    latency = time.monotonic() - started
    detail = f"model={provider.model} endpoint={provider.base_url} {latency:.1f}s"
    if text:
        return True, f"[{provider.name}] OK ({detail}) reply={text.strip()[:40]!r}"
    return False, f"[{provider.name}] FAIL error={client.last_error} ({detail})"


def verify_pool(settings, channel: str) -> tuple[bool, str]:
    from app.services.llm_client import LLMClient

    client = LLMClient(settings, channel=channel)
    if not client.configured:
        return False, f"[pool:{channel}] FAIL error=not_configured"
    started = time.monotonic()
    text = client.complete(PROBE_MESSAGES, temperature=0)
    latency = time.monotonic() - started
    detail = f"provider={client.last_provider} model={client.last_model} {latency:.1f}s"
    if text:
        return True, f"[pool:{channel}] OK ({detail}) reply={text.strip()[:40]!r}"
    return False, f"[pool:{channel}] FAIL error={client.last_error} ({detail})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the configured LLM channels with one real completion each.")
    parser.add_argument("--channel", help="only verify providers of this channel (student/teacher/grading/shared)")
    parser.add_argument(
        "--pool", metavar="CHANNEL",
        help="send one request through the full channel pool instead of isolating each provider",
    )
    parser.add_argument(
        "--set-env", action="append", default=[], metavar="KEY=VALUE",
        help="override an environment variable before settings load (failover drills)",
    )
    args = parser.parse_args()
    for item in args.set_env:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--set-env expects KEY=VALUE, got {item!r}")
        os.environ[key.strip()] = value.strip()

    from app.config import settings

    if args.pool:
        ok, line = verify_pool(settings, args.pool)
        print(line)
        return 0 if ok else 1

    providers = [
        provider for provider in settings.llm_providers
        if not args.channel or provider.channel == args.channel
    ]
    if not providers:
        print("没有可验证的通道：请配置 LLM_PROVIDERS_JSON（或指定 --channel/--pool）")
        return 1
    failures = 0
    for provider in providers:
        ok, line = verify_provider(settings, provider)
        print(line)
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
