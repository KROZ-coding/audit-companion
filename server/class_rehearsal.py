# -*- coding: utf-8 -*-
"""课堂全流程演习:教师建班 → AI 出 15 题全题型 → 90 学生注册审核 →
全体作答 + 独特答疑 + AI 练习 → 教师学情统计 + 学生自查。

仅对自有测试实例运行。用法:
    python class_rehearsal.py --base http://127.0.0.1:8020 --students 90
"""
import argparse
import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

# ---------- 计时工具 ----------
PHASES = {}  # name -> {"events": [(who, seconds)], "start": t}


def phase_start(name: str) -> None:
    PHASES[name] = {"events": [], "start": time.monotonic(), "errors": []}


def phase_event(name: str, who: str, seconds: float, error: str | None = None) -> None:
    PHASES[name]["events"].append((who, seconds))
    if error:
        PHASES[name]["errors"].append(f"{who}: {error}")


def phase_end(name: str) -> float:
    return time.monotonic() - PHASES[name]["start"]


def pct(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))] if ordered else 0.0


def new_client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=httpx.Timeout(420, connect=15), trust_env=False)


# ---------- 教师流程 ----------
def teacher_login(client) -> None:
    response = client.post("/api/auth/login", json={"username": "teacher01", "password": "teach123*", "role": "teacher"})
    response.raise_for_status()


def ai_generate_15(client) -> dict:
    """AI 出题:三批共 15 题,覆盖题型(single/multi/short)。返回计时和各批结果。"""
    # 从题库快速出题端点取可用知识点(前端"刷新知识点"同源)
    graph = client.get("/api/graph/nodes", params={"graph": "knowledge"}).json()
    points = [node["name"] for node in graph if node.get("name")][:6]
    if len(points) < 4:
        points = ["审计基础理论", "审计程序方法", "职业道德"]
    batches = [
        {"course_id": "audit-101", "knowledge_points": points[:2], "question_type": "single_choice", "difficulty": "medium", "count": 5},
        {"course_id": "audit-101", "knowledge_points": points[2:4] or points[:2], "question_type": "short_answer", "difficulty": "medium", "count": 5},
        {"course_id": "audit-101", "knowledge_points": points[4:6] or points[:2], "question_type": "multi_choice", "difficulty": "hard", "count": 5},
    ]
    times, question_ids = [], []
    for batch in batches:
        began = time.monotonic()
        for attempt in (1, 2, 3):
            response = client.post("/api/bank/generate", json=batch)
            if response.status_code == 201:
                break
            print(f"    出题批尝试 {attempt} 失败: {response.status_code} {response.text[:90]}", flush=True)
            time.sleep(3)
        times.append(time.monotonic() - began)
        response.raise_for_status()
        question_ids.extend(item["id"] for item in response.json()["questions"])
    return {"times": times, "total": sum(times), "question_ids": question_ids}


def publish_questions(client, question_ids) -> dict:
    """送审 + 通过,题目进入已发布状态。"""
    began = time.monotonic()
    for qid in question_ids:
        client.post(f"/api/bank/{qid}/submit", json={}).raise_for_status()
    submit_done = time.monotonic() - began
    approve_began = time.monotonic()
    for qid in question_ids:
        client.post(f"/api/bank/{qid}/review", json={"decision": "pass", "comment": "课堂演习"}).raise_for_status()
    return {"submit": submit_done, "approve": time.monotonic() - approve_began,
            "total": time.monotonic() - began}


def assign_to_students(client, question_ids, student_ids) -> dict:
    """组卷布置给学生名单。"""
    began = time.monotonic()
    response = client.post("/api/quiz/assign", json={
        "course_id": "audit-101", "title": "第三章 课堂测验", "question_ids": question_ids,
        "student_ids": student_ids,
    }).raise_for_status()
    body = response.json()
    return {"assign": time.monotonic() - began, "session_ids": body.get("session_ids", []),
            "student_count": body.get("student_count")}


def teacher_reports(client) -> dict:
    """教师学情统计:名册 + 学生详情 + Excel 导出。"""
    out = {}
    began = time.monotonic()
    roster = client.get("/api/progress/class", params={"course_id": "audit-101"}).raise_for_status().json()
    out["roster"] = time.monotonic() - began
    students = roster.get("students") if isinstance(roster, dict) else roster
    detail_began = time.monotonic()
    for student in (students or [])[:10]:
        sid = student.get("id") or student.get("student_id")
        if sid:
            client.get(f"/api/progress/students/{sid}", params={"course_id": "audit-101"}).raise_for_status()
    out["detail_10"] = time.monotonic() - detail_began
    excel_began = time.monotonic()
    response = client.post("/api/admin/excel/snapshot", json={"course_id": "audit-101"})
    response.raise_for_status()
    out["excel"] = time.monotonic() - excel_began
    out["excel_bytes"] = len(response.content)
    return out


# ---------- 学生流程 ----------
def student_register(base, index) -> tuple[str, str, float]:
    client = new_client(base)
    username = f"cls{index:03d}"
    began = time.monotonic()
    response = client.post("/api/auth/register", json={
        "username": username, "display_name": f"课堂学生{index:03d}", "student_number": f"C{index:06d}",
        "password": f"Class*{index:03d}pw", "phone": f"137{index % 100:08d}",
        "class_code": "AUD101",
    })
    elapsed = time.monotonic() - began
    if response.status_code != 202:
        return username, "", elapsed, response.text[:80]
    return username, f"Class*{index:03d}pw", elapsed, None


def student_answer_ask_practice(client, index, assignment) -> dict:
    """单个学生:答测验(部分对部分错) + 独特答疑 + AI 练习。"""
    result = {}
    # 1. 答测验:按 index 决定正确率,制造成绩分布
    began = time.monotonic()
    mine = client.get("/api/quiz/assignments", params={"course_id": "audit-101"}).raise_for_status().json()
    mine = [item for item in mine if item["status"] == "ongoing"]
    if not mine:
        raise RuntimeError("没有待作答的布置测验")
    detail = client.get(f"/api/quiz/{mine[0]['id']}").raise_for_status().json()
    answers = {}
    correct_ratio = 0.4 + (index % 6) * 0.1  # 40%-90%
    for question in detail["questions"]:
        qid, qtype = question["id"], question["type"]
        if random.random() < correct_ratio:
            answers[qid] = {"single_choice": 1, "multi_choice": [0, 1], "judge": True, "fill": "充分性与适当性", "short_answer": "充分性是数量要求,适当性是质量要求,数量不能弥补质量缺陷。", "case": "应执行函证与截止测试,并评估坏账准备。"}[qtype]
        else:
            answers[qid] = {"single_choice": 0, "multi_choice": [2], "judge": False, "fill": "不确定", "short_answer": "大概就是数量和质量的区别。", "case": "直接出具无保留意见。"}[qtype]
    submit = client.post(f"/api/quiz/{detail['id']}/submit", json={"answers": answers})
    submit.raise_for_status()
    result["quiz"] = time.monotonic() - began
    body = submit.json()
    result["quiz_status"] = body.get("result", {}).get("status")
    result["score"] = body.get("result", {}).get("total")

    # 2. 独特答疑(每人问题不同)
    began = time.monotonic()
    ask = client.post("/api/chat/ask", json={
        "question": f"我是{index:03d}号学生,请结合审计证据的{['可靠性','充分性','适当性','获取程序','函证控制','截止测试','分析程序','职业怀疑','工作底稿','复核要点'][index % 10]}讲一讲{'审计Evidence' if index % 3 == 0 else '审计证据'},并给出一个{['制造业','零售业','金融业','服务业','建筑业'][index % 5]}的例子",
        "course_id": "audit-101",
    })
    ask.raise_for_status()
    ask_body = ask.json()
    result["ask"] = time.monotonic() - began
    result["ask_ok"] = not ask_body.get("degraded")
    result["ask_sources"] = len(ask_body.get("sources") or [])

    # 3. AI 练习:依据刚才的答疑生成并作答
    began = time.monotonic()
    generated = client.post("/api/practice/generate", json={"course_id": "audit-101", "count": 2,
                                                            "source_question": f"{index:03d}号:审计证据的可靠性如何判断"})
    generated.raise_for_status()
    detail_p = generated.json()
    answers_p = {question["id"]: 1 for question in detail_p["questions"] if question["type"] == "single_choice"}
    for question in detail_p["questions"]:
        if question["type"] == "judge":
            answers_p[question["id"]] = True
        elif question["type"] == "fill":
            answers_p[question["id"]] = "外部"
    submit_p = client.post(f"/api/practice/{detail_p['id']}/submit", json={"answers": answers_p})
    submit_p.raise_for_status()
    client.delete(f"/api/practice/{detail_p['id']}")
    result["practice"] = time.monotonic() - began
    result["practice_questions"] = len(detail_p["questions"])
    return result


def student_self_check(client) -> dict:
    """学生自查:学情报告 + 掌握度 + 历史成绩。"""
    began = time.monotonic()
    me = client.get("/api/auth/me").raise_for_status().json()
    report = client.get("/api/progress/summary", params={"course_id": "audit-101"})
    mastery = client.get("/api/progress/mastery", params={"course_id": "audit-101"})
    history = client.get("/api/progress/history", params={"course_id": "audit-101"})
    elapsed = time.monotonic() - began
    ok = history.status_code == 200
    return {"seconds": elapsed, "ok": ok}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8020")
    parser.add_argument("--students", type=int, default=90)
    args = parser.parse_args()

    print(f"课堂演习目标: {args.base} · 学生 {args.students} 人\n", flush=True)

    # ===== 阶段 A:教师准备 =====
    print("== 阶段 A:教师建班与 AI 出题 ==", flush=True)
    phase_start("A")
    teacher = new_client(args.base)
    teacher_login(teacher)
    phase_event("A", "teacher_login", 0)
    gen = ai_generate_15(teacher)
    PHASES["A"]["gen"] = gen
    published = publish_questions(teacher, gen["question_ids"])
    PHASES["A"]["publish"] = published
    phase_end("A")
    print(f"  AI 出 15 题(3 批): 总 {gen['total']:.1f}s, 各批 {['%.1f' % t for t in gen['times']]}", flush=True)

    # ===== 阶段 B:注册与审核 =====
    print(f"\n== 阶段 B:{args.students} 名学生注册 → 教师审核 ==", flush=True)
    phase_start("B")
    with ThreadPoolExecutor(max_workers=30) as pool:
        registrations = list(pool.map(lambda i: student_register(args.base, i), range(1, args.students + 1)))
    reg_errors = [r for r in registrations if r[3]]
    usernames = [(r[0], r[1]) for r in registrations if not r[3]]
    print(f"  注册完成 {len(usernames)}/{args.students}, 失败 {len(reg_errors)}", flush=True)
    if reg_errors[:3]:
        print(f"  失败样例: {reg_errors[:3]}", flush=True)
    # 教师逐个审核(串行,像真实课堂)
    approve_client = new_client(args.base)
    teacher_login(approve_client)
    pending = approve_client.get("/api/enrollment/pending").raise_for_status().json()
    approve_times = []
    approved_ids = []
    for row in pending:
        began = time.monotonic()
        response = approve_client.post(f"/api/enrollment/{row['id']}/approve", json={"class_name": "课堂演习班"})
        if response.status_code == 200:
            approved_ids.append(response.json()["id"])
            approve_times.append(time.monotonic() - began)
    student_ids = approved_ids
    B_total = phase_end("B")
    print(f"  审核完成 {len(student_ids)} 人, 单次审核 p50 {pct(approve_times, 0.5):.2f}s", flush=True)

    # ===== 阶段 C:作答 + 答疑 + 练习 =====
    print(f"\n== 阶段 C:{len(student_ids)} 人同时作答 + 独特答疑 + AI 练习 ==", flush=True)
    phase_start("C")
    assign_info = assign_to_students(approve_client, gen["question_ids"], student_ids)
    print(f"  教师布置给 {assign_info['student_count']} 人, 耗时 {assign_info['assign']:.2f}s", flush=True)
    # 学生端点拿自己的会话:取任一学生的 assignments 视图(会话 ID 一致)
    probe_client = new_client(args.base)
    probe_client.post("/api/auth/login", json={"username": usernames[0][0], "password": usernames[0][1], "role": "student"}).raise_for_status()
    probe_client.close()
    # 每个学生各自的会话 ID 由各自登录后查询;这里只需 title
    assignment = {"title": "第三章 课堂测验"}

    def run_student(i):
        client = new_client(args.base)
        username, password = usernames[i]
        login_began = time.monotonic()
        response = client.post("/api/auth/login", json={"username": username, "password": password, "role": "student"})
        response.raise_for_status()
        login_time = time.monotonic() - login_began
        try:
            outcome = student_answer_ask_practice(client, i, assignment)
            outcome["login"] = login_time
            outcome["who"] = username
            phase_event("C", username, outcome["quiz"] + outcome["ask"] + outcome["practice"])
            return outcome
        except Exception as error:
            phase_event("C", username, time.monotonic() - login_began, str(error)[:90])
            return {"who": username, "error": str(error)[:90], "login": login_time}

    with ThreadPoolExecutor(max_workers=45) as pool:
        outcomes = list(pool.map(run_student, range(len(usernames))))
    C_total = phase_end("C")

    ok_outcomes = [o for o in outcomes if "error" not in o]
    err_outcomes = [o for o in outcomes if "error" in o]
    print(f"  完整完成 {len(ok_outcomes)}/{len(usernames)}, 失败 {len(err_outcomes)}", flush=True)
    if err_outcomes[:3]:
        print(f"  失败样例: {[o['error'] for o in err_outcomes[:3]]}", flush=True)

    # ===== 阶段 D:统计与自查 =====
    print("\n== 阶段 D:教师学情统计 + 学生自查 ==", flush=True)
    phase_start("D")
    reports = teacher_reports(approve_client)
    teacher_time = phase_end("D")
    phase_start("D_self")
    with ThreadPoolExecutor(max_workers=30) as pool:
        self_checks = list(pool.map(
            lambda i: (lambda c: (student_self_check(c), c.close()))(new_client(args.base)) if False else None,
            [],
        ))
    # 学生自查(并发抽样 30 人)
    def self_check_task(i):
        client = new_client(args.base)
        client.post("/api/auth/login", json={"username": usernames[i][0], "password": usernames[i][1], "role": "student"}).raise_for_status()
        began = time.monotonic()
        try:
            client.get("/api/progress/me", params={"course_id": "audit-101"}).raise_for_status()
            client.get("/api/progress/history", params={"course_id": "audit-101"}).raise_for_status()
            return time.monotonic() - began, True
        except Exception:
            return time.monotonic() - began, False
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=30) as pool:
        self_results = list(pool.map(self_check_task, range(0, len(usernames), max(1, len(usernames) // 30))))
    D_self_total = phase_end("D_self")

    # ===== 汇总 =====
    print("\n" + "=" * 72)
    print("课堂全流程演习汇总")
    print("=" * 72)
    rows = []
    rows.append(("A. AI 出 15 题(3 批)", f"{gen['total']:.1f}s", "批均 %.1fs" % (gen['total'] / 3)))
    rows.append(("B. 90 人注册", f"{statistics.median([r[2] for r in registrations if not r[3]]):.1f}s p50",
                 f"审核单次 p50 {pct(approve_times, 0.5):.2f}s"))
    quiz_times = [o["quiz"] for o in ok_outcomes]
    ask_times = [o["ask"] for o in ok_outcomes]
    practice_times = [o["practice"] for o in ok_outcomes]
    ask_ok = sum(1 for o in ok_outcomes if o["ask_ok"])
    ask_src = sum(1 for o in ok_outcomes if o["ask_sources"] > 0)
    rows.append(("C-1. 全体作答(提交→批改返回)", f"p50 {pct(quiz_times, .5):.1f}s / p95 {pct(quiz_times, .95):.1f}s", f"n={len(quiz_times)}"))
    rows.append(("C-2. 独特答疑(每人不同问题)", f"p50 {pct(ask_times, .5):.1f}s / p95 {pct(ask_times, .95):.1f}s", f"命中资料 {ask_src}/{len(ask_times)}, 无降级 {ask_ok}/{len(ask_times)}"))
    rows.append(("C-3. AI 练习(生成+作答)", f"p50 {pct(practice_times, .5):.1f}s / p95 {pct(practice_times, .95):.1f}s", f"人均 {sum(o['practice_questions'] for o in ok_outcomes) / max(len(ok_outcomes), 1):.1f} 题"))
    self_times = [t for t, ok in self_results if ok]
    rows.append(("D-1. 教师学情统计", f"名册 {reports['roster']:.2f}s · 详情10人 {reports['detail_10']:.2f}s · Excel {reports['excel']:.2f}s", f"Excel {reports['excel_bytes']//1024}KB"))
    rows.append(("D-2. 学生自查(30 人抽样)", f"p50 {pct(self_times, .5):.2f}s / p95 {pct(self_times, .95):.2f}s", f"成功 {len(self_times)}/{len(self_results)}"))
    phase_totals = {"A": gen["total"] + 0.5, "B": B_total, "C": C_total, "D": teacher_time + D_self_total}
    rows.append(("阶段合计", f"A {phase_totals['A']:.1f}s · B {phase_totals['B']:.1f}s · C {phase_totals['C']:.1f}s · D {phase_totals['D']:.1f}s",
                 f"整堂演习 {sum(phase_totals.values()):.0f}s(不含思考时间)"))
    width = max(len(r[0]) for r in rows)
    for name, timing, extra in rows:
        print(f"{name:<{width}}  {timing:<40} {extra}")

    # 评分分布(教师视角的效果)
    scores = [o["score"] for o in ok_outcomes if o.get("score") is not None]
    if scores:
        print(f"\n成绩分布: n={len(scores)} 最高 {max(scores)} 最低 {min(scores)} 均分 {statistics.mean(scores):.1f} 中位 {statistics.median(scores)}")
        bands = {"优秀(≥80%)": 0, "良好(60-79%)": 0, "及格(40-59%)": 0, "待加强(<40%)": 0}
        for s in scores:
            ratio = s / 135.0  # 15 题卷面
            bands["优秀(≥80%)" if ratio >= .8 else "良好(60-79%)" if ratio >= .6 else "及格(40-59%)" if ratio >= .4 else "待加强(<40%)"] += 1
        for band, count in bands.items():
            print(f"  {band}: {count} 人")
    print(f"\n失败明细: C 阶段 {len(err_outcomes)} 人" + (f" {[(o['who'], o['error'][:50]) for o in err_outcomes[:5]]}" if err_outcomes else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
