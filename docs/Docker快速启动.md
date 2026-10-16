# Docker 快速启动指南

> 目标:双击 `启动Docker服务.bat`(或一条 compose 命令)把正式服务跑进 Docker,
> 并快速打通 Cloudflare 公网隧道。不依赖 venv,适合换机演示、快速拉起、
> 或宿主机 Python 环境被占用时并行开一个实例。

## 0. 前置条件

| 条件 | 说明 |
|---|---|
| Docker Desktop 在跑 | 脚本会自检,守护进程没起会直接提示 |
| `server/.env` 存在 | 模型 Key 配置(容器内 `SKIP_ENV_FILE` 未设,走 `.env` 正常加载);没有也能跑,AI 功能降级规则模式 |
| `server/data/knowledge/index.pkl` | 知识索引(约 59MB,只读挂载);没有则答疑降级 `no_knowledge`,不影响其余流程 |
| 隧道(可选) | 宿主机 cloudflared + `%TEMP%\cf_tunnel_token.txt`(现成);或设 `CF_TUNNEL_TOKEN` 走容器隧道 |

## 1. 一键启动

双击仓库根 **`启动Docker服务.bat`**,等价于:

```bash
cd server
docker compose -f docker-compose.quick.yml up -d --build   # 首次构建约 2-4 分钟
curl http://127.0.0.1:8000/health                          # {"status":"ok",...}
```

脚本行为(与 `启动服务.bat` 同款约定):

- 幂等可重跑:应用已健康、公网已通、cloudflared 已在跑,都自动跳过;
- 启动后轮询 `/health` 最长 60 秒,最后核验本地与公网两个端点;
- 失败时给出对应修复提示(守护进程未起、端口被占、token 缺失等)。

换端口:`QUICK_PORT=8001 docker compose -f docker-compose.quick.yml up -d --build`
(脚本默认 8000,与 Cloudflare 面板入口 `localhost:8000` 对齐,改了端口隧道的
面板 ingress 也要跟着改)。

## 2. 编排说明(server/docker-compose.quick.yml)

| 项 | 值 | 说明 |
|---|---|---|
| 容器 | `audit-quick-app` | uvicorn 直跑,host 8000 → 容器 8000 |
| 镜像 | `Dockerfile.sim` 复用 | python:3.12-slim + requirements,代码运行时挂载,不进镜像 |
| 代码 | `..:/app:ro` 只读挂载 | 仓库根整个挂进去(前端 HTML 与 `vendor/` 必须与 server 同级,结构见 DEPLOYMENT_NOTES.md 第 1 节) |
| 配置 | `..` 挂载含 `server/.env` | Key 只存本机文件,绝不进镜像、不入库 |
| 数据 | 命名卷 `quick-data` → `/data` | store.sqlite3、documents、excel 全在卷里;与 venv 版 `server/data/` 完全隔离,两边可同时跑 |
| 知识索引 | `data/knowledge/index.pkl:ro` | 缺文件时 compose 会报挂载错,注释掉该行即降级 no_knowledge |

关键取舍:数据用命名卷而不是宿主机目录,因为 SQLite WAL 在 Windows 绑定挂载上
文件锁语义不可靠(挂载跑过 store.json 时代的模拟数据没问题,SQLite 必须避开)。
进容器看数据:

```bash
docker exec audit-quick-app python -c "import sqlite3;c=sqlite3.connect('/data/store.sqlite3');print(c.execute('select count(*) from users').fetchone())"
```

首次部署新机器需要重建知识索引(产物必须落在 `server/data/knowledge/index.pkl`
才会被挂载)。宿主机有 Python 就直接跑:

```bash
cd server && python build_knowledge_index.py "../审计学知识库"
```

没有宿主机 Python 时用镜像跑(cmd,在 `server/` 目录执行;直接写入宿主机 `server/data/`):

```bat
docker run --rm -v "%cd%\..:/app" -w /app/server audit-companion-quick-app:latest python build_knowledge_index.py /app/审计学知识库
```

## 3. 隧道:两种模式

默认 **host 模式**(与现网拓扑一致):容器把 8000 发布到宿主机,隧道继续由
宿主机 cloudflared 承担——Cloudflare 面板里公网入口指向 `localhost:8000`,
token 模式下 CLI 改不了 ingress,所以这条链路零面板改动即可复用。

```bash
"%ProgramFiles(x86)%\cloudflared\cloudflared.exe" tunnel --no-autoupdate run --token <token>
```

可选 **container 模式**(全容器化):

```bash
set CF_TUNNEL_TOKEN=<token>
docker compose -f server/docker-compose.quick.yml --profile tunnel up -d
```

注意:面板托管 token 的 ingress(默认 `localhost:8000`)优先级高于容器里的
任何 `--url` 参数,必须先到 Cloudflare Zero Trust 面板把公网入口改成
`http://audit-companion-quick-app-1:8000`(compose 网络内容器名),否则隧道
会指向容器外的宿主机 localhost,打不进容器。改完入口再启用此模式。

## 4. 常用运维

```bash
docker logs audit-quick-app                              # 应用日志
docker compose -f server/docker-compose.quick.yml ps     # 健康状态
docker compose -f server/docker-compose.quick.yml down   # 停止(数据保留在卷)
docker volume rm audit-companion-quick_quick-data        # 彻底清数据(慎用)
QUICK_PORT=8001 docker compose -f server/docker-compose.quick.yml up -d  # 换端口并行
```

- 与 venv 版互不干扰:数据一个在命名卷、一个在 `server/data/`,端口同为 8000
  时只能起一个(端口冲突);`QUICK_PORT` 错开即可并行。
- 与 `docker-compose.sim.yml`(学期模拟,端口 18000、sim-data 卷)完全隔离。
- 数据备份:容器内 `/data/store.sqlite3` 是单文件,`docker cp audit-quick-app:/data/store.sqlite3 .`
  即可拷出;正式备份仍走 `/api/admin/backup?format=archive`(见 DEPLOYMENT_NOTES.md 第 4 节)。
- 改了代码或 `.env`:重新 `up -d --build` 即生效(env 是进程启动时读取的)。

## 5. 故障排查

| 现象 | 处理 |
|---|---|
| `docker info` 失败 / 脚本报 daemon 不可达 | 先启动 Docker Desktop,等鲸鱼图标安静后再重跑 |
| 首次构建拉镜像慢 | `Dockerfile.sim` 已配 `docker.m.daocloud.io` 镜像源前缀 |
| 容器反复重启 | `docker logs audit-quick-app` 看栈;生产护栏(`APP_ENV=production` 缺 `ALLOWED_HOSTS` 等)会拒绝启动 |
| /health 200 但公网不通 | 隧道没起或面板入口不对;重跑脚本会自动补起宿主机 cloudflared |
| 8000 被占 | `QUICK_PORT` 换端口,或 `netstat -ano | findstr :8000` 找占用进程处理 |
| 答疑显示无来源 | 知识索引没挂上,按第 2 节重建并放入 `server/data/knowledge/` |
