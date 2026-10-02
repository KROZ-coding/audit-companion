"""Reusable HTTP load test for the audit companion backend.

Spawns a dedicated uvicorn process on a temp DATA_DIR, seeds N student accounts,
then drives each student through real HTTP flows and reports p50/p95 latency.

Usage (from the server directory):
    .\\.venv\\Scripts\\python.exe load_test.py --users 60 --scenario mixed
    .\\.venv\\Scripts\\python.exe load_test.py --users 60 --scenario mixed --no-knowledge
    .\\.venv\\Scripts\\python.exe load_test.py --users 10 --scenario subjective --json report.json

Scenarios:
    mixed      login + real model Q&A + objective quiz (comparable with the 2026-09 baseline)
    objective  login + objective quiz only (rule grading, no model calls)
    subjective login + subjective quiz submit + grading poll (real model grading calls)
    ask        login + real model Q&A only

Real model calls are made and consume quota. Knowledge retrieval uses the real
local BM25 index unless --no-knowledge is passed.
"""

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVER_DIR = Path(__file__).resolve().parent
REAL_KNOWLEDGE_INDEX = SERVER_DIR / "data" / "knowledge" / "index.pkl"
COURSE_ID = "audit-101"
CHAT_QUESTION = "简述审计证据的充分性与适当性及两者关系。"
TERMINAL_GRADED = {"graded", "needs_review"}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


@dataclass
class PhaseStats:
    label: str
    latencies: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add(self, seconds: float, error: str | None = None) -> None:
        self.latencies.append(seconds)
        if error:
            self.failures.append(error)

    @property
    def ok(self) -> int:
        return len(self.latencies) - len(self.failures)


@dataclass
class Report:
    users: int
    scenario: str
    with_knowledge: bool
    phases: dict[str, PhaseStats] = field(default_factory=dict)
    degraded_ask: int = 0
    degraded_reasons: dict[str, int] = field(default_factory=dict)
    ask_sources_nonempty: int = 0
    graded_subjective: int = 0
    total_seconds: float = 0.0

    def line(self, label: str) -> PhaseStats:
        return self.phases.setdefault(label, PhaseStats(label))

    def summary(self) -> str:
        rows = []
        for stats in self.phases.values():
            if not stats.latencies:
                continue
            rows.append(
                f"  {stats.label:<22} n={len(stats.latencies):>3} ok={stats.ok:>3} "
                f"p50={percentile(stats.latencies, 0.5):6.1f}s "
                f"p95={percentile(stats.latencies, 0.95):6.1f}s "
                f"max={max(stats.latencies):6.1f}s"
                + (f" failures={stats.failures[:3]}" if stats.failures else "")
            )
        rows.append(f"  答疑命中资料: {self.ask_sources_nonempty}/{self.line('ask').ok or 0} · degraded: {self.degraded_ask}")
        if self.degraded_reasons:
            detail = "、".join(f"{key}={value}" for key, value in sorted(self.degraded_reasons.items()))
            rows.append(f"  degraded 原因: {detail}")
        if "subjective_submit" in self.phases or "subjective_poll" in self.phases:
            rows.append(f"  主观题完成批改: {self.graded_subjective}")
        return "\n".join(rows)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed_users(data_dir: Path, users: int) -> None:
    from app.config import Settings
    from app.store import Store

    settings = Settings(data_dir=str(data_dir), persistence_enabled=True, cookie_secure=False)
    store = Store(settings)
    for index in range(users):
        student = store.add_user(f"load{index:03d}", f"压测学生{index:03d}", f"load{index:03d}pw*", "student")
        store.enrollments[(COURSE_ID, student.id)] = "压测班"
    assert store.save(), "压测种子数据写入失败"
    print(f"已预置 {users} 个压测学生账号于 {data_dir}")


def start_server(port: int, data_dir: Path, with_knowledge: bool) -> subprocess.Popen:
    env = os.environ.copy()
    env["DATA_DIR"] = str(data_dir)
    env["PERSISTENCE_ENABLED"] = "1"
    env["APP_ENV"] = "development"
    env["COOKIE_SECURE"] = "0"
    env["KNOWLEDGE_INDEX_PATH"] = str(REAL_KNOWLEDGE_INDEX if with_knowledge else data_dir / "knowledge" / "disabled.pkl")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(SERVER_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    return process


async def wait_healthy(port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError("uvicorn 未在时限内就绪")


def random_objective_answer(question: dict) -> Any:
    if question["type"] == "single_choice":
        return random.randrange(len(question.get("options") or [0]))
    if question["type"] == "multi_choice":
        return [0]
    if question["type"] == "judge":
        return True
    return "充分性是数量要求，适当性是质量要求。"


async def run_user(client: httpx.AsyncClient, index: int, scenario: str, report: Report) -> None:
    started = time.monotonic()
    phase = report.line("login")
    begin = time.monotonic()
    try:
        response = await client.post("/api/auth/login", json={
            "username": f"load{index:03d}", "password": f"load{index:03d}pw*", "role": "student",
        })
        response.raise_for_status()
        phase.add(time.monotonic() - begin)
    except Exception as error:
        phase.add(time.monotonic() - begin, f"load{index:03d}: {error}")
        return

    if scenario in {"mixed", "ask"}:
        phase = report.line("ask")
        begin = time.monotonic()
        try:
            response = await client.post("/api/chat/ask", json={"question": CHAT_QUESTION, "course_id": COURSE_ID})
            response.raise_for_status()
            body = response.json()
            phase.add(time.monotonic() - begin)
            if body.get("sources"):
                report.ask_sources_nonempty += 1
            if body.get("degraded"):
                report.degraded_ask += 1
                reason = body.get("degraded_reason") or "unknown"
                report.degraded_reasons[reason] = report.degraded_reasons.get(reason, 0) + 1
        except Exception as error:
            phase.add(time.monotonic() - begin, f"load{index:03d}: {error}")

    if scenario in {"mixed", "objective"}:
        phase = report.line("objective_quiz")
        begin = time.monotonic()
        try:
            started_quiz = await client.post("/api/quiz/start", json={
                "course_id": COURSE_ID,
                "counts": {"single": 1, "multi": 0, "judge": 0, "fill": 0, "short": 0, "case": 0},
            })
            started_quiz.raise_for_status()
            quiz = started_quiz.json()
            question = quiz["questions"][0]
            submit = await client.post(f"/api/quiz/{quiz['id']}/submit", json={
                "answers": {question["id"]: random_objective_answer(question)},
            })
            submit.raise_for_status()
            phase.add(time.monotonic() - begin)
        except Exception as error:
            phase.add(time.monotonic() - begin, f"load{index:03d}: {error}")

    if scenario == "subjective":
        phase = report.line("subjective_submit")
        begin = time.monotonic()
        try:
            started_quiz = await client.post("/api/quiz/start", json={
                "course_id": COURSE_ID,
                "counts": {"single": 0, "multi": 0, "judge": 0, "fill": 0, "short": 1, "case": 0},
            })
            started_quiz.raise_for_status()
            quiz = started_quiz.json()
            question = quiz["questions"][0]
            submit = await client.post(f"/api/quiz/{quiz['id']}/submit", json={
                "answers": {question["id"]: "充分性是数量要求，适当性是质量要求，数量不能弥补质量缺陷。"},
            })
            submit.raise_for_status()
            result = submit.json().get("result") or {}
            session_id = quiz["id"]
            phase.add(time.monotonic() - begin)
            status = result.get("status")
            if status in TERMINAL_GRADED:
                report.graded_subjective += 1
            elif status == "grading":
                poll = report.line("subjective_poll")
                begin = time.monotonic()
                while time.monotonic() - begin < 420:
                    body = (await client.get(f"/api/grading/{session_id}/status")).raise_for_status().json()
                    if body.get("status") in TERMINAL_GRADED:
                        report.graded_subjective += 1
                        break
                    await asyncio.sleep(1.5)
                poll.add(time.monotonic() - begin)
        except Exception as error:
            phase.add(time.monotonic() - begin, f"load{index:03d}: {error}")

    report.total_seconds = max(report.total_seconds, time.monotonic() - started)
    print(".", end="", flush=True)


async def drive(base_url: str, users: int, scenario: str, report: Report) -> None:
    # 客户端必须在计时窗口外创建：每个 AsyncClient 的 SSL 上下文构建是同步 CPU 操作，
    # 60 个一起建会把事件循环卡住十几秒，污染所有延迟测量。
    clients = [
        httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(420.0, connect=10.0))
        for _ in range(users)
    ]
    try:
        await asyncio.gather(*(run_user(clients[index], index, scenario, report) for index in range(users)))
    finally:
        for client in clients:
            await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--scenario", choices=["mixed", "objective", "subjective", "ask"], default="mixed")
    parser.add_argument("--no-knowledge", action="store_true", help="disable the local BM25 index (isolate the model path)")
    parser.add_argument("--json", help="write the raw report to this path")
    parser.add_argument("--keep", action="store_true", help="keep the temp data dir for inspection")
    parser.add_argument("--attach", metavar="URL", help="attach to an already-running instance instead of spawning one (users must already exist there)")
    args = parser.parse_args()

    if args.attach:
        print(f"附着模式: {args.attach} · 场景: {args.scenario}(账号需已存在于目标实例)")
        report = Report(users=args.users, scenario=args.scenario, with_knowledge=not args.no_knowledge)
        begin = time.monotonic()
        asyncio.run(drive(args.attach.rstrip("/"), args.users, args.scenario, report))
        elapsed = time.monotonic() - begin
        print(f"\n完成,整批用时 {elapsed:.1f}s")
        print(report.summary())
        if args.json:
            payload = {"users": args.users, "scenario": args.scenario, "attach": args.attach, "elapsed_seconds": elapsed,
                       "phases": {label: {"n": len(s.latencies), "ok": s.ok, "p50": percentile(s.latencies, 0.5), "p95": percentile(s.latencies, 0.95), "max": max(s.latencies) if s.latencies else 0.0, "failures": s.failures} for label, s in report.phases.items() if s.latencies}}
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"报告已写入 {args.json}")
        return 0 if all(not s.failures for s in report.phases.values()) else 1

    data_dir = Path(tempfile.mkdtemp(prefix="audit-loadtest-"))
    port = free_port()
    print(f"压测目录: {data_dir} · 端口: {port} · 场景: {args.scenario} · 知识检索: {'开' if not args.no_knowledge else '关'}")
    report = Report(users=args.users, scenario=args.scenario, with_knowledge=not args.no_knowledge)
    process = None
    try:
        seed_users(data_dir, args.users)
        process = start_server(port, data_dir, with_knowledge=not args.no_knowledge)
        asyncio.run(wait_healthy(port))
        print(f"服务就绪，开始 {args.users} 人压测…")
        begin = time.monotonic()
        asyncio.run(drive(f"http://127.0.0.1:{port}", args.users, args.scenario, report))
        elapsed = time.monotonic() - begin
        print(f"\n完成，整批用时 {elapsed:.1f}s")
        print(report.summary())
        if args.json:
            payload = {
                "users": args.users, "scenario": args.scenario, "with_knowledge": report.with_knowledge,
                "elapsed_seconds": elapsed,
                "phases": {
                    label: {"n": len(stats.latencies), "ok": stats.ok,
                            "p50": percentile(stats.latencies, 0.5), "p95": percentile(stats.latencies, 0.95),
                            "max": max(stats.latencies) if stats.latencies else 0.0,
                            "failures": stats.failures}
                    for label, stats in report.phases.items() if stats.latencies
                },
                "ask_sources_nonempty": report.ask_sources_nonempty,
                "ask_degraded": report.degraded_ask,
                "ask_degraded_reasons": report.degraded_reasons,
                "subjective_graded": report.graded_subjective,
            }
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"报告已写入 {args.json}")
        return 0 if all(not stats.failures for stats in report.phases.values()) else 1
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
        else:
            print(f"数据目录保留: {data_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
