# SafeFusion

> 语义检索 + 关键词规则双通道融合的多模态违规内容识别引擎（文本 / 图片）

SafeFusion 以「基础规则 → 多模态语义检索 → LLM 兜底」分层漏斗，在低成本下实现文本 + 图片统一审核，追求够用的准确度与较低的误判违规率。核心设计是**一律汇总决策、无短路**，适用于社交平台内容审核（帖子配图、评论、头像等）等低成本低误判的内容风控场景。

- **协议**：GPL-3.0（代码开源；训练与审核数据一律不开源，见[数据资产](#数据资产)）
- **状态**：v0.3.0（易用性与可运维性大版本：配置 DB 化 + 全量热应用、模型懒加载按需下载、新前端管理面板）
- **环境**：Python ≥ 3.10 · uv · FastAPI · SQLite · 自研 numpy 向量库 · Vue3+Vite（前端）

---

## 目录

1. [架构速览](#架构速览)
2. [快速开始](#快速开始)
3. [文档](#文档)
4. [目录结构](#目录结构)
5. [数据资产](#数据资产)
6. [开发指南](#开发指南)
7. [License](#license)

---

## 架构速览

分层漏斗，各层独立可插拔：

```text
统一审核 API (FastAPI :8000)
   │
   ├─ ① 缓存层        审核缓存 / 高频缓存 / 图片去重缓存 / 短文本LLM缓存 / 永久黑白名单
   ├─ ② 基础规则层     图片白名单(pHash) │ 文本关键词(Aho-Corasick+拼音) → 正则消歧 │ 轻量文本风险模型(复用 fasttext.pt)
   ├─ ③ 语义检索层     多模态Embedding(Chinese-CLIP/云端API) → 自研向量库黑白对抗检索 → 三信号置信度
   ├─ ④ LLM 兜底层     OpenAI 兼容多模态 LLM，结构化 JSON 输出，失败回退语义层
   └─ ⑤ 编排与决策     汇总决策(无短路) → 三档置信度动作 → 请求级参数覆盖 → 写缓存/写审计
```

关键设计约束：

1. **一律汇总决策，无短路**：基础规则层各检查并行执行，关键词 / 轻量模型命中仅产生「强风险信号」并提升进入后续层的权重，最终由编排层综合裁决。
2. **唯一快速放行通道**：所有帧白名单命中**且**文本无任何风险信号 → `has_violation=false (source=basic_rules_pass)`。
3. **全部阈值 / 开关可配置**：内置默认 → config.yaml → 数据库（管理端在线改） → 环境变量，管理端保存**即时生效（热应用）**。
4. **降级链**：LLM 不可用 → 回退语义层；云端 Embedding 不可用 → 切本地；GPU 不可用 → CPU；向量库后端可替换（Faiss / 外部向量库为二期）。

详细架构说明见 [docs/architecture.md](docs/architecture.md)。

---

## 快速开始

### 1. 安装依赖（uv）

```bash
pip install uv
uv sync            # 核心依赖（FastAPI/uvicorn/numpy/pyahocorasick 等）
uv sync --extra ml # 可选：需要本地 Chinese-CLIP / fasttext 推理时安装 torch/transformers（体积大，首次约数 GB）
```

> 沙箱约定：运行 / 测试 / 检查一律直接调用解释器 `.venv\Scripts\python.exe -m ...`（Windows）或 `.venv/bin/python -m ...`（Linux/macOS），**不要使用 `uv run`**。

### 2. 配置

复制 `config.example.yaml` 为 `config.yaml`，按需修改阈值与后端；加载顺序为**内置默认 → YAML → 数据库（管理端设置） → 环境变量**。**密钥类一律通过环境变量注入**：

```bash
# Windows PowerShell
$env:SAFEFUSION_LLM_API_KEY="<你的 LLM Key>"      # LLM 兜底（OpenAI 兼容）
$env:SAFEFUSION_EMBEDDING_API_KEY="<云端 Embedding Key>"  # 云端 Embedding（可选）
$env:ADMIN_PASSWORD="<管理面板登录令牌>"            # 管理 API 令牌（见下方「登录」）
$env:HF_ENDPOINT="https://hf-mirror.com"           # 可选：中国大陆镜像（模型下载加速）
```

```bash
# Linux / macOS
export SAFEFUSION_LLM_API_KEY="..."
export SAFEFUSION_EMBEDDING_API_KEY="..."
export ADMIN_PASSWORD="..."
export HF_ENDPOINT="https://hf-mirror.com"
```

严禁把密钥写入任何会提交的文件或日志；管理面板 / API 响应中密钥一律遮蔽。

### 3. 启动服务

双服务统一入口（审核 API :8000 / 管理 API :8001）：

```bash
.venv\Scripts\python.exe -m safefusion.api
```

启动日志会输出：

```
SafeFusion 启动：审核 API http://0.0.0.0:8000 ｜ 管理 API http://0.0.0.0:8001 ｜ degraded=embedding,semantic,...
```

- `degraded=...` 为当前未就绪组件清单（v0.3.0 起 embedding/semantic 是**懒加载**，启动时未装配属正常，见 [docs/model-guide.md](docs/model-guide.md)）；
- 首次启动会自动执行配置覆盖层迁移（见 [docs/upgrade-backup.md](docs/upgrade-backup.md)）；
- 前端 `web/dist` 构建产物存在时由管理服务托管（同源免 CORS）。

### 4. 登录管理面板

浏览器访问 **http://127.0.0.1:8001/**，输入管理令牌（`X-Admin-Token`）。

**令牌从哪里来（按优先级）**：

| 来源 | 说明 |
|---|---|
| `config.yaml` 的 `admin_token`（或 `admin.token`） | 显式配置 |
| 环境变量 `ADMIN_PASSWORD` | 推荐做法；内存最高优先，**不写入数据库** |
| 数据库 `settings` 表 `admin.token` | 由「设置 → 安全 → 修改密码」持久化（改密后重启且未设环境变量时生效） |
| 启动自动生成 | 以上都未配置时，**启动日志打印一次性令牌**（WARNING 行：「未配置 ADMIN_PASSWORD 环境变量，本次启动自动生成管理令牌（仅此一次输出）：……」）；**重启后变化，需重新获取** |

登录后如词库为空或模型未就绪，会弹出**首次使用三步向导**（令牌 / 数据准备 / 模型与向量库，完成度基于后端真实计数，可跳过、可从顶栏「指南」重新进入）。

更完整的安装 / 配置 / 启动 / 登录说明见 [docs/setup-guide.md](docs/setup-guide.md)。

---

## 文档

详细文档统一放在 `docs/` 下：

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 架构速览与关键设计约束 |
| [docs/setup-guide.md](docs/setup-guide.md) | 安装、配置、启动、登录管理面板 |
| [docs/data-prep.md](docs/data-prep.md) | 词库 / 语料 / 图片 / 向量库数据准备 |
| [docs/model-guide.md](docs/model-guide.md) | Chinese-CLIP / fasttext 部署与故障排查 |
| [docs/config-guide.md](docs/config-guide.md) | 配置优先级、DB 化、热应用 |
| [docs/ui-guide.md](docs/ui-guide.md) | 管理面板界面导览 |
| [docs/api-guide.md](docs/api-guide.md) | 审核 API 契约（`POST /v1/audit`） |
| [docs/admin-guide.md](docs/admin-guide.md) | 管理端 API 操作手册 |
| [docs/upgrade-backup.md](docs/upgrade-backup.md) | 升级、备份与 Docker |
| [docs/faq.md](docs/faq.md) | 常见问题 FAQ |

---

## 目录结构

```text
src/safefusion/
├── api/            # 审核 API（app.py）与管理 API（admin.py，32 端点）
├── cache/          # 五级进程内缓存（memory / redis 可插拔）
├── core/           # AppContext 聚合、编排决策、配置合并（config_override）、热应用（hot_apply）
├── engines/        # 关键词+正则（命中词索引）、轻量模型、图片管线、Embedding（懒加载）、语义、LLM
├── models/         # 数据契约（AuditRequest / AuditResult 等）
├── storage/        # SQLite DAO（6 表，含 settings 配置表）+ 自研 numpy 向量库
├── config.py       # 配置模型与加载（默认值 → YAML → 环境变量）
└── logging_setup.py
scripts/            # normalize_assets.py（资产归一化，含 --images-dir）；merge_manifest.py（群聊白并入清单）；build_vector_db.py（向量库构建）
web/                # 前端管理面板（Vue3+Vite+TS；node_modules/dist 不入库）
tests/              # pytest 用例
data/               # 运行时数据（gitignored：audit.db / corpus / keywords / rules / whitelist / vectors / models / backups / review_reports）
docs/               # 用户文档
```

---

## 数据资产

训练与审核数据（旧 Node 版语料、cherry 文本分类语料、已训模型权重、guardian 规则库等）为私有资产，**不在本开源仓库分发**。归一化脚本 `scripts/normalize_assets.py` 仅在本机从 `node/data` 与 cherry 语料目录构建 `data/` 下的统一布局（词库 / 语料 / 向量导入清单），产物路径全部参数化且已被 `.gitignore` 排除。v0.3.0 附带的 `data/rules/guardian_swear.json`、`data/rules/guardian_ad.json` 与群聊白语料同为私有数据，不入库。

---

## 开发指南

- 提交代码前阅读根目录 `CLAUDE.md` 与 `开发/rules.md`（工程流程、沙箱约定、硬性规则）
- 内部开发日志 `开发/changelog.md` 不回库；用户视角变更见 `CHANGELOG.md`
- 运行 / 测试 / 检查一律使用 `.venv\Scripts\python.exe -m ...`（禁止 `uv run`）
- 前端构建：`cd web && npm install && npm run build`（产物 `web/dist` 由管理服务托管）

---

## License

[GPL-3.0](LICENSE)
