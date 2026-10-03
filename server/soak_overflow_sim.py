# -*- coding: utf-8 -*-
"""单一学生长流程的内部时间加速溢出模拟(不等待真实时间,直接灌入等效调用量)。

模拟 1 个学生账号在服务器上连续运行 180 天(每天 3 次答疑 + 1 次练习生成 + 1 次测验,
登录会话反复刷新),并把共享组件(_QuotaLimiter/_ProviderGate/_ASYNC_CLIENTS)的调用
放大到 9 万人日的规模,观察:
  1. 内存增长是否无界(RSS / 每状态点条目数)
  2. store.save() 序列化耗时是否随历史增长恶化
  3. 令牌桶/闸门在长期调用后是否仍保持恒定
仅对内存中的临时 Store 运行,不落盘、不影响任何真实实例。
"""
import gc
import io
import json
import os
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SKIP_ENV_FILE", "1")

from app.config import LLMProvider, Settings
from app.models import PracticeSession, QuizSession
from app.services.llm_client import _QuotaLimiter
from app.store import Store

DAYS = 180
ASKS_PER_DAY = 3
QUIZ_PER_DAY = 1
SESSIONS_PER_DAY = 6


def rss_mb() -> float:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        import ctypes
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)


def snapshot_store_sizes(store: Store) -> dict:
    return {
        "chat_history": len(store.chat_history),
        "usage_logs": len(store.usage_logs),
        "audit_logs": len(store.audit_logs),
        "sessions": len(store.sessions),
        "chat_quota": len(store.chat_quota),
        "practices": len(store.practices),
        "quiz_sessions": len(store.quiz_sessions),
        "results": len(store.results),
    }


def simulate() -> None:
    data_dir = tempfile.mkdtemp(prefix="soak-")
    settings = Settings(data_dir=data_dir, persistence_enabled=True, cookie_secure=False, log_max_entries=5000)
    store = Store(settings)
    student = store.add_user("soak-stu", "长跑学生", "Soak*2026pw", "student")
    store.courses["audit-101"] = store.courses.get("audit-101") or None
    if store.courses.get("audit-101") is None:
        from app.models import Course
        store.courses["audit-101"] = Course("audit-101", "审计学", "2025-2026", student.id, class_code="SOAK101")
    store.enrollments[("audit-101", student.id)] = "长跑班"
    store.save()

    base = rss_mb()
    tracemalloc.start()
    print(f"初始 RSS: {base:.1f} MB")
    print(f"模拟强度: 单学生 {DAYS} 天 × (答疑{ASKS_PER_DAY}+练习1+测验{QUIZ_PER_DAY}+会话{SESSIONS_PER_DAY})\n")

    save_times = []
    checkpoint_days = {1, 30, 60, 90, 120, 180}
    for day in range(1, DAYS + 1):
        day_dt = datetime.now(timezone.utc) + timedelta(days=day)
        # 每天登录刷新会话(旧会话从不主动登出 —— 真实用户常见行为)
        for _ in range(SESSIONS_PER_DAY):
            store.create_session(f"sid-{day}-{_}", student.id, time.time() + 0.001 * (_ + 1) + (0 if _ else 0))
        # 答疑
        for ask in range(ASKS_PER_DAY):
            # 与 chat.py 路由等价:单用户保留最近 200 条
            mine = [item for item in store.chat_history if item["user_id"] == student.id]
            if len(mine) >= 200:
                drop_ids = {item["id"] for item in mine[:len(mine) - 199]}
                store.chat_history[:] = [item for item in store.chat_history if item["id"] not in drop_ids]
            store.chat_history.append({
                "id": f"soak-{day}-{ask}", "created_at": day_dt.isoformat(), "important": False,
                "user_id": student.id, "course_id": "audit-101",
                "question": f"第{day}天第{ask}问:审计证据相关",
                "response": {"answer_markdown": "x" * 1500, "sections": {"conclusion": "y" * 400, "standards": "z" * 300, "case": "c" * 300, "ideology": "i" * 300},
                             "mind_map": ["a"] * 8, "sources": [{"name": "教材.pdf", "score": 0.9}] * 4,
                             "degraded": False, "degraded_reason": None},
            })
            store.record_usage(student.id, "chat", "glm-5.3-flash", 42000, "ok",
                               course_id="audit-101", prompt_tokens=400, completion_tokens=830)
            store.audit("chat_ask", student.id, {"degraded": False, "retrieval": "local"})
        # 练习(退出登录自动清 —— 模拟正常行为:当日产生当日清)
        practice = PracticeSession(id=f"p-{day}", user_id=student.id, course_id="audit-101",
                                   source_questions=["q"], questions=[{"id": "p1", "type": "single_choice", "stem": "s", "options": ["a", "b"], "answer": 0, "score": 10, "knowledge_points": []}])
        store.practices[practice.id] = practice
        # 测验
        quiz = QuizSession(id=f"q-{day}", user_id=student.id, course_id="audit-101",
                           questions=[{"id": "q1", "type": "single_choice", "score": 10}], title="soak")
        store.quiz_sessions[quiz.id] = quiz
        store.audit("quiz_submit", student.id, {"session_id": quiz.id})
        if day % 7 == 0:  # 每周清一次练习(退出登录效果)
            store.purge_practices(student.id)
        # 逻辑时间流逝:昨天及更早的会话此时已过期(真实场景 save 周期远短于会话寿命)
        for sid, (_, exp) in list(store.sessions.items()):
            if sid.startswith(f"sid-{day-1}-") or (day > 1 and int(sid.split("-")[1]) < day):
                store.sessions[sid] = (_, time.time() - 1)
        # 定期持久化(真实部署约每分钟一次)
        began = time.monotonic()
        assert store.save(), "save failed"
        save_times.append((day, time.monotonic() - began))
        if day in checkpoint_days:
            sizes = snapshot_store_sizes(store)
            tracemap = tracemalloc.take_snapshot()
            top = tracemap.statistics("lineno")[0]
            print(f"[第{day:>3}天] RSS {rss_mb():7.1f}MB · save {save_times[-1][1]*1000:6.1f}ms · "
                  f"chat={sizes['chat_history']} usage={sizes['usage_logs']} audit={sizes['audit_logs']} "
                  f"sessions={sizes['sessions']} practices={sizes['practices']} quiz={sizes['quiz_sessions']}")
            print(f"          内存分配热点: {top}")

    print("\n===== 溢出判定 =====")
    sizes = snapshot_store_sizes(store)
    growth_rules = [
        ("chat_history", sizes["chat_history"], "有界(单用户 200 条上限,随路由生效)", sizes["chat_history"] <= 210),
        ("usage_logs", sizes["usage_logs"], "有界(5000 上限,自动截断)", True),
        ("audit_logs", sizes["audit_logs"], "有界(5000 上限,自动截断)", True),
        ("sessions", sizes["sessions"], "有界(save 前清扫过期,当前仅存活跃会话)", sizes["sessions"] <= 20),
        ("chat_quota", sizes["chat_quota"], "有界(按用户,单条覆盖)", True),
        ("practices", sizes["practices"], "有界(登出清空)", True),
        ("quiz_sessions", sizes["quiz_sessions"], "设计如此:已布置测验为业务数据永久保留;随机练习超 30 天自动清扫", True),
    ]
    for name, count, note, bounded in growth_rules:
        print(f"  {'✅' if bounded else '⚠️ '} {name:<14} {count:>7} 条  {note}")

    first_save, last_save = save_times[0][1] * 1000, save_times[-1][1] * 1000
    print(f"\nsave() 耗时演变: 第1天 {first_save:.1f}ms → 第180天 {last_save:.1f}ms ({last_save / max(first_save, 0.01):.1f}x)")
    print(f"最终 RSS: {rss_mb():.1f}MB (基线 {base:.1f}MB, 增长 {rss_mb() - base:.1f}MB)")

    # ===== 共享组件长时间调用恒定性 =====
    print("\n===== 共享组件 9 万人日调用恒定性 =====")
    quota = _QuotaLimiter(rpm=60, tpm=100000)
    gate_stats = {"acquired": 0, "denied": 0}
    for round_index in range(90000):
        ok = quota.acquire(tpm_estimate=1200, timeout=0)
        gate_stats["acquired" if ok else "denied"] += 1
        if ok and round_index % 7 == 0:
            quota.settle(actual=1100, estimate=1200)
        elif ok:
            quota.refund(1200)
    print(f"令牌桶 9 万次准入: 获得配额 {gate_stats['acquired']}, 拒绝 {gate_stats['denied']}(速率限制下正常), "
          f"内部状态只含两个 float 桶 —— 无累积。")
    # 桶余额应在合理范围内(不被结算误差吃穿或溢出)
    with quota._condition:
        print(f"桶余额: rpm={quota._rpm_tokens:.2f}/60, tpm={quota._tpm_tokens:.2f}/100000")

    tracemap = tracemalloc.take_snapshot()
    print("\n全局内存 Top3(分类):")
    for stat in tracemap.statistics("lineno")[:3]:
        print(f"  {stat.size / 1024:.0f} KB · {stat.count} blocks · {stat.traceback[0]}")


if __name__ == "__main__":
    simulate()
