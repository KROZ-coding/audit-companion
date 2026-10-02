# 审计智能学伴后端

这是对应 `智能学伴演示页.html` 和 `后端实现规划.md` 的 FastAPI 服务端基础实现。

当前状态：

- 登录、HttpOnly Cookie 会话、失败锁定、角色拦截和三组演示账号可直接运行。
- 管理员可以创建、编辑和删除课程，维护教师负责人；教师和管理员可以维护学生选课关系，账号支持启用/禁用。
- 学生可查看精简学情、近期测验和已确认错题；教师可检索课程学生、查看个人测验/答疑/课堂记录，并在复核页查看原答案和评分依据。
- 测验支持按全课程、班级或指定学生布置，可设截止时间并追踪提交、逾期和待复核状态。
- 课程归属、题目答案隔离、测验会话快照、客观题规则评分、简答题教师复核、掌握度查询已形成最小闭环。
- 审计日志已覆盖登录、退出、测验、题库和复核等关键操作，管理员可以查询。
- Chat 已接入 MaxKB 检索适配器，透传资料出处并记录 `usage_log`；未配置或请求失败时明确降级。学生和教师请求会校验课程归属，教师监控只显示负责课程；多课程部署可用 `MAXKB_COURSE_DATASETS` 做数据集隔离。
- 题库支持题目校验、审核状态流转、CSV/XLSX 预览/确认导入和章节筛选；单选、多选、判断、填空、简答和案例题均可进入冻结快照批改。
- 图谱 CRUD、节点映射、资料本地落盘和安全响应头已具备；PDF 向量化仍是明确未接入项，资料上传后返回 `deferred`，不伪造已完成索引。
- Chat、题库、资料、三大图谱、管理端 API 路径已经建立。
- 资料按“本人或管理员”限制管理；删除用户时会清理会话、选课、成绩、学习记录和其私有上传资源。
- 上传接口校验扩展名和基础文件签名；Nginx 示例包含 HTTP 到 HTTPS 跳转、登录限流和隐藏文件拒绝。
- 图谱节点写操作按创建者隔离，映射关系只允许问题节点指向知识节点。
- 默认使用进程内存储；设置 `PERSISTENCE_ENABLED=true` 后会用 `DATA_DIR/store.json` 原子保存并在重启时恢复。JSON 持久化适合单进程运行，多进程/多实例部署前请先替换为数据库。
- 管理员可通过 `/api/admin/backup?format=archive` 导出包含 Store JSON 和本地资料文件的 ZIP 备份，并通过 `/api/admin/restore` 恢复。
- 教师/管理员可生成包含课程、名册、作业、测验与答题明细、掌握度、答疑、课堂互动、题库、资料、AI 用量和操作日志的 `.xlsx`；支持手动下载、24 小时以上周期快照及带预览和快照保护的历史清理。
- 演示账号只在 `APP_ENV=development` 时创建；生产环境不会自动种子固定密码。
- 批改在请求内同步完成，单机 JSON 模式不会出现多进程写文件冲突。
- 大模型接入层已就绪：在 `server/.env` 配置 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（[OI] 兼容接口）后，答疑会生成结构化回答（结论/准则/案例/思政 + 思维导图）、简答题与案例题按 rubric 由模型判分、学情报告由模型撰写建议；未配置时全部回退为规则模式，接口不报错。
- 教师端 `POST /api/quiz/demo-random` 用真实题库即时生成演示评分，但不创建学生测验、成绩或掌握度记录；`GET /api/bank/quick-question` 返回一道已发布快问快答题（含参考答案），供课堂点名使用，答题结果会保存在课程记录中。
- 开发种子数据含完整三大图谱（知识/胜任力/问题各 17 个节点，10 条问题→知识映射）。
- 本地知识库已接入：`审计学知识库/` 全量离线解析为 BM25 索引（422 文件 → 20258 知识块，索引约 59MB，加载 0.5s），答疑优先用 MaxKB，未配置时自动改用本地检索并把命中资料名作为 `sources` 返回，`degraded=false`。解析时修复了 PDF 字体把 GBK 码位当 Unicode 的乱码问题。
- 前端依赖已本地化：`vendor/d3.min.js` 由后端 `/vendor` 提供，不再依赖 unpkg CDN（原先内网/离线时思政地图无法渲染）。
- 登录身份强校验：请求带 `role`，与账号实际角色不符返回 403 `role_mismatch`。
- 学生自助注册（`POST /api/auth/register`）：账号、真实姓名、学号、密码、手机号、课堂代码；提交后为 `pending`，不能登录，需任课教师在「注册审核」页通过。账号、手机号、学号唯一；同 IP 每小时最多 100 次注册，适配课堂集中入班。
- 忘记密码不再使用“账号 + 手机号”公开重置；管理员可从用户管理中设置新密码，并使旧会话失效。
- 课堂代码：每门课程有 6 位代码（如 `AUD101`），教师可在审核页查看并重新生成，旧代码立即失效。接口 `GET /api/enrollment/pending`、`POST /api/enrollment/{id}/approve|reject`、`POST /api/courses/{id}/class-code`。

## 启动

Windows 可双击项目根目录的 `start_audit_companion.bat`，脚本会启动后端、检查健康状态并打开浏览器；默认使用 `server/data` 保存开发数据。

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/` 运行完整页面，打开 `http://127.0.0.1:8000/docs` 查看 OpenAPI。

## 配置大模型（可选）

复制 `server/.env.example` 为 `server/.env`，填入：

```ini
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=你的Key
LLM_MODEL=qwen-plus
```

后端启动时自动读取 `.env`。不填则答疑返回降级提示、主观题走 rubric 规则判分、学情报告用规则文案，其余功能不受影响。

`LLM_REASONING_EFFORT=low` 可让推理型模型（如 grok-4.6）明显提速；普通对话模型留空。

## 本地知识库

`审计学知识库/` 与 MaxKB 无关：后端离线把资料解析、分块、建 BM25 索引，检索不需要网络、不需要向量模型。首次或资料更新后执行：

```powershell
cd server
.\.venv\Scripts\python.exe build_knowledge_index.py "..\审计学知识库"
```

- 索引默认写到 `server/data/knowledge/index.pkl`（可用 `KNOWLEDGE_INDEX_PATH` 改）。
- 全量解析约 15 分钟（PDF 慢）；若只调了分词/停用词，用 `--reindex` 复用已解析文本，约 1 分钟。
- 支持 `.pdf .docx .xlsx .md .txt .epub`；按目录前缀自动归类到 `audit_textbook / audit_standards / audit_cases / cpa_question_bank`。
- 解析完成后执行 `import_knowledge_base.py`，会把同一批资料注册到教师端「资料库导入」页面；脚本可重复执行，不会重复文档。
- `GET /health` 的 `knowledge` 字段显示索引是否加载、知识块数量与构建时间；`GET /api/knowledge/status`（教师/管理员）返回同样信息。

`/health` 只表示进程存活；`/ready` 在生产环境会检查 MaxKB 配置，未就绪时返回 503。Nginx 反代示例见 `nginx.conf.example`。

公网部署请通过 HTTPS 反向代理运行，并在 `server/.env` 配置 `APP_ENV=production`、`PERSISTENCE_ENABLED=true`、`COOKIE_SECURE=true` 和明确的 `ALLOWED_HOSTS`。生产环境缺少这些配置时服务拒绝启动；不要用固定演示账号或开发启动模式暴露公网。

生产环境若前端与 API 同源，`CORS_ORIGINS` 留空；若分离部署，只填写精确的可信前端源。不要把 `server/data`、`.env` 或知识索引目录挂载为 Nginx 静态目录。

生产配置模板见 `server/.env.production.example`。部署前复制为 `server/.env`，替换域名、MaxKB 数据集和服务端密钥，并确保 `DATA_DIR` 仅服务账号可读写；启动脚本会同时检查 `/health` 与 `/ready`。

运行最小检查：

```powershell
python -m unittest discover -s tests -v
```

演示账号：

| 用户名 | 密码 | 角色 |
|---|---|---|
| `admin` | `admin123*` | admin |
| `teacher01` | `teach123*` | teacher |
| `stu001` | `stu123*` | student |

首页会直接提供 `智能学伴演示页.html`；登录、Chat、测验、批改、学情报告、题库、资料、图谱、用户和审计页面都调用本服务 API。
