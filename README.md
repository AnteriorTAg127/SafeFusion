# SafeFusion

> 语义检索 + 关键词规则双通道融合的多模态违规内容识别引擎（文本 / 图片）

SafeFusion 以「基础规则 → 多模态语义检索 → LLM 兜底」分层漏斗，在低成本下实现文本 + 图片统一审核，追求够用的准确度与较低的误判违规率。核心设计是**一律汇总决策、无短路**，适用于社交平台内容审核（帖子配图、评论、头像等）等低成本低误判的内容风控场景。

- **协议**：GPL-3.0（代码开源；训练与审核数据一律不开源，见[数据资产](#数据资产)）
- **状态**：v0.2.1（前端管理面板 + 配置全量可自定义）
- **环境**：Python ≥ 3.10 · uv · FastAPI · SQLite · 自研 numpy 向量库 · Vue3+Vite（前端）

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
管理 API (FastAPI :8001)  Key管理 │ 词库管理 │ 图片白名单管理 │ 审核日志查询 │ 正则规则 │ 定时复核 │ 配置管理
前端管理面板 (Vue3+Vite, 托管于 :8001)  登录 │ 概览 │ 审核记录 │ 词库 │ 白名单 │ 规则 │ 复核 │ 设置（配置自定义）
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

### 4. ML 能力启用（v0.2，可选）

语义检索（本地 Chinese-CLIP）与轻量文本风险模型（fasttext.pt）依赖 ML 运行库，按需启用：

```bash
# ① 安装 ML 依赖（torch / transformers，体积大；首次约数 GB）
uv sync --extra ml

# ② 构建向量库：import_manifest.jsonl → Chinese-CLIP 批量编码 → data/vectors/
.venv\Scripts\python.exe scripts\build_vector_db.py                 # 全量（13.6 万条，GPU 优先）
.venv\Scripts\python.exe scripts\build_vector_db.py --dry-run       # 只统计不编码
.venv\Scripts\python.exe scripts\build_vector_db.py --max-rows 5    # 调试小样本
```

- **权重**：首次运行自动从 HuggingFace 下载 `OFA-Sys/chinese-clip-vit-base-patch16`（约数百 MB）；
  离线 / 内网环境用 `--model <本地权重目录>` 指向已下载权重；更多参数见脚本 `--help`。
- **断点续跑**：已编码 id 记录在 `data/vectors/done_ids.json`，中途中断后重跑自动跳过已入库条目；
  **全量重建** = 清空 `data/vectors/` 后重跑。

```bash
# ③ 配置（config.yaml，样例见 config.example.yaml 对应注释块）
#    embedding.local.*   → Chinese-CLIP 后端（model_name / weights_path / device）
#    light_model.*       → fasttext.pt + config.json
#      （复用 开发/cherry文本分类/部署/model/ 训练产物，或拷入 data/models/ 后指向该路径）

# ④ 重启服务：向量库与模型随启动加载；/health 出现 embedding / light_model / semantic
#    降级项时，按上面步骤检查 ML 依赖与配置。
```

### 5. 运行服务

双服务统一入口（v0.1 起）：

```bash
.venv\Scripts\python.exe -m safefusion.api
# 审核 API :8000 / 管理 API :8001
```

### 6. 前端管理面板（v0.2.1）

浏览器访问 **http://127.0.0.1:8001/**（管理服务托管 `web/dist` 构建产物，同源免 CORS）：

- 页面：登录（X-Admin-Token）→ 概览（统计卡 + 7 天趋势）/ 审核记录 / 词库 / 白名单 / 规则 / 复核 / 设置
- **设置页 = 配置全量可自定义**：11 个配置分组在线编辑（embedding 后端、阈值、缓存、LLM、语义融合等），保存写入
  `data/config_overrides.json`（密钥类只显示「环境变量名 + 已配置布尔」，值一律不可编辑），**重启后生效**
- 配置优先级：内置默认 < `config.yaml` < 管理端覆盖层 < 环境变量

开发模式（本地联调，`/admin` 代理到 `:8001`）：

```bash
cd web
npm install            # 首次（npm 缓存建议 --cache ./.npm-cache 沙箱适配）
npm run dev            # http://localhost:5173
npm run build          # 产物 web/dist，供后端托管（vue-tsc 类型检查 + vite 打包）
```

### 7. Docker

```bash
docker compose up -d --build
```

- 端口映射：`8000`（审核 API）/`8001`（管理 API）
- 密钥：编辑同目录 `.env`（`docker compose` 自动读取），见 `docker-compose.yml` 注释
- 数据持久化：`./data` 挂载为容器内 `/app/data`
- 本地 CLIP / GPU：镜像默认不含 ML 依赖，按 `Dockerfile` 注释自行扩展

## API 概览

| 端点 | 说明 | 鉴权 |
|---|---|---|
| `POST /v1/audit` | 审核请求（text / images / context / skip_llm / overrides） | Bearer / X-Api-Key |
| `GET /health` | 健康检查 + 指标摘要 | 免认证 |
| `POST/GET/PATCH/DELETE /admin/keys` | API Key 生成、禁用、分组（standard/full） | ADMIN_PASSWORD |
| `GET/POST/DELETE /admin/keywords` | 词库分类查看、批量导入、词条增删 | ADMIN_PASSWORD |
| `GET/POST/DELETE /admin/whitelist/images` | 白名单图片上传/批量导入、pHash 入库 | ADMIN_PASSWORD |
| `GET /admin/logs` | 审核记录分页查询与导出 | ADMIN_PASSWORD |
| `GET/POST/DELETE /admin/rules`、`PATCH /admin/rules/{id}/active` | 正则消歧规则 CRUD 与启停（v0.2） | ADMIN_PASSWORD |
| `GET /admin/review/status`、`POST /admin/review/run` | 定时复核状态与手动触发（v0.2） | ADMIN_PASSWORD |
| `GET /admin/config`、`PUT /admin/config/{group}` | 配置全量读写（Key 遮蔽；空对象恢复默认；重启生效）（v0.2.1） | ADMIN_PASSWORD |
| `POST /admin/vectors/rebuild` | 向量库重建 / 增量导入（归一化清单） | ADMIN_PASSWORD |

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
scripts/            # normalize_assets.py 资产归一化；build_vector_db.py 向量库构建（ML，v0.2）
web/                # 前端管理面板（Vue3+Vite+TS，v0.2.1；node_modules/dist 不入库）
tests/              # pytest 用例
data/               # 运行时数据（gitignored，不入库，含 config_overrides.json 配置覆盖层）
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