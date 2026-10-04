# 学期模拟运行手册(Docker 版)

目标:在一台装了 Docker 的电脑上,跑出一个完全真实的"30 名学生 × 30 节课"学期数据:
真实注册、真实审核、真实出题/发布/布置、真实作答与判分、真实教师复核、真实答疑,
最终能直接在浏览器里登录教师/管理员/任意学生账户查证全部数据。

组成文件:

| 文件 | 作用 |
|---|---|
| `docker-compose.sim.yml` | 两个容器:`audit-sim-app`(应用本体,宿主机端口默认 18000)+ `audit-sim-llm`(OpenAI 兼容假模型) |
| `Dockerfile.sim` | 极简镜像(python:3.12-slim + requirements);代码与数据运行时挂载,不进镜像 |
| `sim_llm_server.py` | 假模型:按提示词特征返回答疑 JSON / rubric 评分 / 出题 / 学情报告,零真实 API 消耗 |
| `sim_class_term.py` | 学期模拟器:只通过真实 HTTP API"当人",不直接写任何数据 |
| `sim-data/` | 容器 `/data` 的宿主机映射:store.json、Excel 快照、sim_summary.json 全在这里(.gitignore) |

## 1. 启动实例

```bash
cd server
docker compose -f docker-compose.sim.yml up -d --build   # 首次构建约 2-4 分钟
docker compose -f docker-compose.sim.yml ps              # 两个容器 healthy 即就绪
curl http://127.0.0.1:18000/health                       # {"status":"ok",...,"llm":true}
```

- 宿主机端口默认 18000(避开 8000 的正式服务),要改:`SIM_PORT=8001 docker compose ...`。
- 知识索引:挂载宿主机 `data/knowledge/index.pkl`(本机已存在);新机器若无此文件,
  注释掉 compose 中的该挂载行,答疑会降级为 no_knowledge,其余流程不受影响。
- 重建模拟数据:`docker compose -f docker-compose.sim.yml down` 后删除 `sim-data/` 目录再 up。

## 2. 跑模拟

```bash
docker compose -f docker-compose.sim.yml exec sim-app \
    python sim_class_term.py --base http://127.0.0.1:8000
```

默认参数即 30 人 × 30 讲 × 每讲 3 轮测验(≤5,第 1 轮含主观题)+ 每讲每生 1 次答疑。
常用覆盖:

```bash
python sim_class_term.py --base http://127.0.0.1:8000 \
    --sessions 30 --students 30 --quizzes-per-session 5 \
    --pause-seconds 60        # 两讲之间停 1 分钟,让时间戳更接近真实分布
```

- 从宿主机跑也行(需 httpx):`python sim_class_term.py --base http://127.0.0.1:18000`。
- 随机种子 `--seed` 固定可复现;学生能力 0.50~0.95 决定答题概率,判分永远在服务端。
- 全程预计 20~40 分钟:写请求都要原子落盘 store.json,数据变大后单次保存变慢属正常。
- 重复执行是幂等的:已注册/已发布的内容会复用(注册 409 视为已存在),继续往后跑。

每讲输出一行:`[N/30] 测验 3 轮 · 提交 90 · 答疑 30 · 复核 30 · 累计题目 270`。

## 3. 登录查证(用户要的"找得到账户")

浏览器打开 http://127.0.0.1:18000 :

| 角色 | 账号 / 密码 | 能看到什么 |
|---|---|---|
| 管理员 | admin / admin123* | 全部用户(30 名学生+种子账户)、课程、审计日志、备份 |
| 教师 | teacher01 / teach123* | 学生管理里的 30 人、测验与批改、答疑记录、题库、班级学情 |
| 学生 | sim01 / Sim01cls*(sim02~sim30 同规则) | 自己的测验成绩、错题、掌握度、答疑历史、AI 练习 |

- 学生明细(含逐个密码、能力值、得分)在 `sim-data/sim_summary.json` 的 `students` 字段。
- 期末 Excel 快照在 `sim-data/excel/term_snapshot.xlsx`(14 个 sheet 的全量学情)。
- 命令行快速核账:`docker compose -f docker-compose.sim.yml exec sim-app python -c
  "import json;d=json.load(open('/data/store.json',encoding='utf-8'));print(len(d['users']),len(d['questions']),len(d['quiz_sessions']))"`

## 4. 数据边界与说明

- 容器环境刻意调宽两项:CHAT_DAILY_LIMIT=100000(30 讲压缩在真实一天内跑完,否则撞日配额)、
  REGISTRATION_RATE_LIMIT=100000(避免注册限流);其余配置与开发默认一致。
- 题目内容来自 8 道手工模板(与种子题同风格),按讲次轮换并标注"第 N 讲"章节;
  判分、掌握度、复核、学情报告全部由服务端规则/模型产生,模拟器不伪造任何结论。
- 主观题流程是完整的"AI rubric 初评 → manual_pending → 教师复核 → 计入掌握度"。
- 会落审计日志与 AI 用量(各 5000 条封顶);30 讲跑完 usage_logs 不会溢出。
- 安全提示:这套数据是模拟数据,别把 sim-data 里的 store.json 拷进正式服务。

## 5. 切换真实模型(可选)

模拟实例默认用零消耗假模型。想让 18000 上的答疑/学情问答跑真模型:

```bash
docker compose -f docker-compose.sim.yml -f docker-compose.sim.real.yml up -d
docker compose -f docker-compose.sim.yml up -d   # 切回假模型
```

原理:叠加配置清空假模型三件套并放开 SKIP_ENV_FILE,让容器读取挂载进来的 server/.env 通道池。真实额度消耗见上表单耗。
