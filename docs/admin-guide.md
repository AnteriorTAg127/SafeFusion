# 管理端操作手册

管理 API（:8001）全部端点均需请求头 `X-Admin-Token: <管理令牌>`（缺省 / 错误 → 401）。错误响应 `{"error": "..."}` 脱敏；分页参数 `page`（从 1 起）/ `page_size`（≤500）。

## 1. 审核 API 与健康（:8000）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/audit` | 审核请求（见 API 对接文档契约） |
| GET | `/health` | 免认证健康检查：`{status, version, degraded[], cache, uptime_s}` |

## 2. 管理端点全表（:8001，共 32 个）

> 「v0.3.0」标记为本版本新增端点（11 个）；`PUT /admin/config/{group}` 在本版本语义重构（写 DB + 热应用）。

| 方法 | 路径 | 说明 / 请求要点 | 版本 |
|---|---|---|---|
| POST | `/admin/keys` | 创建 API Key：`{tier: standard\|full, note?}` → `{key(明文仅此一次), tier, enabled, note}`（201） | v0.1 |
| GET | `/admin/keys` | 列出全部 Key：key 字段**脱敏**（前 8 位 + …）；无 last_used/rate_limit 列 | v0.1 |
| PATCH | `/admin/keys/{key}` | 更新 Key：`{enabled?, note?}`（note 更新需存储层支持，否则 501）；`key` 支持完整明文或**唯一脱敏前缀**（前缀多命中 → 400） | v0.1（前缀匹配 v0.3.0） |
| DELETE | `/admin/keys/{key}` | 删除 Key（支持完整 Key 或唯一前缀） | v0.1 |
| POST | `/admin/keywords/import` | 批量导入词库：multipart `file`（CSV「类别,词」两列 / TXT 每行一词 + `category` 查询参数）；仅 UTF-8（可带 BOM） | v0.1 |
| GET | `/admin/keywords` | 词库分页查询：`category? / page / page_size` → `{total, page, page_size, items[]}` | v0.1 |
| DELETE | `/admin/keywords/{keyword_id}` | 按主键删除词条（404 不存在） | v0.1 |
| **POST** | **`/admin/keywords/dedup`** | **一键去重（G10）**：先备份 zip 至 `data/backups/` → 按 (category,word) 去重 → 引擎热重载 → `{status, before, after, removed, failed, backup_file, reload}` | **v0.3.0** |
| GET | `/admin/rules` | 规则列表：`category? / active_only(默认 true)` → `{total, items[]}` | v0.2 |
| POST | `/admin/rules` | 批量新增：JSON 数组 `[{category,pattern,action,note}]` 或 multipart CSV；action 缺省 exempt；重复/非法 → 400 整批拒绝；写库后热重载 | v0.2 |
| DELETE | `/admin/rules/{rule_id}` | 删除规则（404 不存在），删除后热重载 | v0.2 |
| PATCH | `/admin/rules/{rule_id}/active` | 启停规则：`{active: bool}`，热重载即时生效 | v0.2 |
| POST | `/admin/review/run` | 手动触发一轮复核：未注入 reviewer → 501；已有复核执行中 → 202；正常 → 200 + 报告摘要 | v0.2 |
| GET | `/admin/review/status` | 复核状态：启用/间隔/运行中/最近报告/报告目录（未注入 → 501） | v0.2 |
| POST | `/admin/whitelist/images` | 上传白名单图片：multipart `files`（多图）；md5+pHash 入库、原图存 `data/whitelist/{md5}.png`；单张失败不中断 | v0.1 |
| GET | `/admin/whitelist/images` | 白名单分页查询：`page/page_size` → `{total, items[]}` | v0.1 |
| DELETE | `/admin/whitelist/images/{entry_id}` | 删除白名单条目：删记录 + 尽力删磁盘原图 → `{deleted, file_deleted, file}` | v0.1 |
| GET | `/admin/logs` | 审核记录分页（时间倒序）：`start/end/has_violation/source/category/key_tier/page/page_size` | v0.1 |
| GET | `/admin/logs/export` | 导出 CSV（utf-8-sig BOM 兼容 Excel）：过滤参数同 `/admin/logs`，**无分页、全量流式**（前端按 1 万行提示约定） | v0.1 |
| POST | `/admin/vectors/rebuild` | 向量库重建/增量：`{manifest_path?}` → `{status, manifest, result}`；未注入钩子 → 501 | v0.1 |
| GET | `/admin/config` | 全量有效配置（按分组，Key 遮蔽为 `{api_key_env, configured}`） | v0.2.1 |
| **GET** | **`/admin/config/sources`** | **叶子字段级来源映射 `{分组: {点分路径: default\|yaml\|db\|env}}`**（设置页来源徽标数据源） | **v0.3.0** |
| PUT | `/admin/config/{group}` | 写 DB + **热应用**：部分键覆盖；空对象 `{}` 删除该组 DB（恢复默认）；失败回滚旧实例与 DB → 500/422；成功 → `{config, saved, applied, apply_scope, sources, deleted_db_group}` | v0.2.1（**v0.3.0 语义重构**） |
| **GET** | **`/admin/models`** | **模型清单**：chinese-clip（状态/HF 缓存 blobs 与大小）、fasttext（配置/文件/可加载）、vector_store（黑白条数+维度）、semantic（装配状态+原因码） | **v0.3.0** |
| **POST** | **`/admin/models/download`** | **后台下载 CLIP 权重**（202）：`{model_name?}` → `{task_id, status, reused, cache_dir}`；同模型互斥（进行中任务复用） | **v0.3.0** |
| **GET** | **`/admin/models/download/{task_id}`** | **下载进度轮询**：`{status, stage, progress, downloaded_bytes, total_bytes, error}`（404 任务不存在） | **v0.3.0** |
| **POST** | **`/admin/models/load`** | **显式装配语义层**（同步，含 300s 等待）：`{status, reason, message, semantic_ready, duration_s, summary}` | **v0.3.0** |
| **GET** | **`/admin/health`** | **管理侧健康聚合**：`{status, version, components{8 组件}, degraded[], data{词库/向量黑白/白名单/规则}, cache, uptime_s}`；`degraded` 与 :8000 `/health` 同口径 | **v0.3.0** |
| **GET** | **`/admin/test-examples`** | **试运行示例**：从 `data/corpus/black.csv` / `white.csv` 头部（每池 ≤400 候选）随机抽 ≤20 条 ≤200 字符去重样本 → `{items:[{text,pool}], total}`；缺失不报错 | **v0.3.0** |
| **POST** | **`/admin/test-audit`** | **管理端试运行审核**：契约同 `/v1/audit`，走管理令牌、固定 `tier=full` 返回完整 detail（复用缓存与审计日志管线） | **v0.3.0** |
| **POST** | **`/admin/config/test-connection`** | **渠道冒烟测试**：`{channel: embedding\|llm\|fasttext, config?}`（`config` 为临时参数不落库、`api_key` 键剥离）→ `{channel, ok, message, detail}`；无 Key / 缺 base_url 有明确中文提示，全部失败不崩 | **v0.3.0** |
| **POST** | **`/admin/config/password`** | **改密（C5）**：`{current_password, new_password(≥10)}`；hmac 常数时间校验；通过后旧令牌**立即失效**，持久化 settings `admin.token`（重启后 env `ADMIN_PASSWORD` 仍最高优先） | **v0.3.0** |

## 3. Key 前缀匹配说明

`PATCH/DELETE /admin/keys/{key}` 的 `key` 支持两种形态：

1. **完整明文 Key**（精确匹配）；
2. **列表脱敏前缀**（`GET /admin/keys` 回显的「前 8 位 + …」）→ 清理尾字符后做**前缀唯一匹配**：唯一命中即操作该 Key；前缀命中多条 → 400「请提供完整 Key」；零命中 → 404。

这样「列表只回显前缀」也能完成停用/删除操作（v0.3.0 T40 补齐的管理缺口）。
