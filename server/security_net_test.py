# -*- coding: utf-8 -*-
"""网络层攻击模拟(仅对自有本地测试实例运行,强度受限)。

覆盖:登录字典爆破与锁定/时延侧信道、会话 ID 碰撞与会话固定、
Host 头攻击与请求走私探测、慢速连接资源耗竭(Slowloris-lite)。
"""
import socket
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = "http://127.0.0.1:8020"

# 常见弱密码字典(教学用精简版,来源为历年泄露榜常见形态)
WORDLIST = [
    "123456", "password", "12345678", "qwerty", "abc123", "monkey", "letmein",
    "dragon", "111111", "baseball", "iloveyou", "trustno1", "sunshine", "master",
    "welcome", "shadow", "ashley", "football", "jesus", "michael", "ninja",
    "mustang", "password1", "admin123", "stu123", "stu001", "student", "school",
    "audit123", "shenji123", "zhangwei", "zhangwei123", "teacher", "teacher123",
    "beijing2026", "qwerty123", "1q2w3e4r", "zaq12wsx", "p@ssw0rd", "Passw0rd",
    "123123", "654321", "666666", "888888", "a123456", "123qwe", "qwe123",
    "1qaz2wsx", "abcd1234", "test123", "root123", "pass123", "qazwsx", "121212",
]

results = []


def record(test_id: str, name: str, severity: str, passed: bool, detail: str) -> None:
    mark = "OK  " if passed else "HIT "
    results.append((test_id, name, severity, passed, detail))
    print(f"[{mark}] {test_id:<5} ({severity:<6}) {name} :: {detail}", flush=True)


def section(title: str) -> None:
    print(f"\n---- {title} ----", flush=True)


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=15, trust_env=False)

    section("A. 登录字典爆破与锁定")
    # A1: 对固定用户连续打字典,第 5 次应锁定,之后正确密码也应被拒
    victim = "stu001"
    codes = []
    for password in WORDLIST[:8]:
        response = client.post("/api/auth/login", json={"username": victim, "password": password, "role": "student"})
        codes.append(response.status_code)
    locked_after = codes.index(429) + 1 if 429 in codes else None
    record("A1", "连续错密码触发账号锁定", "HIGH", locked_after is not None and locked_after <= 5,
           f"前8次状态 {codes}, 第{locked_after}次锁定")

    correct = client.post("/api/auth/login", json={"username": victim, "password": "stu123*", "role": "student"})
    record("A2", "锁定期间正确密码也被拒", "HIGH", correct.status_code == 429, f"锁定后正确密码 -> {correct.status_code}")

    # A3: 多用户低速率分布式爆破(每用户只试 4 次,不触发锁定),验证锁定阈值下的暴露面
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(client.post, "/api/auth/login",
                        json={"username": f"stu{i:03d}", "password": WORDLIST[i % len(WORDLIST)], "role": "student"})
            for i in range(1, 6)
        ]
        slow_codes = [future.result().status_code for future in futures]
    # stu001 已锁定会 429;其余不存在用户 401
    record("A3", "低速率绕锁定策略只能得到 401/429(无信息泄漏)", "MEDIUM",
           all(code in {401, 429} for code in slow_codes), f"5 个账号各 1 次状态 {slow_codes}")

    # A4: 用户名变体绕过锁定尝试
    variants = ["STU001", " stu001", "stu001 ", "Stu001"]
    variant_codes = [client.post("/api/auth/login", json={"username": v, "password": "wrong-pass*1", "role": "student"}).status_code for v in variants]
    # stu001 已锁定;变体要么命中同一账号(429)要么是不存在账号(401),都不产生成功登录
    record("A4", "用户名大小写/空白变体无法绕过锁定", "MEDIUM", all(code in {401, 429} for code in variant_codes),
           f"变体状态 {variant_codes}(401=变体被规范化为不存在账号,429=命中已锁定的本体)")

    # A5: 时延侧信道——存在用户 vs 不存在用户的拒绝耗时差
    def time_login(username: str) -> float:
        begin = time.monotonic()
        client.post("/api/auth/login", json={"username": username, "password": "WrongGuess*9", "role": "student"})
        return time.monotonic() - begin

    existing = [time_login("stu001") for _ in range(12)]
    missing = [time_login(f"ghost-{i}") for i in range(12)]
    gap_ms = (statistics.median(existing) - statistics.median(missing)) * 1000
    record("A5", "存在/不存在用户的拒绝耗时差(用户名枚举侧信道)", "MEDIUM", abs(gap_ms) < 40,
           f"存在用户中位 {statistics.median(existing)*1000:.0f}ms vs 不存在 {statistics.median(missing)*1000:.0f}ms,差 {gap_ms:.0f}ms"
           f"{'——可据此枚举用户名' if gap_ms > 40 else ''}")

    section("B. 会话 ID 碰撞与固定")
    # B1: 随机抽 3000 个伪造 sid 访问受保护端点
    import secrets as _secrets
    forged = 0
    for _ in range(3000):
        guess = _secrets.token_urlsafe(32)
        if client.get("/api/auth/me", cookies={"sid": guess}).status_code != 401:
            forged += 1
    record("B1", "3000 次随机会话 ID 碰撞全部失败", "HIGH", forged == 0, f"命中 {forged}/3000;sid 为 256 位 token_urlsafe,2^256 空间碰撞不可行")

    # B2: 会话固定——攻击者预设 sid,登录后服务端必须换发新 sid
    evil_sid = "attacker-fixed-session-id"
    response = client.post("/api/auth/login", json={"username": "teacher01", "password": "teach123*", "role": "teacher"},
                           headers={"Cookie": f"sid={evil_sid}"})
    issued = response.headers.get("set-cookie", "")
    record("B2", "登录强制换发新会话 ID(防会话固定)", "HIGH", "sid=" in issued and evil_sid not in issued,
           f"服务端下发新 sid:{issued.split(';')[0][:30]}…")
    client.post("/api/auth/logout")

    section("C. Host 头与请求走私探测")
    # C1: 恶意 Host 头(开发态通配放行属预期,但不得反射/中毒)
    evil_hosts = ["evil.example", "internal.corp:8080", "127.0.0.1:6379"]
    outcomes = []
    for host in evil_hosts:
        response = client.get("/health", headers={"Host": host})
        outcomes.append(response.status_code)
    record("C1", "恶意 Host 头不造成异常(生产由 ALLOWED_HOSTS 白名单拦截)", "MEDIUM",
           all(code in {200, 400, 404} for code in outcomes), f"状态 {outcomes}")

    # C2: 绝对 URI 形态请求
    raw = socket.create_connection(("127.0.0.1", 8020), timeout=5)
    raw.sendall(b"GET http://evil.example/health HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n")
    status_line = raw.recv(1024).split(b"\r\n")[0].decode(errors="replace")
    raw.close()
    record("C2", "绝对 URI 请求被正常处理或拒绝", "MEDIUM", any(code in status_line for code in ("200", "400", "404")),
           f"响应行 {status_line!r}")

    # C3: 双 Content-Length / Transfer-Encoding 冲突(走私探测,只验证不崩溃)
    smuggle_variants = [
        b"POST /api/auth/login HTTP/1.1\r\nHost: 127.0.0.1:8020\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{}",
        b"POST /api/auth/login HTTP/1.1\r\nHost: 127.0.0.1:8020\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: identity\r\nConnection: close\r\n\r\n0\r\n\r\n",
    ]
    smuggle_status = []
    for payload in smuggle_variants:
        try:
            sock = socket.create_connection(("127.0.0.1", 8020), timeout=5)
            sock.sendall(payload)
            line = sock.recv(1024).split(b"\r\n")[0].decode(errors="replace")
            sock.close()
            smuggle_status.append(line)
        except (socket.timeout, ConnectionError) as error:
            smuggle_status.append(f"conn:{type(error).__name__}")
    healthy_after = client.get("/health").status_code == 200
    record("C3", "CL/TE 冲突请求被 h11 拒绝且服务不崩", "MEDIUM",
           healthy_after and all("500" not in line for line in smuggle_status),
           f"响应行 {smuggle_status},事后 /health={'200' if healthy_after else '异常'}")

    section("D. 慢速连接耗竭(Slowloris-lite, 20 连接/30 秒封顶)")
    socks = []
    try:
        for _ in range(20):
            try:
                sock = socket.create_connection(("127.0.0.1", 8020), timeout=3)
                sock.sendall(b"GET /api/auth/me HTTP/1.1\r\nHost: 127.0.0.1:8020\r\nX-Slow: ")
                socks.append(sock)
            except OSError:
                break
        time.sleep(20)
        during = client.get("/health").status_code
        begin = time.monotonic()
        during_login = client.post("/api/auth/login", json={"username": "stu001", "password": "stu123*", "role": "student"}).status_code
        login_latency = time.monotonic() - begin
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
    record("D1", "20 条半开连接期间服务保持可用", "MEDIUM", during == 200 and during_login in {200, 429},
           f"/health={during}, 登录={during_login}, 登录耗时 {login_latency:.2f}s, 慢连接数 {len(socks)}")

    print(f"\n========== 结论 ==========", flush=True)
    hits = [item for item in results if not item[3] and item[2] in {"HIGH", "MEDIUM"}]
    print(f"检查项 {len(results)} · 命中(中高危) {len(hits)}")
    for test_id, name, severity, _, detail in hits:
        print(f"  [{severity}] {test_id} {name}: {detail}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
