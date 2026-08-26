# Changelog

本文件记录 SafeFusion 面向用户的版本变更（[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循语义化版本）。内部开发日志见 `开发/changelog.md`（不入库）；真实数据不在本仓库分发，见 README「数据资产」。

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