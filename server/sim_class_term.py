"""学期模拟器:30 名学生 × 30 节课的真实流程压排(数据全部来自服务端,不伪造成绩)。

设计目标(与 class_rehearsal.py 的单堂演习不同,这里是"整个学期"):
  - 真实注册:学生走 /api/auth/register(pending)→ 教师 /api/enrollment 逐个审核;
  - 真实发布:教师每讲建题(draft)→ 送审(reviewing)→ 审核通过(published)→ 组卷布置;
  - 真实结论:成绩/掌握度/学情报告全部由服务端判分与统计产生,本脚本只负责"当人";
  - 可在 Docker 实例里登录查证:admin / teacher01 为种子账户,学生 sim01..simNN。

用法(推荐在 compose 的 sim-app 容器内执行,零宿主机依赖):
  docker compose -f docker-compose.sim.yml exec sim-app \
      python sim_class_term.py --base http://127.0.0.1:8000
宿主机执行(需要 httpx):python sim_class_term.py --base http://127.0.0.1:18000

假模型:配合 docker-compose.sim.yml 的 sim-llm 服务(LLM_BASE_URL 指向它),
所有 LLM 环节(答疑/主观题 rubric 批改/学情报告)零真实 API 消耗;
若实例配置了真实通道且不带 --no-llm-check,也可以直接打真实模型(会消耗额度)。
"""

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

CLIENT_TIMEOUT = 60.0
CHAT_POOL = [
    "什么是审计证据?可靠性怎么判断?", "系统讲解函证程序", "注册会计师的职业道德有哪些要求?",
    "审计重要性水平怎么确定?", "七大审计程序分别适用什么场景?", "审计抽样有哪些方式?",
    "什么是审计风险模型?", "监盘和盘点有什么区别?", "审计工作底稿的作用是什么?",
    "四种审计意见的出具条件?", "内部控制的五要素?", "什么是管理层凌驾风险?",
    "审计沟通要和治理层沟通什么?", "质量控制制度的要素?", "集团审计要注意什么?",
    "持续经营假设怎么评估?", "关联方审计的要点?", "会计估计的审计程序?",
    "期后事项分哪几类?", "书面声明的作用与局限?", "利用内部审计工作的条件?",
    "什么是职业怀疑?举例说明", "审计业务约定书包括哪些内容?", "错报怎么汇总评价?",
    "数据分析在审计中的应用?", "什么是反舞弊程序?", "审计档案保存期限要求?",
    "关键审计事项是什么?", "如何评估审计证据的充分性?", "审阅和审计的区别?",
    "审计独立性受威胁的情形?", "完成审计工作前要做什么?", "什么是重要性中的性质因素?",
    "控制测试和实质性程序的区别?", "什么是双重目的测试?", "函证不回函怎么办?",
    "舞弊三角理论的内容?", "审计意见段的结构?", "如何做分析程序?", "审计抽样风险有哪些?",
]
FILLER_SHORT = "大概就是数量和质量的区别,展开说不清楚。"

# 8 道手工模板题(客观题内容来自种子题风格),按讲次轮换复用并标记章节
QUESTION_FACTORIES = [
    ("single_choice", lambda ch: {
        "stem": f"下列各项中,可靠性最高的审计证据是({ch})。",
        "options": ["管理层书面声明", "银行询证函回函", "内部记账凭证", "口头答复"],
        "answer": 1, "reference_answer": "银行询证函回函,外部直接获取的证据可靠性最高。",
        "rubric": [], "knowledge_points": ["审计证据可靠性"], "difficulty": "easy",
    }),
    ("multi_choice", lambda ch: {
        "stem": f"下列哪些属于获取审计证据的具体程序({ch})?",
        "options": ["检查", "观察", "函证", "重新计算"],
        "answer": [0, 1, 2, 3], "reference_answer": "检查、观察、函证、重新计算均为审计程序。",
        "rubric": [], "knowledge_points": ["审计程序"], "difficulty": "medium",
    }),
    ("judge", lambda ch: {
        "stem": f"审计风险越高,可接受的检查风险越低({ch})。",
        "options": [], "answer": True, "reference_answer": "由审计风险模型,两者反向变动。",
        "rubric": [], "knowledge_points": ["审计风险模型"], "difficulty": "medium",
    }),
    ("fill", lambda ch: {
        "stem": f"审计证据的____性是数量要求,____性是质量要求({ch})。",
        "options": [], "answer": "充分", "reference_answer": "充分性是数量要求,适当性是质量要求。",
        "rubric": [], "knowledge_points": ["审计证据充分性与适当性"], "difficulty": "medium",
    }),
    ("single_choice", lambda ch: {
        "stem": f"注册会计师与被审计单位管理层串通,最直接违背的是({ch})。",
        "options": ["独立性", "及时性", "经济性", "灵活性"],
        "answer": 0, "reference_answer": "独立性是职业道德的基本原则。",
        "rubric": [], "knowledge_points": ["职业道德"], "difficulty": "easy",
    }),
    ("multi_choice", lambda ch: {
        "stem": f"下列关于重要性的说法,正确的有({ch})。",
        "options": ["数量维度", "性质维度", "与证据数量反向", "可以每张凭证各定一个"],
        "answer": [0, 1, 2], "reference_answer": "重要性同时具有数量与性质两个维度。",
        "rubric": [], "knowledge_points": ["重要性"], "difficulty": "hard",
    }),
    ("judge", lambda ch: {
        "stem": f"函证回函直接寄给被审计单位即可,无需注册会计师控制({ch})。",
        "options": [], "answer": False, "reference_answer": "函证全过程必须由注册会计师控制。",
        "rubric": [], "knowledge_points": ["函证"], "difficulty": "easy",
    }),
    ("short_answer", lambda ch: {
        "stem": f"简述审计证据的充分性与适当性及两者关系({ch})。",
        "options": [], "answer": None,
        "reference_answer": "充分性是数量要求,适当性是质量要求,数量不能弥补质量缺陷。",
        "rubric": [{"point": "充分性", "score": 8}, {"point": "适当性", "score": 8}, {"point": "关系", "score": 9}],
        "knowledge_points": ["审计证据充分性与适当性"], "difficulty": "medium",
    }),
]
# 每讲的题型编排:第 1 轮含主观题(触发教师复核),其余轮次纯客观
ROUND_PLAN = {1: [0, 2, 7], 2: [1, 4], 3: [3, 5], 4: [6, 2], 5: [0, 1]}
GOOD_SHORT = "充分性是数量要求,适当性是质量要求;数量不足不能以质量弥补,两者缺一不可。"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fail(message: str):
    print(f"[终止] {message}", file=sys.stderr)
    sys.exit(1)


def make_client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=CLIENT_TIMEOUT, trust_env=False)


def call(client: httpx.Client, method: str, path: str, ok=(200, 201, 202, 204), retries=2, **kwargs) -> httpx.Response:
    last = None
    for attempt in range(retries + 1):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            last = error
            time.sleep(1.0 + attempt)
            continue
        if response.status_code in ok:
            return response
        last = RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
        time.sleep(0.5)
    raise RuntimeError(f"请求最终失败: {last}")


def login(base: str, username: str, password: str, role: str) -> httpx.Client:
    client = make_client(base)
    call(client, "POST", "/api/auth/login", json={"username": username, "password": password, "role": role})
    return client


def build_answer(question: dict, meta: dict, ability: float, rng: random.Random):
    """按学生能力概率作答;返回 (答案, 主观题质量 0~1 或 None)。"""
    qtype = question["type"]
    correct = meta["answer"]
    if rng.random() < ability:
        return (list(correct) if isinstance(correct, list) else correct), 1.0
    if qtype == "single_choice":
        wrong = (correct + 1) % max(len(question.get("options") or [1]), 2)
        return wrong, 0.0
    if qtype == "multi_choice":
        wrong = [0] if correct != [0] else [1]
        return wrong, 0.0
    if qtype == "judge":
        return (not correct), 0.0
    if qtype == "fill":
        return "不确定", 0.0
    return (GOOD_SHORT if rng.random() < ability else FILLER_SHORT), (1.0 if rng.random() < ability else 0.35)


def main():
    parser = argparse.ArgumentParser(description="审计智能学伴 · 学期模拟器(30 人 × 30 讲)")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="服务端地址")
    parser.add_argument("--students", type=int, default=30)
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--quizzes-per-session", type=int, default=3, choices=range(1, 6),
                        help="每讲布置的测验轮数(≤5),第 1 轮含主观题")
    parser.add_argument("--chats-per-session", type=int, default=1, help="每讲每名学生答疑次数")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--pause-seconds", type=float, default=0.0, help="两讲之间的真实停顿(默认 0)")
    parser.add_argument("--seed", type=int, default=2026, help="随机种子(可复现)")
    parser.add_argument("--class-prefix", default="sim", help="学生用户名前缀(如 sim → sim01)")
    parser.add_argument("--course-id", default="sim-term", help="模拟课程编号")
    parser.add_argument("--no-llm-check", action="store_true", help="跳过对假模型的连通性检查")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    random.seed(args.seed)

    started = time.time()
    base = args.base.rstrip("/")

    # ---- 0. 健康检查 ----
    with make_client(base) as probe:
        try:
            health = call(probe, "GET", "/health").json()
        except RuntimeError as error:
            fail(f"服务不可用({base}):{error}")
    if not args.no_llm_check and not health.get("integrations", {}).get("llm"):
        fail("实例未配置任何 LLM 通道;请用 docker-compose.sim.yml(自带 sim-llm)启动实例。")
    knowledge = health.get("knowledge", {})
    print(f"[0/3] 服务就绪 env={health.get('environment')} llm={health['integrations']['llm']} "
          f"知识块={knowledge.get('chunks', 0)}")

    # ---- 1. 教师建课(真实管理 API)----
    admin = login(base, "admin", "admin123*", "admin")
    teacher = login(base, "teacher01", "teach123*", "teacher")
    teacher_id = call(teacher, "GET", "/api/auth/me").json()["id"]
    existing = [c for c in call(admin, "GET", "/api/admin/courses").json() if c["id"] == args.course_id]
    if existing:
        course = existing[0]
        print(f"[1/3] 复用已有模拟课程 {course['id']}(课堂代码 {course['class_code']})")
    else:
        course = call(admin, "POST", "/api/admin/courses", json={
            "id": args.course_id, "name": f"审计学(学期模拟{args.seed})", "term": "2025-2026",
            "teacher_id": teacher_id,
        }).json()
        print(f"[1/3] 教师建课成功 {course['id']} · 课堂代码 {course['class_code']}")
    class_code = course["class_code"]
    course_id = course["id"]

    # ---- 2. 学生真实注册 → 教师真实审核 ----
    students = []
    def register_one(index: int):
        number = f"{index:02d}"
        username = f"{args.class_prefix}{number}"
        payload = {
            "username": username, "display_name": f"模拟学生{number}",
            "student_number": f"2025{args.class_prefix.upper()}{number}",
            "password": f"Sim{number}cls*", "phone": f"138{index:08d}", "class_code": class_code,
        }
        try:
            with make_client(base) as client:
                response = client.post("/api/auth/register", json=payload)
                return {"index": index, "username": username, "password": payload["password"],
                        "display_name": payload["display_name"], "student_number": payload["student_number"],
                        "status_code": response.status_code}
        except httpx.HTTPError as error:
            return {"index": index, "username": username, "password": payload["password"],
                    "display_name": payload["display_name"], "student_number": payload["student_number"],
                    "status_code": f"transport:{type(error).__name__}"}

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(register_one, range(1, args.students + 1)))
    created = [item for item in results if item["status_code"] == 202]
    conflict = [item for item in results if item["status_code"] == 409]
    if len(created) + len(conflict) != args.students:
        fail(f"注册出现异常响应:{[item['status_code'] for item in results]}")
    approved = 0
    for _ in range(6):
        pending = call(teacher, "GET", "/api/enrollment/pending").json()
        mine = [row for row in pending if row["username"].startswith(args.class_prefix)]
        if not mine:
            break
        for row in mine:
            call(teacher, "POST", f"/api/enrollment/{row['id']}/approve",
                 json={"class_name": "模拟一班"})
            approved += 1
    print(f"[1/3] 学生注册 {args.students} 人(新建 {len(created)}/复用 {len(conflict)}),教师审核通过 {approved} 人")

    # ---- 3. 学生登录,建立会话 ----
    roster = []
    def login_student(item):
        try:
            client = login(base, item["username"], item["password"], "student")
            uid = call(client, "GET", "/api/auth/me").json()["id"]
            return item | {"client": client, "uid": uid, "error": None}
        except RuntimeError as error:
            return item | {"client": None, "uid": None, "error": str(error)[:120]}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        roster = list(pool.map(login_student, results))
    broken = [item["username"] for item in roster if item["client"] is None]
    if broken:
        fail(f"学生登录失败:{broken}")
    for item in roster:
        item["ability"] = round(0.5 + 0.45 * ((item["index"] * 7) % 10) / 9, 3)  # 0.50~0.95 能力分布
    print(f"[1/3] {len(roster)} 名学生全部登录成功;能力分布 0.50~0.95(能力值只影响答题概率,不参与判分)")

    # ---- 4. 逐讲推进 ----
    stats = {item["username"]: {"quizzes": 0, "score": 0.0, "max": 0.0, "chats": 0,
                                "reviewed": 0, "quality": {}} for item in roster}
    bank_created = 0
    chat_pool_cycle = list(CHAT_POOL)
    rng.shuffle(chat_pool_cycle)

    for session in range(1, args.sessions + 1):
        chapter = f"第{session}讲"
        plan = [ROUND_PLAN[1]] + [ROUND_PLAN[min(round_no, max(ROUND_PLAN))] for round_no in range(2, args.quizzes_per_session + 1)]
        round_questions = []
        for round_no, template_ids in enumerate(plan, start=1):
            questions = []
            for template_id in template_ids:
                kind, factory = QUESTION_FACTORIES[template_id]
                body = factory(chapter)
                body.update({"type": kind, "course_id": course_id, "chapter": chapter,
                             "quick_response": kind in {"single_choice", "judge"}})
                question = call(teacher, "POST", "/api/bank", json=body).json()
                call(teacher, "POST", f"/api/bank/{question['id']}/submit")
                call(teacher, "POST", f"/api/bank/{question['id']}/review",
                     json={"decision": "pass", "comment": ""})
                questions.append({"id": question["id"], "type": kind, "answer": body["answer"],
                                  "stem": body["stem"], "options": body["options"]})
                bank_created += 1
            round_questions.append(questions)

        for questions in round_questions:
            call(teacher, "POST", "/api/quiz/assign", json={
                "course_id": course_id, "title": f"{chapter}随堂测验",
                "question_ids": [question["id"] for question in questions],
                "due_at": (now_utc() + timedelta(days=7)).isoformat(),
            })
        assignments_count = len(round_questions)

        def do_session(item):
            client = item["client"]
            username = item["username"]
            try:
                rng2 = random.Random(args.seed * 1000 + item["index"] * 131 + session)
                ability = min(0.98, item["ability"] + 0.003 * session)  # 缓慢进步曲线
                done, scored, maxed = 0, 0.0, 0.0
                rows = call(client, "GET", f"/api/quiz/assignments",
                            params={"course_id": course_id}).json()
                ongoing = [row for row in rows if row["status"] == "ongoing"]
                for row in ongoing:
                    quiz = call(client, "GET", f"/api/quiz/{row['id']}").json()
                    answers = {}
                    for question in quiz["questions"]:
                        meta = next((q for q in sum(round_questions, []) if q["id"] == question["id"]), None)
                        if meta is None:
                            continue
                        answer, quality = build_answer(question, meta, ability, rng2)
                        answers[question["id"]] = answer
                        stats[username]["quality"][(session, question["id"])] = quality
                    if not answers:
                        continue
                    result = call(client, "POST", f"/api/quiz/{row['id']}/submit",
                                  json={"answers": answers}).json()["result"]
                    done += 1
                    scored += float(result.get("total") or 0)
                    maxed += float(result.get("max_total") or 0)
                chats = 0
                for chat_no in range(args.chats_per_session):
                    question = chat_pool_cycle[(item["index"] * 31 + session * 7 + chat_no) % len(chat_pool_cycle)]
                    call(client, "POST", "/api/chat/ask",
                         json={"question": question, "course_id": course_id})
                    chats += 1
                return done, scored, maxed, chats
            except Exception as error:  # 单个学生异常不拖垮整讲,记入日志继续
                print(f"  [警告] {username} 第{session}讲执行异常:{str(error)[:160]}", flush=True)
                return 0, 0.0, 0.0, 0

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            round_results = list(pool.map(do_session, roster))
        for item, (done, scored, maxed, chats) in zip(roster, round_results):
            entry = stats[item["username"]]
            entry["quizzes"] += done
            entry["score"] += scored
            entry["max"] += maxed
            entry["chats"] += chats

        # 教师复核本讲主观题(复核是掌握度计入的前提,真实工作流);按服务端返回的 student.id 精确匹配
        uid_to_item = {item.get("uid"): item for item in roster if item.get("uid")}
        reviewed = 0
        for row in call(teacher, "GET", "/api/grading/queue/details").json():
            item = uid_to_item.get(row["student"]["id"])
            quality = 0.6
            if item is not None:
                quality = stats[item["username"]]["quality"].get((session, row["question"]["id"]), 0.6)
            score = round(row["max_score"] * quality * 2) / 2
            call(teacher, "POST", f"/api/grading/{row['result_id']}/review", json={
                "question_id": row["question"]["id"], "score": score,
                "reason": "复核:回答覆盖主要评分要点" if quality >= 1 else "复核:回答未充分覆盖评分要点",
            })
            reviewed += 1

        print(f"[{session}/{args.sessions}] 测验 {assignments_count} 轮 · "
              f"提交 {sum(item[0] for item in round_results)} · 答疑 "
              f"{sum(item[3] for item in round_results)} · 复核 {reviewed} · 累计题目 {bank_created}")
        if args.pause_seconds:
            time.sleep(args.pause_seconds)

    # ---- 5. 期末:学情报告 + 班级统计 + Excel 快照 ----
    print("[3/3] 期末总结:生成 30 份学情报告、班级统计与 Excel 快照…")

    def term_report(item):
        client = item["client"]
        report = call(client, "POST", "/api/progress/report").json()["report"]
        me = call(client, "GET", "/api/progress/me").json()
        entry = stats[item["username"]]
        return {"username": item["username"], "password": item["password"],
                "display_name": item["display_name"], "student_number": item["student_number"],
                "ability": item["ability"], **{k: entry[k] for k in ("quizzes", "score", "max", "chats")},
                "mastered": len(me.get("mastered", [])), "weak": len(me.get("weak", [])),
                "insufficient": len(me.get("insufficient", [])),
                "report_source": report.get("source"), "report_chars": len(report.get("markdown") or "")}

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        term_rows = list(pool.map(term_report, roster))

    class_stats = call(teacher, "GET", "/api/progress/class", params={"course_id": course_id}).json()
    excel = call(admin, "POST", "/api/admin/excel/snapshot", json={"course_id": course_id})

    excel_path = None
    try:
        os.makedirs("/data/excel", exist_ok=True)
    except OSError:
        pass
    # 快照按执行环境落盘:容器内优先 /data(即 sim-data 卷),宿主机执行则落当前目录
    for candidate in ("/data/excel/term_snapshot.xlsx", "sim_term_excel.xlsx"):
        try:
            with open(candidate, "wb") as handle:
                handle.write(excel.content)
            excel_path = candidate
            break
        except OSError:
            continue

    summary = {
        "generated_at": now_utc().isoformat(),
        "base": base, "course": {"id": course_id, "class_code": class_code, "name": course["name"]},
        "credentials": {"admin": "admin/admin123*", "teacher": "teacher01/teach123*"},
        "plan": {"students": args.students, "sessions": args.sessions,
                 "quizzes_per_session": args.quizzes_per_session},
        "bank_questions_created": bank_created,
        "class_stats": {"student_count": class_stats.get("student_count"),
                        "students_with_data": class_stats.get("students_with_data"),
                        "weak_points": class_stats.get("weak_points", [])[:8]},
        "students": term_rows,
    }
    summary_path = None
    for candidate in ("/data/sim_summary.json", "sim_summary.json"):
        try:
            with open(candidate, "w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
            summary_path = candidate
            break
        except OSError:
            continue

    total_score = sum(row["score"] for row in term_rows)
    total_max = sum(row["max"] for row in term_rows)
    print("\n===== 学期模拟完成 =====")
    print(f"耗时 {time.time() - started:.0f}s · 课程 {course_id}(课堂代码 {class_code}) · "
          f"新建题目 {bank_created}")
    print(f"全班 {len(term_rows)} 人 · 测验提交 {sum(row['quizzes'] for row in term_rows)} 份 · "
          f"答疑 {sum(row['chats'] for row in term_rows)} 次 · "
          f"整体得分率 {100 * total_score / max(total_max, 1):.1f}%")
    print(f"薄弱知识点 Top: {[(p['knowledge_point'], p['average_mastery']) for p in (class_stats.get('weak_points') or [])[:5]]}")
    print(f"Excel 快照:{excel_path} · 学期汇总:{summary_path}")
    print("登录查证(浏览器打开 http://127.0.0.1:18000):")
    print(f"  教师:teacher01 / teach123*(在「学生管理」可见 {len(term_rows)} 名学生)")
    print(f"  管理员:admin / admin123*")
    print(f"  学生: sim01 ~ sim{args.students:02d} / 密码 SimNNcls*(如 sim01 / Sim01cls*)")
    print("  学生明细与密码表见学期汇总 JSON 的 students 字段。")


if __name__ == "__main__":
    main()
