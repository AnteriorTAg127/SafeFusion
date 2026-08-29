# Changelog

本文件记录 SafeFusion 面向用户的版本变更（[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循语义化版本）。内部开发日志见 `开发/changelog.md`（不入库）；真实数据不在本仓库分发，见 README「数据资产」。

## [0.2.2] - 2026-08-28

### Added

- **向量库全量构建（v0.2.2 事项落地）**：189,262 条文本全部编码入库（black 96,786 + white 92,476），2048 维 L2 归一化，`data/vectors/` 双池 npz + meta + done_ids.json 断点续跑
- **在线 Embedding 联调（llama.cpp 本地服务）**：`CloudEmbeddingAPI` 新增 `allow_no_key`（无鉴权本地服务如 `llama-server --embeddings` 免 Key，默认 False 保留云端强制 Key 安全语义）；`build_vector_db.py` 新增 `--backend cloud`（`--base-url` / `--cloud-model` / `--no-api-key` / `--cloud-timeout`）
- **群聊白语料并入**：`scripts/merge_manifest.py` 将 `white_groupchat.csv`（53,341 条）并入向量导入清单（池内 `(pool,text)` 去重、幂等、`--dry-run`）
- **测试**：新增 allow_no_key 用例与 build_vector_db cloud 参数解析用例；581 pytest 全绿、ruff 全绿

### Notes

- 向量库构建由脚本完成，管理端仅状态查看（`/admin/models`）；图片语料待用户提供后经 `normalize_assets.py --images-dir` 并入图片清单再增量构建
- 构建使用 5545 端口的 WeMM-Embedding-2B-Q4_K_M（llama.cpp，2048 维），耗时约数小时；中断后重跑 `build_vector_db.py` 自动续跑

## [0.3.0] - 2026-08-27

### Added

- **配置存储升级（架构变更）**：SQLite `settings` 表取代 `config_overrides.json`（启动自动迁移）；优先级 内置默认 < config.yaml < 数据库 < 环境变量（**env 只覆盖内存、绝不写 DB**）；设置页每个字段带**生效来源徽标**（默认/YAML/数据库/环境变量，`GET /admin/config/sources`）
- **全量热应用（保存即生效）**：参数类（阈值/开关/复核）直接生效；embedding/llm/light_model/cache 组件重建类**先试建造→落库→原子替换→失败回滚**（500 未落库）；server/logging 端口类下次启动生效（响应标注 `apply_scope`）；改密即时热切（旧令牌立即失效，持久化 DB）
- **模型懒加载 + 按需下载**：启动**不再装配/下载模型**（零网络）；首次审核触发单飞装配（缓存命中秒级，失败细分原因码不自动重试）；`GET /admin/models` 状态 + `POST /admin/models/download` 后台任务（进度轮询）+ `/admin/models/load` 显式装配；设置页「🤖 模型卡」下载/装配/进度可视化 + 分步指引
- **前端管理面板大改（易用性）**：深色主题（跟随系统 + 顶栏切换 + 防闪烁）、**🧪 试运行页**（随机示例一键填充 → 分层证据面板）、概览**系统状态区**（8 组件徽标 + 中文原因 + 跳转）与**数据状态卡**（词库/向量/白名单/规则计数）、审核记录**CSV 导出 + 自动刷新 + 证据面板详情**、词库**一键去重**（自动备份 zip + 结果摘要）、**API Key 管理页**（创建一次性显示完整 Key / 脱敏前缀列表 / 停用删除）、设置页**测试连接**（embedding/llm/fasttext 冒烟）、**首次使用三步向导**、空态即下一步、导入格式示例内嵌、顶栏指南
- **guardian 正则规则库入库**：从 `guardian_benchmark/db/lexicon.db` 提取 **24,531 条**规则（骂人/脏话 24,014 + 广告推广 517，action=violate，0 非法 pattern）直接导入 SQLite
- **RegexRuleEngine 命中词索引**：规则提取关键短语建 AC 倒排，查询按关键词命中词**只匹配相关子集**（正确性安全网：仅「命中必含某短语且短语未现」才跳过，未覆盖回退全量扫描，判定逐字等价）
- **数据资产**：真实群聊记录清洗出**白语料 53,341 条**（`white_groupchat.csv`，与既有白池去重后合入，白池总计 92,476 条）；`normalize_assets.py` 新增 `--images-dir` 图片清单生成（黑白池自动归属/校验/去重）+ **图片语料准备指南**
- **管理端点新增（11 个）**：`/admin/health`、`/admin/models`×4、`/admin/test-examples`、`/admin/test-audit`、`/admin/config/test-connection`、`/admin/config/password`、`/admin/config/sources`、`/admin/keywords/dedup`；`PATCH/DELETE /admin/keys/{key}` 支持**脱敏前缀唯一匹配**
- **README 重写为上手手册**：快速开始/数据准备向导/模型部署实践/配置体系/界面导览/审核 API 对接/管理端点全表（32 个）/升级与备份/FAQ
- **测试**：580 个 pytest 用例（577 基线零回归 + 新增 3）、ruff 全绿、前端构建 721 模块零类型错误、人工第四轮 14/14 PASS（热应用/懒加载/改密闭环/去重实跑）

### Notes

- `server`/`logging` 分组热应用为 `apply_scope="config"`（端口/日志绑定需重启）；`review.interval_min` 热入库但调度器间隔冻结于启动
- 模型装配失败**不自动重试**，需在设置页「模型卡」或 `/admin/models/load` 显式重试；本地装配一律 `local_files_only` 只读缓存、绝不因装配联网下载（下载走 download 端点）
- 审核日志仍不落原文全文（隐私权衡）；试运行走业务管线（会写审计日志）
- 向量库构建（`build_vector_db.py`）与在线 embedding 联调仍按用户决策延后（v0.2.2 事项；本期完成数据侧全部准备：语料/图片清单/guardian 规则）

## [0.2.1] - 2026-08-27

### Added

- **前端管理面板**：Vue3 + Vite + TypeScript 独立 Web 工程（`web/`），八个视图——登录（X-Admin-Token）/ 概览（统计卡 + ECharts 7 天趋势）/ 审核记录（筛选 + detail 展开）/ 词库 / 白名单 / 规则 / 复核 / 设置；仅参考 AstrBot dashboard 的**设计规则**（分区 tab、统计卡片、表格分页、二次确认、Toast、防 XSS），零 AstrBot 依赖
- **配置全量可自定义**：管理端 `GET/PUT /admin/config`（11 个配置分组在线读写）；`data/config_overrides.json` 覆盖层（优先级：内置默认 < config.yaml < 覆盖层 < 环境变量，**重启生效**）；密钥类字段一律遮蔽（只返回环境变量名 + 已配置布尔，值不可编辑）
- **`semantic.fuse_mode` 运行时接线**：图文融合模式（pool/concat/weighted_avg）不再需要改代码；`backend=cloud` 且 `weighted_avg/pool` 时 422 校验并建议 `concat`（在线维度 ≠ 本地 CLIP 512）
- **前端静态托管**：`web/dist` 存在时由管理服务（:8001）mount，同源免 CORS；开发模式 `npm run dev`（/admin 代理到 :8001）
- **测试**：487 个 pytest 用例（+34 配置层，零回归）、ruff 全绿、人工第三轮 12/12 PASS（静态托管/鉴权/遮蔽/校验/恢复默认/覆盖层落盘/重启生效闭环）

### Notes

- **本地向量库构建验证（黑白各 1000 条）与在线 embedding 联调按用户决策顺延 v0.2.2** 一并执行；本期仅打通「配置可自定义」路径（`build_vector_db.py` 与双后端早已就绪，随时可跑）
- 前端概览「降级组件数」卡与部分统计为近似口径（管理侧无 /health 等价端点，见 `开发/v0.2.1/` 记录）
- ECharts 当前整包打进概览懒加载 chunk（约 1MB），后续可按需引入优化

## [0.2.0] - 2026-08-26

### Added

- **动图抽帧（M3）**：GIF 均匀抽帧（默认 5 帧可配），每帧独立进白名单/语义层，任一帧违规 → 整体违规；LLM 兜底动图多帧连贯性提示词
- **正则消歧规则库（M4）**：`rules` 表 + 管理端 `/admin/rules` CRUD（JSON/CSV），**词库与规则热重载免重启**；编排器统一走 `keyword_engine.disambiguate`
- **Redis 缓存后端（M5）**：`CacheBackend` 协议，`memory` / `redis`（redis.asyncio 惰性依赖）双后端可配置切换，Redis 不可达自动降级 memory
- **Rerank 四信号（M6）**：`RerankBackend` 协议 + `LocalClipRerank`（复用 CLIP 候选二次编码，零新模型），置信度升级 `w_top·black_top + w_margin·margin + w_rerank·rerank`（默认关闭，配置即启用）
- **定时复核（M7）**：`ReviewScheduler`（周期可配/手动触发），中置信度样本一致率统计报告 + 阈值调整建议（默认不自动调阈值，写 `data/review_reports/`）
- **拼音首字母收紧（M1，质量修复）**：首字母变体仅对 ≥3 汉字词生成，消除「安南→an 命中今天」类误报（回归用例固化）
- **ML 环境就绪（M2 部分）**：`uv sync --extra ml` 依赖（torch/transformers）、`scripts/build_vector_db.py`（断点续跑/检查点/逐条降级）、**ChineseCLIP 架构修正**（文本编码器专类装载 + transformers 5.x shim）、fasttext 轻模型真跑上线
- **测试**：453 个 pytest 用例（零回归）、integration（ML）用例定义、人工端到端第二轮 7/7

### Notes

- **多模态向量库（CLIP 黑白库）与语义检索层真跑按用户决策延后至 v0.2.1**：本期语义层保持降级（空池原因码），embedding 以云端/本地接口保留；CLIP 权重已下载至本地缓存可复用
- 审核日志存 `text_hash` 与各层明细，不落原文全文；定时复核默认统计模式（逐条 LLM 复核需原文落库，见 `开发/v0.2/prd.md`）

## [0.1.0] - 2026-08-26

### Added

- **工程骨架**：uv 依赖管理（`uv.lock` 锁定）、ruff 质量门、配置三层加载（默认值 → YAML → 环境变量，密钥仅经环境变量）、JSON 行结构化日志、Docker 部署物（`Dockerfile` / `docker-compose.yml`，8000/8001 端口）
- **数据契约层**：`AuditRequest` / `AuditResult` 及 detail 子模型（standard/full 分级，对齐 PRD §4）
- **存储层**：SQLite DAO（API Key / 词库 / 白名单 / 审核日志四表）+ 自研 numpy 向量库（L2 余弦 Top-K、按池持久化、增量）
- **基础规则层**：Aho-Corasick 关键词（拼音/全半角/繁简/符号变体 + 原文回映射）、正则消歧、fasttext 轻模型（缺模型自动 disabled）、图片管线（解码 / GIF 首帧 / MD5+pHash / 白名单）
- **语义层**：Embedding 双后端（本地 Chinese-CLIP / 云端 API 可插拔）、向量融合三模式、黑白对抗检索 + 三信号置信度 + 降级原因码
- **LLM 兜底**：OpenAI 兼容客户端（净化防注入 + 强制 JSON + 失败回退）
- **缓存层**：五级进程内缓存（完整缓存键 / 高频 / 图片去重 / 短文本 LLM / 永久黑白名单）
- **编排决策层**：`process_audit` 分层漏斗（无短路、唯一快速放行、三档置信度动作、请求级参数覆盖、缓存键含 tier 隔离）
- **审核 API**：`POST /v1/audit`（Bearer/X-Api-Key 鉴权、限流、standard 裁剪 detail、overrides 403）、`GET /health`、异常脱敏
- **管理 API（8001）**：Key / 词库 / 图片白名单 / 审核日志 / 向量重建五组端点，`X-Admin-Token` 鉴权
- **资产归一化脚本**：`scripts/normalize_assets.py` 一键构建统一布局（词库 CSV / 黑白语料 CSV / 向量导入清单 JSONL），dry-run 支持
- **测试**：316 个 pytest 用例 + 人工第一轮 12/12（真实语料 20380 词、白名单快放行、缓存隔离、审计日志）

### Fixed

- 空词库 make_automaton 崩溃、审核/高频缓存键 tier 隔离、`WhitelistMatcher` np.int64 距离 JSON 序列化、pydantic 2.11 弃用告警、`.gitignore` 误伤 models 子包

### Known（v0.1 → v0.2 记录）

- 降级模式下（无 ML）关键词强信号按保守口径判违规，广谱词库误报依赖正则规则库收敛（v0.2 已交付）
- 词库/规则写入需重启生效（v0.2 已改为热重载）