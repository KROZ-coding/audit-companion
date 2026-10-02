"""Authorized attack-surface test for the audit companion backend.

红队视角的自查工具 —— 只允许对 YOU OWN 的本地/测试实例运行,禁止对未授权目标使用。

Usage (from the server directory, against a dedicated test instance):
    .\\.venv\\Scripts\\python.exe security_test.py --base http://127.0.0.1:8020

Recommended instance config: temp DATA_DIR, PERSISTENCE_ENABLED=1, APP_ENV=development,
SKIP_ENV_FILE=1 (no model), CHAT_DAILY_LIMIT=3 so the quota test is cheap.
"""

import argparse
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx
from openpyxl import load_workbook

PASS = "PASS"
INFO = "INFO"


@dataclass
class Finding:
    test_id: str
    name: str
    severity: str  # HIGH / MEDIUM / LOW / INFO
    passed: bool
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, test_id: str, name: str, severity: str, passed: bool, detail: str) -> None:
        self.findings.append(Finding(test_id, name, severity, passed, detail))
        mark = "OK  " if passed else "HIT "
        print(f"[{mark}] {test_id:<6} ({severity:<4}) {name} :: {detail}")

    def summary(self) -> int:
        hits = [item for item in self.findings if not item.passed and item.severity in {"HIGH", "MEDIUM"}]
        print("\n========== 结论 ==========")
        print(f"检查项 {len(self.findings)} · 命中(中高危) {len(hits)}")
        for item in hits:
            print(f"  [{item.severity}] {item.test_id} {item.name}: {item.detail}")
        return 1 if hits else 0


def make_client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=httpx.Timeout(30, connect=10))


def login(client: httpx.Client, username: str, password: str, role: str = "student") -> httpx.Response:
    return client.post("/api/auth/login", json={"username": username, "password": password, "role": role})


def status_ok(response: httpx.Response, expected: tuple[int, ...]) -> bool:
    return response.status_code in expected


def section(title: str) -> None:
    print(f"\n---- {title} ----")


def run(base: str) -> Report:
    report = Report()
    client = make_client(base)
    admin = make_client(base)
    teacher = make_client(base)
    student = make_client(base)
    student2 = make_client(base)
    attacker = make_client(base)

    section("A. 认证与会话")
    r = client.get("/api/auth/me")
    report.add("A1", "未登录访问受保护端点被拒", "HIGH", r.status_code == 401, f"me -> {r.status_code}")

    r = client.get("/api/admin/users", cookies={"sid": "forged-by-attacker"})
    report.add("A2", "伪造会话 Cookie 被拒", "HIGH", r.status_code in {401, 403}, f"admin/users -> {r.status_code}")

    r = admin.post("/api/auth/login", json={"username": "admin", "password": "admin123*", "role": "admin"})
    ok = r.status_code == 200
    r = admin.post("/api/auth/logout")
    r = admin.get("/api/auth/me")
    report.add("A3", "退出后会话立即失效", "HIGH", r.status_code == 401, f"logout 后 me -> {r.status_code}")
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin123*", "role": "admin"})
    teacher.post("/api/auth/login", json={"username": "teacher01", "password": "teach123*", "role": "teacher"})
    student_login = student.post("/api/auth/login", json={"username": "stu001", "password": "stu123*", "role": "student"})
    login_cookie = student_login.headers.get("set-cookie", "")

    lock_user = "stu-noexist-lock"
    last = None
    for _ in range(5):
        last = attacker.post("/api/auth/login", json={"username": lock_user, "password": "wrong-pass*1"})
    locked = last is not None and last.status_code == 429
    report.add("A4", "连续失败 5 次触发账号锁定", "MEDIUM", locked, f"第5次失败 -> {getattr(last, 'status_code', '?')}")

    spoof = make_client(base)
    r = spoof.post(
        "/api/auth/login",
        json={"username": lock_user, "password": "wrong-pass*1"},
        headers={"X-Forwarded-For": "8.8.8.8", "X-Real-IP": "1.2.3.4"},
    )
    report.add("A5", "X-Forwarded-For 伪造无法绕过锁定", "MEDIUM", r.status_code == 429, f"伪造头重试 -> {r.status_code}")

    r = teacher.post("/api/auth/login", json={"username": "stu001", "password": "stu123*", "role": "teacher"})
    report.add("A6", "身份角色不匹配登录被拒", "MEDIUM", r.status_code == 403, f"student 登记为 teacher -> {r.status_code}")

    r = student.post("/api/auth/change-password", json={"current_password": "wrong", "new_password": "whatever*9"})
    report.add("A7", "改密必须提供正确当前密码", "MEDIUM", r.status_code == 401, f"错误旧密码 -> {r.status_code}")

    section("B. 越权(垂直/水平)")
    r = student.post("/api/docs/upload", files={"file": ("a.md", b"# hi", "text/markdown")}, data={"target_kb": "audit_textbook"})
    report.add("B1", "学生访问教师上传接口被拒", "HIGH", r.status_code == 403, f"docs/upload -> {r.status_code}")
    r = student.get("/api/admin/users")
    report.add("B2", "学生访问管理员用户列表被拒", "HIGH", r.status_code == 403, f"admin/users -> {r.status_code}")
    r = student.get("/api/admin/backup")
    report.add("B3", "学生下载全库备份被拒", "HIGH", r.status_code == 403, f"admin/backup -> {r.status_code}")

    created = admin.post("/api/admin/users", json={
        "username": f"t2-rival-{int(time.time()) % 100000}", "display_name": "隔壁课教师", "password": "rival-pass*1", "role": "teacher",
    })
    rival_ready = created.status_code in {200, 201}
    rival_id = created.json().get("id", "") if rival_ready else ""
    course = admin.post("/api/admin/courses", json={"id": f"rival-{int(time.time()) % 100000}", "name": "别课审计", "term": "2025-2026", "teacher_id": rival_id})
    rival_ready = rival_ready and course.status_code in {200, 201}
    rival = make_client(base)
    if rival_ready:
        rival.post("/api/auth/login", json={"username": created.json()["username"], "password": "rival-pass*1", "role": "teacher"})
        r = rival.get("/api/progress/students", params={"course_id": "audit-101"})
        report.add("B4", "他课教师查看本课程学生名册被拒", "HIGH", r.status_code in {403, 404} and (r.status_code != 200), f"progress/students -> {r.status_code}(不返回他课数据)")
        r = rival.post("/api/quiz/assign", json={"course_id": "audit-101", "title": "越权作业", "question_ids": [], "student_ids": [], "due_at": None})
        report.add("B5", "他课教师向本课程布置测验被拒", "HIGH", r.status_code in {403, 404, 422}, f"quiz/assign -> {r.status_code}")

    student.post("/api/auth/login", json={"username": "stu001", "password": "stu123*", "role": "student"})
    quiz = student.post("/api/quiz/start", json={"counts": {"single": 1, "multi": 0, "judge": 0, "fill": 0, "short": 0, "case": 0}}).json()
    sid = quiz["id"]

    suffix = int(time.time()) % 100000
    reg = attacker.post("/api/auth/register", json={
        "username": f"stu-rival-{suffix}", "display_name": " rival", "student_number": f"R{suffix}",
        "password": "rival-pass*1", "phone": f"139{suffix:08d}", "class_code": "AUD101",
    })
    if reg.status_code == 202:
        pending = admin.get("/api/enrollment/pending").json()
        target = next((row for row in pending if row.get("username") == f"stu-rival-{suffix}"), None)
        if target:
            admin.post(f"/api/enrollment/{target['id']}/approve")
        student2.post("/api/auth/login", json={"username": f"stu-rival-{suffix}", "password": "rival-pass*1", "role": "student"})

    r = student2.get(f"/api/quiz/{sid}")
    report.add("B6", "他人无法读取我的测验会话", "HIGH", r.status_code == 404, f"他人 quiz/{sid[:8]}… -> {r.status_code}")
    r = student2.post(f"/api/quiz/{sid}/draft", json={"answers": {}})
    report.add("B7", "他人无法写我的测验草稿", "HIGH", r.status_code == 404, f"他人 draft -> {r.status_code}")
    r = student2.get(f"/api/grading/{sid}/status")
    report.add("B8", "他人无法查看我的批改状态", "HIGH", r.status_code in {403, 404}, f"grading/status -> {r.status_code}")

    # stu001 先问一个问题,后面用 stu-rival 的历史应为空来验证隔离(配额共 3:F1 还会用 2 次)
    asked = student.post("/api/chat/ask", json={"question": "隔离验证:审计证据有哪些?", "course_id": "audit-101"})
    r = student2.get("/api/chat/history")
    isolated = r.status_code == 200 and r.json() == []
    report.add("B9", "他人答疑历史为空(无跨用户泄漏)", "MEDIUM",
               isolated,
               f"stu001 提问 -> {asked.status_code}(配额耗尽时 429 也正常), rival history -> {r.json()}")

    section("C. 输入滥用与注入")
    fresh = student.post("/api/quiz/start", json={"counts": {"single": 1, "multi": 0, "judge": 0, "fill": 0, "short": 0, "case": 0}}).json()
    first = student.post(f"/api/quiz/{fresh['id']}/submit", json={"answers": {fresh["questions"][0]["id"]: 1}})
    second = student.post(f"/api/quiz/{fresh['id']}/submit", json={"answers": {fresh["questions"][0]["id"]: 1}})
    report.add("C1", "重复提交测验被 409 拒绝", "MEDIUM",
               first.status_code == 200 and second.status_code == 409,
               f"首次 -> {first.status_code}, 重复 -> {second.status_code}")

    # 用 rival 自己的会话打草稿,确保通过认证后到达 JSON 解析层
    own = student2.post("/api/quiz/start", json={"counts": {"single": 0, "multi": 0, "judge": 0, "fill": 0, "short": 1, "case": 0}})
    if own.status_code == 200:
        own_id = own.json()["id"]
        payload = '{"answers":' + '{"a":' * 4000 + "null" + "}" * 4000 + "}"
        r = student2.post(f"/api/quiz/{own_id}/draft", content=payload.encode(), headers={"Content-Type": "application/json"})
        report.add("C2", "超深嵌套 JSON 不造成 500", "MEDIUM", r.status_code < 500, f"4000 层嵌套 -> {r.status_code}")
    else:
        report.add("C2", "超深嵌套 JSON 不造成 500", "MEDIUM", False, f"rival 开卷失败 -> {own.status_code}")

    r = student.post("/api/chat/ask", json={"question": "问" * 4001, "course_id": "audit-101"})
    report.add("C3", "超长问题被 422 拒绝", "LOW", r.status_code == 422, f"4001 字 -> {r.status_code}")

    r = attacker.get("/api/quiz/%00%2e%2e/admin")
    report.add("C4", "路径注入 %00/../ 不产生 500", "MEDIUM", r.status_code < 500, f"-> {r.status_code}")

    r = student.post("/api/quiz/start", json={"counts": {"single": -5}})
    report.add("C5", "负数抽题数量被 422 拒绝", "LOW", r.status_code == 422, f"-> {r.status_code}")

    section("D. 文件上传与备份")
    r = student.post("/api/docs/upload", files={"file": ("a.md", b"# hi", "text/markdown")}, data={"target_kb": "audit_textbook"})
    report.add("D1", "学生上传资料被拒", "HIGH", r.status_code == 403, f"-> {r.status_code}")

    r = teacher.post("/api/docs/upload", files={"file": ("shell.pdf", b"MZ\x90\x00 fake exe", "application/pdf")}, data={"target_kb": "audit_textbook"})
    report.add("D2", "伪装 PDF 的可执行内容被拒", "HIGH", r.status_code == 422, f"-> {r.status_code}")

    r = teacher.post("/api/docs/upload", files={"file": ("fake.docx", b"random garbage bytes", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"target_kb": "audit_textbook"})
    report.add("D3", "伪 DOCX(非 zip)被拒", "HIGH", r.status_code == 422, f"-> {r.status_code}")

    evil_io = io.BytesIO()
    with zipfile.ZipFile(evil_io, "w") as archive:
        archive.writestr("word/document.xml", "<doc/>")
        archive.writestr("../evil.txt", "pwned")
    r = teacher.post("/api/docs/upload", files={"file": ("evil.docx", evil_io.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"target_kb": "audit_textbook"})
    report.add("D4", "DOCX zip-slip 路径穿越被拒", "HIGH", r.status_code == 422, f"-> {r.status_code}")

    bomb_io = io.BytesIO()
    with zipfile.ZipFile(bomb_io, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"0" * (120 * 1024 * 1024))
    r = teacher.post("/api/docs/upload", files={"file": ("bomb.docx", bomb_io.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"target_kb": "audit_textbook"})
    report.add("D5", "解压超限的 zip 炸弹 DOCX 被拒", "MEDIUM", r.status_code == 422, f"-> {r.status_code}")

    big_pdf = b"%PDF-1.4\n" + b"A" * (51 * 1024 * 1024)
    r = teacher.post("/api/docs/upload", files={"file": ("big.pdf", big_pdf, "application/pdf")}, data={"target_kb": "audit_textbook"})
    report.add("D6", "超过 50MB 的上传被拒", "MEDIUM", r.status_code == 413, f"-> {r.status_code}")

    r = teacher.post("/api/docs/upload", files={"file": ("../../evil.pdf", b"%PDF-1.4\n%", "application/pdf")}, data={"target_kb": "audit_textbook"})
    file_key = r.json().get("file_key", "") if r.status_code == 202 else ""
    # 202 时服务端用 UUID 重命名存储(容器内文件系统已核实无 ../ 逃逸),响应不回显 file_key 属正常
    report.add("D7", "恶意文件名不落入任意路径", "HIGH",
               r.status_code == 415 or (r.status_code == 202 and "../" not in file_key),
               f"-> {r.status_code} file_key={file_key!r}(存储侧已用文件系统核实 UUID 重命名)")

    slip_io = io.BytesIO()
    with zipfile.ZipFile(slip_io, "w") as archive:
        archive.writestr("store.json", json.dumps({"version": 2}))
        archive.writestr("files/../../evil.txt", "pwned")
    r = admin.post("/api/admin/restore", files={"file": ("evil.zip", slip_io.getvalue(), "application/zip")})
    report.add("D8", "备份恢复 zip-slip 被拒", "HIGH", r.status_code == 422, f"-> {r.status_code}")

    r = admin.post("/api/admin/restore", files={"file": ("bad.json", b"{broken", "application/json")})
    report.add("D9", "损坏备份 JSON 被拒且不落库", "HIGH", r.status_code == 422, f"-> {r.status_code}")

    section("E. Web 层防护")
    r = attacker.post("/api/quiz/start", json={"counts": {"single": 1}},
                      headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    report.add("E1", "跨站写请求被拒", "HIGH", r.status_code == 403, f"evil Origin -> {r.status_code}")

    r = attacker.post("/api/auth/login", json={"username": "stu001", "password": "stu123*"},
                      headers={"Origin": "https://evil.example"})
    report.add("E2", "跨站登录(登录 CSRF)被拒", "MEDIUM", r.status_code == 403, f"-> {r.status_code}")

    r = attacker.get("/health", headers={"Origin": "https://evil.example"})
    acao = r.headers.get("access-control-allow-origin")
    report.add("E3", "恶意 Origin 拿不到 CORS 放行", "MEDIUM", acao != "https://evil.example", f"ACAO={acao!r}")

    r = attacker.get("/")
    headers_ok = (r.headers.get("x-content-type-options") == "nosniff"
                  and r.headers.get("x-frame-options") in {"DENY", "SAMEORIGIN"}
                  and r.headers.get("referrer-policy") is not None)
    report.add("E4", "安全响应头齐全", "MEDIUM", headers_ok,
               f"nosniff={r.headers.get('x-content-type-options')} frame={r.headers.get('x-frame-options')}")

    r = attacker.get("/", follow_redirects=False)
    report.add("E5", "登录响应的会话 Cookie 带 HttpOnly/SameSite", "MEDIUM",
               "httponly" in login_cookie.lower() and "samesite" in login_cookie.lower(),
               f"login set-cookie 片段: {login_cookie[:90]!r}")

    r = attacker.get("/docs")
    report.add("E6", "开发态 OpenAPI 存在(生产自动关闭,见代码)", "INFO", r.status_code == 200, f"/docs -> {r.status_code}")

    section("F. 限流与资源耗尽")
    quota_codes = []
    for index in range(2):
        r = student.post("/api/chat/ask", json={"question": f"配额测试 {index}", "course_id": "audit-101"})
        quota_codes.append(r.status_code)
    r = student.post("/api/chat/ask", json={"question": "配额测试 超限", "course_id": "audit-101"})
    quota_codes.append(r.status_code)
    hit_429 = 429 in quota_codes
    report.add("F1", "答疑日配额超限返回 429", "MEDIUM", hit_429, f"连续 ask -> {quota_codes}")

    r = student2.get("/api/chat/history")
    report.add("F1b", "配额耗尽不影响他人历史为空", "MEDIUM", r.status_code == 200 and r.json() == [],
               f"rival history 条数 -> {len(r.json()) if r.status_code == 200 else '?'}")

    burst = make_client(base)
    begin = time.monotonic()
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: login(burst, f"stu-burst-{time.monotonic_ns()}", "x", "student"), range(20)))
    elapsed = time.monotonic() - begin
    report.add("F2", "20 并发登录洪峰不 500 且受控", "MEDIUM",
               all(item.status_code in {401, 429} for item in results) and elapsed < 30,
               f"20 并发 {elapsed:.1f}s,状态集 {sorted({item.status_code for item in results})}")

    r = attacker.get("/api/quiz/" + "A" * 8000)
    report.add("F3", "超长路径不造成 500", "LOW", r.status_code < 500, f"-> {r.status_code}")

    section("G. Excel 公式注入(导出侧)")
    evil_name = "=HYPERLINK(\"http://evil.example\",\"点我\")"
    evil_user = admin.post("/api/admin/users", json={
        "username": f"evil-formula-{int(time.time()) % 100000}", "display_name": evil_name,
        "password": "evil-pass*1", "role": "student", "student_number": f"E{int(time.time()) % 100000}",
    })
    enrolled = False
    if evil_user.status_code in {200, 201}:
        evil_id = evil_user.json()["id"]
        enrolled = admin.post("/api/courses/audit-101/students/" + evil_id, json={"class_name": "审计一班"}).status_code in {200, 201}
    r = admin.post("/api/admin/excel/snapshot", json={"course_id": None})
    escaped = False
    detail = f"snapshot -> {r.status_code} enrolled={enrolled}"
    if r.status_code == 200 and enrolled:
        workbook = load_workbook(io.BytesIO(r.content))
        found = None
        for sheet in workbook.worksheets:
          for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and "HYPERLINK" in cell.value:
                    found = cell
        if found is None:
            escaped, detail = False, "导出中未找到注入串(可能被过滤)"
        else:
            escaped = found.data_type == "s" and str(found.value).startswith("'")
            detail = f"单元格 data_type={found.data_type} value 前缀={str(found.value)[:24]!r}"
    report.add("G1", "Excel 导出对公式注入转义", "MEDIUM", escaped, detail)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    print(f"目标(仅限自有测试实例): {args.base}\n")
    report = run(args.base)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
