<div align="center">

# 🎓 审计智能学伴

**面向审计学课程的 AI 教学平台 · 答疑 · 练习 · 测验 · 批改 · 学情**

基于知识库检索的智能答疑、依据提问生成的专属练习、随堂/布置测验、模型批改与教师复核、
学情报告、题库与图谱管理。单机即可运行,专为课堂并发场景打磨与实测。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-%E5%90%8E%E7%AB%AF-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-95%20passed-brightgreen)](server/tests/test_smoke.py)
[![Security](https://img.shields.io/badge/%E6%94%BB%E9%98%B2%E6%B5%8B%E8%AF%95-57%20%E9%A1%B9%E9%9B%B6%E5%91%BD%E4%B8%AD-6b21a8)](#-测试压测与安全)
[![Load](https://img.shields.io/badge/60%E4%BA%BA%E5%8E%8B%E6%B5%8B-%E5%85%A8%E9%93%BE%E8%B7%AF%E9%80%9A%E8%BF%87-f59e0b)](#-测试压测与安全)
[![License](https://img.shields.io/badge/License-PolyForm%20NC%201.0.0-d97706)](#-许可)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)](#-快速开始)

<img src="docs/screenshots/student-quiz.png" width="49%" alt="学生答题视图"> <img src="docs/screenshots/teacher-grading.png" width="49%" alt="教师批改中心">

*学生答题(草稿自动保存)· 教师批改中心(规则判分 + 模型 rubric 批改)*

</div>

## ✨ 功能特性

| | 功能 | 说明 |
|---|---|---|
| 💬 | **知识答疑** | 本地 BM25 检索(422 份资料 → 20,258 知识块)+ 大模型,回答按「结论 → 准则依据 → 实务案例 → 思政启示 → 思维导图」结构生成并返回来源;一键「生成对应题目」跳转练习 |
| 🧠 | **AI 练习** | 依据你向 AI 提问的内容生成专属练习(单选/多选/判断/填空),提交即判分并给出解析;历史可查可删,**退出登录自动清空** |
| 📝 | **随堂测验** | 按课程/章节/难度随机组卷,六种题型;作答草稿自动保存,刷新/重启均可恢复 |
| 🧮 | **智能批改** | 客观题规则即时判分;简答与案例题由模型按 rubric 逐要点评分,未达标进入教师复核队列 |
| 👥 | **师生协同** | 课堂代码自助注册、教师审核、测验布置与截止追踪、学情报告与 Excel 导出 |
| 🗺️ | **三大图谱** | 知识 / 胜任力 / 问题图谱,节点映射与掌握度追踪 |
| 🔐 | **会话安全** | Argon2 + 失败锁定、256 位会话令牌、同源写保护、审计日志(57 项攻防实测) |
| 🤖 | **多通道模型池** | 学生 / 教师 / 批改通道独立 Key,主备自动切换,并发闸门 + RPM/TPM 令牌桶限流 |

## 🚀 快速开始

```powershell
git clone https://github.com/KROZ-coding/audit-companion.git
cd audit-companion/server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> ,开发种子账号 `stu001 / stu123*`(学生)、`teacher01 / teach123*`(教师)。
Windows 下也可双击根目录 `start_audit_companion.bat` 一键启动。

配置模型:复制 `server/.env.example` 为 `server/.env` 填入 OpenAI 兼容端点;
不配置则答疑 / 批改 / 学情回退规则模式,其余功能不受影响。
重建知识索引:`server/build_knowledge_index.py "审计学知识库目录"`(语料不入库,另行准备)。

<details>
<summary><b>模型通道池配置(分池 + 主备切换 + 限流)</b></summary>

```ini
# server/.env —— Key 通过 api_key_env 指向环境变量,绝不写入 JSON 或提交
LLM_STUDENT_PRIMARY_KEY=sk-xxx
LLM_STUDENT_BACKUP_KEY=sk-xxx
LLM_TEACHER_KEY=sk-xxx
LLM_GRADING_KEY=sk-xxx
LLM_REASONING_EFFORT=none        # 实测单次答疑快 2-4 倍、省约 70% token
LLM_MAX_TOKENS=4000              # 框住离谱提问导致的失控长生成
LLM_PROVIDERS_JSON=[{"name":"student-primary","channel":"student","base_url":"https://relay.example/v1","api_key_env":"LLM_STUDENT_PRIMARY_KEY","model":"glm-5.3-flash","max_concurrency":16,"queue_size":60,"priority":10},{"name":"student-backup","channel":"student","base_url":"https://relay.example/v1","api_key_env":"LLM_STUDENT_BACKUP_KEY","model":"glm-5.3-flash","max_concurrency":16,"queue_size":30,"priority":20},{"name":"teacher-main","channel":"teacher","base_url":"https://relay.example/v1","api_key_env":"LLM_TEACHER_KEY","model":"glm-5.3-flash","max_concurrency":4,"queue_size":10,"priority":10}]
```

主通道失败自动切换备用(真实演练验证),429/5xx 冷却并按 Retry-After 等待;
`rpm_limit` / `tpm_limit` 按供应商真实额度配置。逐通道验证:`python verify_llm_channels.py`。

</details>

## 🧪 测试、压测与安全

```powershell
cd server
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_smoke.py"   # 95 项测试
.\.venv\Scripts\python.exe verify_llm_channels.py                             # 通道池真实验证
.\.venv\Scripts\python.exe load_test.py --users 60 --scenario mixed           # 60 人混合压测
.\.venv\Scripts\python.exe load_test.py --users 60 --scenario practice --fake-llm  # 零 API 消耗
.\.venv\Scripts\python.exe security_test.py --base http://127.0.0.1:8020      # 应用层攻击自查
.\.venv\Scripts\python.exe security_net_test.py                               # 网络层攻击模拟
```

**性能实测**(60 人真实模型压测):登录 p50 1.7s · 答疑 60/60 成功(单次 16-43s)·
客观测验 p50 0.1s · 主观批改 10/10 完成。**安全实测**(Ubuntu 24.04 容器靶机):
57 项攻击面测试零命中 —— 字典爆破、会话碰撞/固定、请求走私、Slowloris、SSRF、
zip-slip、时延侧信道、Host 投毒等均被防御;生产模式 Host 白名单与演示密码护栏实测生效。

> 部署到 Ubuntu 服务器的完整注意事项(目录结构、生产护栏、nginx 慢速攻击防护、
> 部署检查清单)见 [DEPLOYMENT_NOTES.md](DEPLOYMENT_NOTES.md)。

## 📁 目录结构

```
audit-companion/
├── server/                    # FastAPI 后端
│   ├── app/                   # 路由、服务、模型、存储
│   ├── tests/                 # 95 项冒烟与单元测试
│   ├── security_test.py       # 应用层攻击自查(41 项)
│   ├── security_net_test*.py  # 网络层攻击模拟(16 项)
│   ├── verify_llm_channels.py # 模型通道真实验证
│   ├── load_test.py           # 可复用压测(practice 场景 + 假模型)
│   └── build_knowledge_index.py
├── docs/screenshots/          # 界面截图
├── 智能学伴演示页.html          # 单文件前端(学生/教师/管理员)
├── 课程思政地图.html            # 思政地图(vendor/d3 本地化)
├── DEPLOYMENT_NOTES.md        # 服务器部署技术注意报告
└── start_audit_companion.*    # 一键启动脚本
```

## 🗺️ Roadmap

- [ ] 按供应商真实 RPM 限额配置削峰参数
- [ ] 多进程 / 多实例部署的 SQLite 迁移
- [ ] 浏览器级退出与恢复作答自动化测试

## 📄 许可

本项目采用 [PolyForm Noncommercial 1.0.0](LICENSE) 许可发布:允许查看、学习、课堂教学、
科研与自由修改分发(须保留版权与许可声明);**禁止任何商业牟利用途**,商用需单独授权。
教育机构、公益组织与政府机构的内部教学属于许可内的非商业目的。
本节为简要说明,完整条款以 [LICENSE](LICENSE) 原文为准。

---

<div align="center">

**审计智能学伴** · 经管数智审计课程建设配套 · 单机演示与校内教学场景 · PolyForm NC 1.0.0

</div>
