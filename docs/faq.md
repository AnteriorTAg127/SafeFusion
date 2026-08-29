# 常见问题 FAQ

### Q1 登录提示「Token 无效」（401）

- 令牌错误或已过期：确认用的是**当前生效**的管理令牌——改密后旧令牌立即失效；重启后若未设 `ADMIN_PASSWORD`，一次性令牌会变；
- 令牌来源按 `config.yaml admin_token` → `ADMIN_PASSWORD` 环境变量 → DB `settings.admin.token` → 启动日志一次性令牌的优先级生效（env 重启覆盖 DB）；
- 检查启动终端是否出现「自动生成管理令牌（仅此一次输出）」并复制该次输出。

### Q2 登录提示「无法连接管理服务」

- 后端未启动 / 端口不通：管理 API 默认 :8001；确认 `server.admin_port` 与防火墙；
- 与鉴权无关，属网络问题。

### Q3 模型未就绪原因码（degraded / health 中文含义）

| 原因码 | 含义与处置 |
|---|---|
| `lazy_pending` | 语义层懒加载占位（v0.3.0 正常态）：首次审核请求或「装配 / 重新加载」后自动清除，不触发网络 |
| `embedding_unconfigured` | 未配置 embedding 后端：检查 `embedding.backend` 与 `local/cloud` 参数 |
| `embedding_assets_missing` | 本地权重缺失：下载（`/admin/models/download`）后装配；或 `weights_path` 指向已有权重 |
| `embedding_credential_error` | 云端 Key / base_url 缺失或无效：设置页 embedding 卡「测试连接」看提示；Key 走环境变量 |
| `embedding_config_error` | 配置非法（`weights_path` 目录不存在等）：对照 `config.example.yaml` |
| `embedding_error` | 装配过程异常：查启动日志，修复后显式重新装配 |
| `semantic_engine_error` | 语义引擎构造失败（一般随 embedding 装配失败）：修复后 `POST /admin/models/load` |
| `llm_unavailable` | LLM 未配 Key / 超时 / 无效 base_url：`SAFEFUSION_LLM_API_KEY`（或 `OPENAI_API_KEY`）；「测试连接」验证 |
| `keyword_engine_unavailable` | 关键词引擎装配失败：核实词库表可用 / 启动日志 |
| `regex_rules_disabled` | `keyword.regex_rules_enabled=false`：规则层关闭（可配置开启） |
| `light_model_disabled` | fasttext 未启用：`light_model.model_path/config_path` 未配置或文件缺失 |
| `empty_pool`（前端合成） | 向量库某池为 0 条：未跑 `build_vector_db.py` 或清单未合并 |

### Q4 导入报「文件编码不支持」（400）

- 词库 / 规则导入**仅接受 UTF-8（可带 BOM）**（utf-8-sig 解码）；GBK 内容会 400；请先转为 UTF-8；
- 归一化脚本 `normalize_assets.py` 侧支持 utf-8-sig → gbk → utf-8 自动识别（仅脚本，API 侧不受影响）。

### Q5 配置改了不生效 / 生效方式困惑

- **看来源徽标**：设置页字段徽标显示「数据库」才表示该值来自 DB 管理端设置；显示「环境变量」时 DB 改动会被环境变量钉住覆盖；显示「默认」说明未落库（检查保存是否成功）；
- `server` / `logging` 分组「已保存并生效（端口/日志类配置于下次启动生效）」——这两类需重启；
- 热应用失败会自动回滚：保存报 500「配置应用失败（已回滚…）」说明新值未落库，旧配置继续生效，按提示修正参数；
- 密钥类字段永远是只读徽标（环境变量来源），无法在管理端改值。

### Q6 审核返回异常状态码

| 码 | 含义 |
|---|---|
| 401 | API Key 缺失 / 禁用 / 不存在（`invalid api key`） |
| 403 | standard 组携带 `overrides` |
| 429 | 超过每 Key 限流（默认 60 次/60 秒，`SAFEFUSION_RATE_LIMIT` 调整） |
| 422 | 请求体校验失败（脱敏文案） |
| 500 | 内部错误（脱敏，栈在日志） |

`standard` 组拿到的 `detail` 恒为 `null` 是设计行为——需要分层证据请用 `full` 组 Key 或管理端试运行页。

### Q7 管理令牌轮换 / 改密

- 设置页「安全」卡：当前密码验证 + 新密码 ≥10 位 + 确认 → 成功后**旧令牌立即失效**（当前会话主动登出）；
- 改密持久化到 DB `settings.admin.token`；但若设置了 `ADMIN_PASSWORD` 环境变量，**重启后以环境变量为准**（env 只覆盖内存不写 DB）；
- 一次性令牌（未配置任何来源）每次重启变化，是最不推荐的用法。

### Q8 首次请求很慢？

- v0.3.0 懒加载：首次需要语义层的请求会触发模型装配（缓存命中秒级；冷启动需加载权重）；
- 建议上线前在设置页「模型」卡先「装配 / 重新加载」预热，或跑一次试运行；
- 试运行页若因 15s 前端超时未返回（冷装配），属预期，稍后重试或先在设置页装配。

### Q9 API Key 列表里「最近使用 / 限流」显示 — ？

- `api_keys` 表没有 `last_used` / `rate_limit` 列（未扩表），故管理页显示「—」；
- 限流为全局固定策略（每 Key 60 次/60 秒，`SAFEFUSION_RATE_LIMIT` 环境变量调整），不支持按 Key 独立配额。

### Q10 审核日志为什么不存原文？

- `audit_logs` 表只存 `text_hash` 与各层明细 `detail_json`，不落原文全文（隐私与容量权衡）；
- 定时复核默认「统计模式」；若 `detail_json` 内嵌 `text/content/normalized/原文` 键，自动启用逐条 LLM 复核——否则复核报告为一致率统计 + 阈值建议。
