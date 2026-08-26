# SafeFusion

> 语义检索 + 关键词规则双通道融合的多模态违规内容识别引擎（文本 / 图片）

SafeFusion 以「基础规则 → 多模态语义检索 → LLM 兜底」分层漏斗，在低成本下实现文本 + 图片统一审核，追求够用的准确度与较低的误判违规率。核心设计是**一律汇总决策、无短路**，适用于社交平台内容审核（帖子配图、评论、头像等）等低成本低误判的内容风控场景。

- **协议**：GPL-3.0（代码开源；训练与审核数据一律不开源，见[数据资产](#数据资产)）
- **状态**：v0.1 开发中（核心组件已交付，编排层与 API 待集成）
- **环境**：Python ≥ 3.10 · uv · FastAPI · SQLite · 自研 numpy 向量库

## 架构

分层漏斗，各层独立可插拔，编排层 `process_audit` 串联：

```text
统一审核 API (FastAPI :8000)
   │
   ├─ ① 缓存层        审核缓存 / 高频缓存 / 图片去重缓存 / 短文本LLM缓存 / 永久黑白名单
   ├─ ② 基础规则层     图片白名单(pHash) │ 文本关键词(Aho-Corasick+拼音) → 正则消歧 │ 轻量文本风险模型(复用 fasttext.pt)
   ├─ ③ 语义检索层     多模态Embedding(Chinese-CLIP/云端API) → 自研向量库黑白对抗检索 → 三信号置信度
   ├─ ④ LLM 兜底层     OpenAI 兼容多模态 LLM，结构化 JSON 输出，失败回退语义层
   └─ ⑤ 编排与决策     汇总决策(无短路) → 三档置信度动作 → 请求级参数覆盖 → 写缓存/写审计
管理 API (FastAPI :8001)  Key管理 │ 词库管理 │ 图片白名单管理 │ 审核日志查询
```

关键设计约束：

1. **一律汇总决策，无短路**：基础规则层各检查并行执行，关键词 / 轻量模型命中仅产生「强风险信号」并提升进入后续层的权重，最终由编排层综合裁决。
2. **唯一快速放行通道**：所有帧白名单命中**且**文本无任何风险信号 → `has_violation=false (source=basic_rules_pass)`。
3. **全部阈值 / 开关可配置**（YAML 配置 + 环境变量覆盖密钥类）。
4. **降级链**：LLM 不可用 → 回退语义层；云端 Embedding 不可用 → 切本地；GPU 不可用 → CPU；向量库后端可替换（Faiss / 外部向量库为二期）。

置信度采用三信号（黑库最高相似度 / 黑库 Top-K 均分 / 黑白均分差值与 margin 比较）与三档阈值（默认 `<0.35` 判定安全、`0.35~0.75` LLM 兜底、`>0.75` 判定违规，全部可配），上线后按真实数据校准。

## 快速开始

### 1. 安装依赖（uv）

```bash
pip install uv
uv sync            # 核心依赖（FastAPI/uvicorn/numpy/pyahocorasick 等）
uv sync --extra ml # 可选：需要本地 Chinese-CLIP / fasttext 推理时安装 torch/transformers
```

### 2. 配置

复制 `config.example.yaml` 为 `config.yaml`，按需修改阈值与后端；加载顺序为内置默认值 → YAML → 环境变量。**密钥类一律通过环境变量注入**：

```bash
export SAFEFUSION_LLM_API_KEY=...          # LLM 兜底（OpenAI 兼容）
export SAFEFUSION_EMBEDDING_API_KEY=...    # 云端 Embedding（可选）
export ADMIN_PASSWORD=...                  # 管理 API 令牌（未设置则启动时随机生成并打印）
```

严禁把密钥写入任何会提交的文件或日志。

### 3. 资产归一化（仅本机运行；产出不入 git）

从旧 Node 版数据与 cherry 语料构建统一词库 / 语料 / 向量导入清单（PRD §5）：

```bash
.venv\Scripts\python.exe scripts\normalize_assets.py --dry-run   # 只统计不写盘
.venv\Scripts\python.exe scripts\normalize_assets.py             # 写盘到 ./data（已 gitignore）
```

详见脚本 `--help`（`--node-data` / `--cherry-data` / `--out` / `--max-rows` 均可配）。

### 4. 运行服务（v0.1 待 T10/T11 集成）

T10/T11 落地后，双服务分别启动：

```bash
.venv\Scripts\python.exe -m uvicorn safefusion.api.app:create_app --host 0.0.0.0 --port 8000
.venv\Scripts\python.exe -m uvicorn safefusion.api.admin:create_admin_app --host 0.0.0.0 --port 8001
```

### 5. Docker

```bash
docker compose up -d --build
```

- 端口映射：`8000`（审核 API）/`8001`（管理 API）
- 密钥：编辑同目录 `.env`（`docker compose` 自动读取），见 `docker-compose.yml` 注释
- 数据持久化：`./data` 挂载为容器内 `/app/data`
- 本地 CLIP / GPU：镜像默认不含 ML 依赖，按 `Dockerfile` 注释自行扩展

## API 概览（v0.1 开发中）

| 端点 | 说明 | 鉴权 | 状态 |
|---|---|---|---|
| `POST /v1/audit` | 审核请求（text / images / context / skip_llm / overrides） | Bearer / X-Api-Key | 🔧 开发中（T10） |
| `GET /health` | 健康检查 + 指标摘要 | 免认证 | 🔧 开发中（T10） |
| `POST/GET/PATCH/DELETE /admin/keys` | API Key 生成、禁用、分组（standard/full） | ADMIN_PASSWORD | 🔧 开发中（T11） |
| `GET/POST/DELETE /admin/keywords` | 词库分类查看、批量导入、词条增删 | ADMIN_PASSWORD | 🔧 开发中（T11） |
| `GET/POST/DELETE /admin/whitelist/images` | 白名单图片上传/批量导入、pHash 入库 | ADMIN_PASSWORD | 🔧 开发中（T11） |
| `GET /admin/logs` | 审核记录分页查询与导出 | ADMIN_PASSWORD | 🔧 开发中（T11） |
| `POST /admin/vectors/rebuild` | 向量库重建 / 增量导入（归一化清单） | ADMIN_PASSWORD | 🔧 开发中（T11） |

请求 / 响应契约以 `开发/v0.1/prd.md` §4 与 `src/safefusion/models/schemas.py` 为准。

## 目录结构

```text
src/safefusion/
├── api/            # 审核 API（T10）与管理 API（T11）
├── cache/          # 五级进程内缓存
├── core/           # AppContext 聚合与编排决策（汇总 / 置信度动作）
├── engines/        # 关键词+正则、轻量模型、图片管线、Embedding、语义、LLM
├── models/         # 数据契约（AuditRequest / AuditResult 等）
├── storage/        # SQLite DAO + 自研 numpy 向量库
├── config.py       # 配置三层加载（默认值 → YAML → 环境变量）
└── logging_setup.py
scripts/            # normalize_assets.py 资产归一化
tests/              # pytest 用例
data/               # 运行时数据（gitignored，不入库）
docs/               # 用户文档（如有）
```

## 数据资产

训练与审核数据（旧 Node 版语料、cherry 文本分类语料、已训模型权重等）为私有资产，**不在本开源仓库分发**。归一化脚本 `scripts/normalize_assets.py` 仅在本机从 `node/data` 与 cherry 语料目录构建 `data/` 下的统一布局（词库 / 语料 / 向量导入清单），产物路径全部参数化且已被 `.gitignore` 排除。

## 开发指南

- 提交代码前阅读根目录 `CLAUDE.md` 与 `开发/rules.md`（工程流程、沙箱约定、硬性规则）
- 内部开发日志 `开发/changelog.md` 不回库；用户视角变更见 `CHANGELOG.md`
- 运行 / 测试 / 检查一律使用 `.venv\Scripts\python.exe -m ...`（禁止 `uv run`）

## License

[GPL-3.0](LICENSE)