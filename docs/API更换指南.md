# API 更换指南

> 适用版本:v1.0.0 及以后。所有改动只涉及 `server/.env` 一个文件(Key 与通道配置都
> 在里面,该文件不入库),改完重启后端即可,前端无需改动。
> ⚠️ 本文不包含任何真实 Key 或服务商地址;示例一律用占位符。

## 一、只换 Key(服务商与模型不变)

1. 编辑 `server/.env`,替换四个 Key 变量的值:

   ```
   LLM_TEACHER_KEY=<新教师通道Key>
   LLM_STUDENT_PRIMARY_KEY=<新学生主通道Key>
   LLM_STUDENT_BACKUP_KEY=<新学生备用通道Key>
   LLM_GRADING_KEY=<新批改通道Key>
   ```

2. 重启服务:关掉旧应用窗口后双击仓库根的 `启动服务.bat`。
   (env 是进程启动时读取的,改完必须重启 uvicorn 才生效。)

3. 逐通道真实验证(在 `server/` 目录):

   ```bash
   .venv/Scripts/python verify_llm_channels.py
   ```

   四个通道全部显示 `OK` 即完成。每通道只发一次真实请求,消耗极小。

4. 可选——备用切换演练(确认主通道废掉后流量落到备用):

   ```bash
   .venv/Scripts/python verify_llm_channels.py --pool student --set-env LLM_STUDENT_PRIMARY_KEY=sk-invalid
   ```

5. 旧 Key 若还有效,建议直接到服务商后台作废。

## 二、连服务商一起换(换 base_url 或模型)

1. 改 `server/.env` 的 `LLM_PROVIDERS_JSON`。四个条目(student-primary /
   student-backup / teacher-main / grading-main)每个要改三处:

   | 字段 | 说明 |
   |---|---|
   | `base_url` | 新服务商的 OpenAI 兼容地址(必须兼容 `/v1/chat/completions`,这是接入前提) |
   | `api_key_env` | 保持不变,但把该变量指向新 Key |
   | `model` | 新服务商的模型名 |

   底部的旧式单通道配置(`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`)只是
   兜底——只要 `LLM_PROVIDERS_JSON` 还在就不生效;建议同步改掉,免得以后
   误删通道池时踩坑。

2. 削峰参数按新服务商的真实限额重配:条目里的 `rpm_limit`(当前示例值
   学生 25 / 教师 30 / 批改 40)是按实测配的保守值,拿到新服务商真实
   RPM 后改成对应值。窗口计费(如"每 5 小时 N token")的服务商
   `tpm_limit` 建议不设——窗口额度是事后结算,令牌级预扣只会让学生无谓排队。

3. **共用 Key 必须加 `quota_group`**:如果新服务商是"一个 Key 全通道共用",
   给共用同一 Key 的条目加上相同字段,例如 `"quota_group":"main-key"`。
   否则每个条目按各自 `name` 独立限速,等于变相超过服务商的按 Key 限额。

4. 推理控制:`LLM_REASONING_EFFORT=none` 是针对特定模型调的(关闭隐形思考,
   实测快 2-4 倍、省约 70% token)。换模型后先留空试效果,确认新模型的
   行为再决定要不要配。

5. 重启服务后完成三件验证:

   ```bash
   .venv/Scripts/python verify_llm_channels.py    # 四通道全绿
   ```

   - 浏览器发一条真实答疑、生成一次 AI 练习,确认端到端正常;
   - `curl http://127.0.0.1:8000/health`,确认 `llm_channels` 四项都是 `true`。

6. 正式压测前先确认新服务商的额度窗口规则,按实测单耗折算课堂容量:
   答疑一次约 1250 token、AI 练习生成一次约 3200 token、60 人课约 38 万 token。

## 三、常见问题

- **换了就 401/403**:Key 抄错(多余空格/引号)或服务商侧未开通模型权限;
  先用 `verify_llm_channels.py --channel student` 单通道定位。
- **连接超时**:本机直连部分服务商不稳,确认 `base_url` 可达(参考部署手册
  里 curl 探活的方法);中继类服务商注意是否要加 `/v1` 后缀。
- **模型返回 502(llm_invalid_output)**:新模型没按提示词输出结构化 JSON,
  优先检查 `model` 名是否写对,再考虑该模型是否适合结构化输出。
- **429 变多**:新服务商 RPM 比旧家低,把 `rpm_limit` 调到真实限额以下,
  让本地令牌桶排队消化突发,而不是撞上游 429。
- **验证脚本报 missing environment variable**:某个 `api_key_env` 指向的
  变量在 .env 里不存在或为空,补上即可。

## 四、安全红线

- Key 只存 `server/.env`(该文件已被 .gitignore 排除,绝不入库);
  演示版副本里的 .env 同样含 Key,不用了整个副本删掉,要用的同步换 Key。
- 不把 Key 写进任何文档、聊天记录、截图;泄露过的 Key 立即作废。
- 团队多人使用时按人/按用途分 Key,别共用一个 Key 全班跑。
