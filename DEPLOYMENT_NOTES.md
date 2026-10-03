# 部署技术注意事项报告

> 适用版本:2026-10-03(main @ 5a86b11 之后)
> 本报告所有结论均来自实测,不是推测:Ubuntu 24.04.5 容器(Python 3.12.3)部署验证、
> 57 项攻击面测试(41 项应用层 + 16 项网络层)、60 人规模多轮压测、10 次真实模型调用测量。
> 生产部署前逐条对照检查。

## 1. 目录与运行环境

1. **目录结构不可拆分**。应用按相对路径在 `server/` 的上一级查找 `智能学伴演示页.html`、
   `课程思政地图.html` 和 `vendor/`。只把 `server/` 单独拷到服务器会导致根路由 `/` 直接
   RuntimeError 断连(实测复现)。正确布局:

   ```
   /srv/audit-companion/          # 仓库根(或等价结构)
   ├── server/                    # uvicorn 工作目录
   └── 智能学伴演示页.html 等       # 必须与 server 同级
   ```

2. **Python 版本**:3.11-3.13 均验证可用(Ubuntu 24.04 自带 3.12)。虚拟环境必须建在
   `server/.venv`,启动脚本按此路径查找。

3. **知识索引**:语料不入库。首次部署需要在服务器上重建索引:
   `server/.venv/bin/python build_knowledge_index.py "审计学知识库目录"`,
   产物约 59MB,默认落在 `DATA_DIR/knowledge/index.pkl`。索引缺失时答疑自动降级为
   无检索模式(`no_knowledge`),不报错但回答无来源。

## 2. 生产启动护栏(缺一拒绝启动)

`APP_ENV=production` 时,以下条件缺任何一个进程直接退出(实测全部触发过):

| 护栏 | 说明 |
|---|---|
| `PERSISTENCE_ENABLED=true` | 否则数据不落盘 |
| `COOKIE_SECURE=true` | Cookie 必须走 HTTPS |
| `ALLOWED_HOSTS=你的域名` | Host 头白名单,实测恶意 Host/裸 IP 直连均被 400 拒绝 |
| 三个演示密码已改 | admin/teacher01/stu001 的种子密码(admin123* 等)必须全部修改 |

**/health 与 /ready 不是一回事**:/health 只表示进程活着;/ready 在生产模式额外要求
配置 MaxKB(`MAXKB_URL` + Key),未配置时返回 503。负载均衡探活请用 /health,
除非你确实配了 MaxKB。

## 3. 模型通道与额度管理

1. **通道池配置**在 `server/.env` 的 `LLM_PROVIDERS_JSON`(示例见 `.env.example`)。
   Key 通过 `api_key_env` 指向环境变量,只存本机文件,绝不提交仓库。
   换 Key 或换通道后运行 `verify_llm_channels.py` 逐通道真实验证,并做一次备用切换演练。

2. **实测性能基线**(本机 4 通道、学生通道 2×16 并发):
   - 单次答疑:16-43 秒(已启用 `LLM_REASONING_EFFORT=none` 关闭隐形思考)
   - 60 人混合压测:登录 p50 1.7s、答疑 60/60 成功、客观测验 p50 0.1s
   - 60 人练习生成(假模型):p50 1.7s,整批 28s

3. **额度按窗口计的服务商**(如"每 Key 每 5 小时 N token"):压测会一次性吃掉大量窗口
   额度,正式压测前确认窗口余量。课堂容量按实测单耗折算:答疑一次约 1250 token、
   练习生成一次约 3200 token。

4. **瞬时限流是主要瓶颈,不是总量**:60 并发高峰曾撞出 9 次 429 + 5 次冷却失败
   (修复后冷却窗口内会等待重试一次,仍可能失败)。对策:拿到服务商真实 RPM 限额后,
   在通道池 JSON 里配置 `rpm_limit` 削峰;课堂场景引导学生错峰提问。

5. **`LLM_REASONING_EFFORT=none` 是本次最大提速点**:glm-5.3-flash 经中继默认带隐形
   思考输出(可见 1400 字、计费 3000-8800 token),关闭后单次快 2-4 倍、省约 70%
   token。副作用是思维导图偶发省略(提示词已强化为硬性要求)。Ubuntu 部署时记得
   把本机 `.env` 的这一行带上。

6. **`LLM_MAX_TOKENS=4000` 已默认启用**:防止离谱提问让模型失控长跑(实测曾把单次
   拖到 266 秒)。正常回答约 834 token、练习约 3000 token,均不受影响。

## 4. 安全防御现状与部署职责边界

### 应用内已验证的防御(测试通过,无需额外动作)

- 登录:Argon2 + 5 次失败锁定(锁定期间正确密码也拒绝)+ 用户名变体归一化 + 时延
  侧信道抹平(存在/不存在用户拒绝耗时差 <1ms,无法枚举用户名)
- 会话:256 位随机 sid(3000 次碰撞零命中)、登录强制轮换(防会话固定)、
  HttpOnly/SameSite=Lax、退出即失效
- 输入:Pydantic 严格 schema,10MB 载荷 0.3s 内 422;4000 层嵌套 JSON 不崩
- 文件:上传按魔数/zip 结构校验,UUID 重命名落盘,zip-slip/炸弹全拒
- 备份恢复:`store.json`+`files/` 白名单,多形态穿越全部 422
- 注入:Excel 公式(=+-@ 前缀自动转义)、XSS(输出全转义)、路径注入不达敏感面
- Web 层:同源写保护(跨站写/登录 CSRF 均 403)、安全响应头齐全、恶意 Host 不反射

### 部署时必须由运维层补齐的(应用层防不住或不该它管)

1. **慢速攻击(Slowloris 类)**:单连接每 15 秒发一个字节挂几小时,uvicorn 应用层
   无法防。nginx 已在 `nginx.conf.example` 配好 `client_header_timeout 10s` 等四项
   超时,照抄即可;**不要把 uvicorn 直接暴露公网**。
2. **IP 级限流**:失败锁定按用户名计,分布式低速率爆破只能靠 429 硬扛。nginx 的
   `limit_req zone=login`(30r/m)补上 IP 维度,示例已配。
3. **TLS**:所有流量走 HTTPS(仓库示例含 80→443 跳转), COOKIE_SECURE 依赖它。
4. **主机层**:ufw 只放 80/443;SSH 密钥登录禁密码;Fail2ban 兜底 SSH 爆破;
   应用用非 root 用户跑(DATA_DIR 与 store.json 归属运行用户,权限 750)。
   `index.pkl` 被篡改等同于代码执行,务必锁目录权限。
5. **备份**:`DATA_DIR/store.json` 是全部业务数据;`/api/admin/backup?format=archive`
   可导出含资料文件的 ZIP,建议每日 cron 调用并存到独立介质。

## 5. 架构上限(改部署方式前必须知道)

1. **单进程 JSON 文件存储**:单机单实例稳定;**多进程/多实例前必须先迁移数据库**,
   否则各进程内存状态互不感知,store.json 互相覆盖。
2. **批改在请求内同步完成**:60 人同时交主观题时,批改请求会占满批改通道并发槽
   (已独立成 grading 通道),不影响答疑;若再扩规模,批改应改为后台队列。
3. **单机演示与校内课堂是设计目标**:面向公网互联网产品形态(多租户、水平扩展、
   对象存储)需要另行架构评审。

## 6. 部署检查清单(按序执行)

```
[ ] 完整仓库结构上传(含前端页面,server 与页面同级)
[ ] python3 -m venv server/.venv && 安装 requirements.txt
[ ] cp server/.env.example server/.env,填入通道池 Key(文件权限 600)
[ ] 配置 MAXKB_URL/Key(/ready 在生产模式要求它)
[ ] build_knowledge_index.py 重建索引
[ ] 改掉三个演示密码(admin 登录后改密 + 重置教师/学生)
[ ] ALLOWED_HOSTS=域名,PERSISTENCE_ENABLED=true,COOKIE_SECURE=true
[ ] verify_llm_channels.py 四通道全绿 + 备用切换演练一次
[ ] systemd 单元(非 root 用户,EnvironmentFile=server/.env)
[ ] nginx:443+TLS+示例配置(含慢速超时与限流),uvicorn 仅 127.0.0.1
[ ] ufw 收口 80/443,Fail2ban 启用
[ ] 验证:/health 200、/docs 404(生产关闭)、恶意 Host 400、旧演示密码 401
[ ] 备份 cron 上线
```

## 7. 测试资产复用

| 工具 | 用途 |
|---|---|
| `server/tests/test_smoke.py` | 95 项功能与回归测试(`python -m unittest discover -s tests -p "test_smoke.py"`) |
| `server/security_test.py` | 41 项应用层攻击自查(仅对自有测试实例) |
| `server/security_net_test.py` / `security_net_test2.py` | 16 项网络层攻击模拟(爆破/碰撞/走私/慢速/SSRF) |
| `server/verify_llm_channels.py` | 模型通道真实验证与备用切换演练 |
| `server/load_test.py` | 压测(60 人混合/objective/subjective/ask/practice 场景,--fake-llm 零 API 消耗,--attach 附着已有实例) |

> 所有安全测试工具仅限对你拥有或获授权的系统运行。
