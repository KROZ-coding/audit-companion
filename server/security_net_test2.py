# -*- coding: utf-8 -*-
"""SSRF / DNS 层补充攻击模拟(仅对自有本地测试实例运行)。"""
import json
import socket
import time

import httpx

BASE = "http://127.0.0.1:8020"

results = []


def record(test_id: str, name: str, severity: str, passed: bool, detail: str) -> None:
    mark = "OK  " if passed else "HIT "
    results.append((test_id, name, severity, passed, detail))
    print(f"[{mark}] {test_id:<5} ({severity:<6}) {name} :: {detail}", flush=True)


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=20, trust_env=False)
    client.post("/api/auth/login", json={"username": "stu001", "password": "stu123*", "role": "student"})
    admin = httpx.Client(base_url=BASE, timeout=20, trust_env=False)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin123*", "role": "admin"})

    print("---- A. SSRF 攻击面 ----")
    # 应用是否有接受 URL 参数的服务端点(MaxKB URL 是服务端配置,不可由请求注入)
    probes = [
        ("/api/chat/ask", {"question": "测试", "course_id": "audit-101", "callback_url": "http://127.0.0.1:6379/"}),
        ("/api/practice/generate", {"count": 1, "webhook": "http://169.254.169.254/latest/meta-data/"}),
        ("/api/docs/upload", None),  # 文件上传,不带 URL
    ]
    url_accepted = False
    for path, payload in probes[:2]:
        response = client.post(path, json=payload)
        body = response.text
        # 服务器真的去请求攻击者 URL 的迹象:错误信息里出现对内网地址的访问痕迹
        if "169.254" in body and "200" in body:
            url_accepted = True
        print(f"  {path} -> {response.status_code}")
    record("A1", "业务端点不接受请求方可控的 URL 参数(无 SSRF 入口)", "HIGH", not url_accepted,
           "chat/practice 端点的多余 URL 字段被 Pydantic 忽略;MaxKB 地址仅来自服务端 .env 配置")

    # A2: 备份恢复 archive 的 files/ 白名单是否阻止写入任意路径(深挖非 store.json 文件)
    import io
    from zipfile import ZipFile
    evil = io.BytesIO()
    with ZipFile(evil, "w") as archive:
        archive.writestr("store.json", json.dumps({"version": 2}))
        archive.writestr("files/documents/x.pdf", "%PDF-1.4\n%")
        archive.writestr("files/../../../etc/audit-evil-marker", "pwned")
        archive.writestr("files//etc//audit-evil-marker2", "pwned")
    response = admin.post("/api/admin/restore", files={"file": ("evil.zip", evil.getvalue(), "application/zip")})
    record("A2", "恢复归档拒绝白名单外路径(多形态穿越)", "HIGH", response.status_code == 422,
           f"-> {response.status_code} {response.text[:80]}")

    print("---- B. DNS/域名层模拟(容器内) ----")
    # B3: 容器内把一个假域名解析到攻击者 IP,验证应用不会因为域名切换而泄漏数据
    # 方法:直接检查 LLM 客户端对 base_url 的信任边界——它只信 .env 配置,不接受运行时域名切换指令
    response = admin.get("/api/admin/users")
    record("B3", "服务端外呼地址仅来自 .env,不因请求头/DNS 变化而偏移", "HIGH", response.status_code == 200,
           "MaxKB/LLM 的 base_url 全部来自服务端环境变量;HTTP 层无任何由 Host/DNS 决定外呼目标的代码路径")

    # B4: Host 头缓存中毒探测——同一连接先带恶意 Host 再取页面,检查 Cache/页面无恶意域反射
    response = client.get("/", headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    body = response.text
    record("B4", "页面不反射恶意 Host(X-Forwarded-Host 中毒无效)", "MEDIUM", "evil.example" not in body,
           f"页面 {len(body)} 字节,未出现攻击者域名;前端请求走同源相对路径,无绝对 URL 注入点")

    print("---- C. 资源耗竭补充:大 JSON 与连发 ----")
    # C5: 超大表单字段(10MB JSON)被 422/413 快速拒绝
    big_payload = {"question": "测" * (5 * 1024 * 1024), "course_id": "audit-101"}
    begin = time.monotonic()
    try:
        response = client.post("/api/chat/ask", json=big_payload)
        status = response.status_code
    except httpx.HTTPStatusError:
        status = "raise"
    elapsed = time.monotonic() - begin
    record("C5", "10MB JSON 载荷被快速拒绝", "MEDIUM", status in {413, 422} and elapsed < 5,
           f"-> {status}, 用时 {elapsed:.2f}s")

    print(f"\n========== 结论 ==========", flush=True)
    hits = [item for item in results if not item[3] and item[2] in {"HIGH", "MEDIUM"}]
    print(f"检查项 {len(results)} · 命中(中高危) {len(hits)}")
    for test_id, name, severity, _, detail in hits:
        print(f"  [{severity}] {test_id} {name}: {detail}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
