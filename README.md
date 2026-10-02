# 审计智能学伴

面向审计学课程的 AI 教学平台：学生答疑（本地 BM25 知识检索 + 来源标注）、随堂与布置测验、
模型批改与教师复核、学情报告、题库与三大图谱管理。FastAPI 后端 + 单文件演示页，单机即可运行。

## 仓库结构

| 路径 | 说明 |
|---|---|
| `server/` | FastAPI 后端（`app/`、`tests/`、索引与运维脚本），详见 [server/README.md](server/README.md) |
| `智能学伴演示页.html` | 单文件前端，学生 / 教师 / 管理员三角色 |
| `课程思政地图.html` | 思政地图页面（`vendor/d3.min.js` 本地化，离线可用） |
| `start_audit_companion.ps1/.bat` | 一键启动后端并打开页面 |
| `审计学知识库/` | BM25 语料（约 750MB，不入库；`server/build_knowledge_index.py` 重建索引） |

## 快速开始

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000/ ，开发种子账号见 `server/README.md`。
配置模型复制 `server/.env.example` 为 `server/.env`；未配置时答疑/批改/学情自动回退规则模式。

## 测试与压测

```powershell
cd server
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_smoke.py"   # 后端全量测试
.\.venv\Scripts\python.exe verify_llm_channels.py                             # 通道池逐端点真实验证
.\.venv\Scripts\python.exe load_test.py --users 60 --scenario mixed           # 60 人混合压测
```

`load_test.py` 支持场景选择（mixed / objective / subjective / ask）、`--no-knowledge` 隔离模型链路、
`--json` 输出报告；会自动起独立临时数据的 uvicorn 实例并在结束后清理。

## 模型通道

`server/.env` 的 `LLM_PROVIDERS_JSON` 支持 `student / teacher / grading / shared` 通道分池、
主备切换（真实演练通过）、每端点并发上限与 RPM/TPM 令牌桶限流。Key 通过 `api_key_env` 指向
环境变量，只保存在本机 `.env`，绝不提交。
