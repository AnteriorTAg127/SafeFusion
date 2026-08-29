# SafeFusion

> 语义检索 + 关键词规则双通道融合的多模态违规内容识别引擎（文本 / 图片）

SafeFusion 以「基础规则 → 多模态语义检索 → LLM 兜底」分层漏斗，在低成本下实现文本 + 图片统一审核，追求够用的准确度与较低的误判违规率。核心设计是**一律汇总决策、无短路**，适用于社交平台内容审核（帖子配图、评论、头像等）等低成本低误判的内容风控场景。

- **协议**：GPL-3.0（代码开源；训练与审核数据一律不开源，见[数据资产](#数据资产)）
- **状态**：v0.3.0（易用性与可运维性大版本：配置 DB 化 + 全量热应用、模型懒加载按需下载、新前端管理面板）
- **环境**：Python ≥ 3.10 · uv · FastAPI · SQLite · 自研 numpy 向量库 · Vue3+Vite（前端）

---

## 目录

1. [架构](#架构)
2. [快速开始](#快速开始)（安装 → 配置 → 启动 → 登录）
3. [数据准备向导](#数据准备向导)（词库 / 语料 / 白名单图 / 正则规则 / 图片语料 / 向量库）
4. [模型部署与实践](#模型部署与实践)（Chinese-CLIP 懒加载与下载、fasttext、故障排查）
5. [配置体系](#配置体系)（优先级、DB 化、热应用、来源标识）
6. [界面导览](#界面导览)（管理面板每页一句话 + 主操作）
7. [审核 API 对接](#审核-api-对接)（POST /v1/audit 完整契约）
8. [管理端操作手册](#管理端操作手册)（全部管理端点速查）
9. [升级与备份](#升级与备份)
10. [常见问题 FAQ](#常见问题-faq)

---

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
管理 API (FastAPI :8001)  Key管理 │ 词库管理 │ 图片白名单管理 │ 审核日志查询 │ 正则规则 │ 定时复核 │ 配置管理 │ 模型管理
前端管理面板 (Vue3+Vite, 托管于 :8001)  登录 │ 试运行 │ 概览 │ 审核记录 │ 词库 │ 白名单 │ 规则 │ 复核 │ 密钥 │ 设置
```

关键设计约束：

1. **一律汇总决策，无短路**：基础规则层各检查并行执行，关键词 / 轻量模型命中仅产生「强风险信号」并提升进入后续层的权重，最终由编排层综合裁决。
2. **唯一快速放行通道**：所有帧白名单命中**且**文本无任何风险信号 → `has_violation=false (source=basic_rules_pass)`。
3. **全部阈值 / 开关可配置**：内置默认 → config.yaml → 数据库（管理端在线改） → 环境变量，管理端保存**即时生效（热应用）**。
4. **降级链**：LLM 不可用 → 回退语义层；云端 Embedding 不可用 → 切本地；GPU 不可用 → CPU；向量库后端可替换（Faiss / 外部向量库为二期）。

置信度采用三信号（黑库最高相似度 / 黑库 Top-K 均分 / 黑白均分差值与 margin 比较）与三档阈值（默认 `<0.35` 判定安全、`0.35~0.75` LLM 兜底、`>0.75` 判定违规，全部可配），上线后按真实数据校准。

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

- `degraded=...` 为当前未就绪组件清单（v0.3.0 起 embedding/semantic 是**懒加载**，启动时未装配属正常，见[模型部署与实践](#模型部署与实践)）；
- 首次启动会自动执行配置覆盖层迁移（见[升级与备份](#升级与备份)）；
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

---

## 数据准备向导

> 目标：新用户 30 分钟完成数据准备。所有数据放在 `data/` 目录（已 gitignore，不入库）。管理面板顶栏「指南」内含数据准备清单入口。

### 3.1 逐项清单

| 数据项 | 来源 | 格式 | 放置路径 | 生成 / 导入命令 | 导入入口 | 产物 |
|---|---|---|---|---|---|---|
| **词库** | 旧 Node 版 `keywords/` + cherry 分类语料 | CSV（`类别,词` 两列，UTF-8 可带 BOM）；单条 TXT（每行一词 + 类别） | 原始 CSV 放 `data/keywords/*.csv`；入库后存 SQLite | `.venv\Scripts\python.exe scripts\normalize_assets.py`（生成 CSV）；管理端「词库」页导入，或 `POST /admin/keywords/import` | 前端「词库管理」页 | SQLite `keywords` 表（类别+词 唯一，重复自动跳过） |
| **黑白语料** | 旧 Node 版白/违规 CSV + cherry 文本分类语料 | CSV（`text,label,category,source`） | `data/corpus/black.csv`、`data/corpus/white.csv`、`data/corpus/white_groupchat.csv` | `scripts/normalize_assets.py`（生成 corpus 与导入清单）；**白群聊语料合并**见下 | 「试运行」页随机示例（读语料头部抽样 20 条）；`scripts/build_vector_db.py` 编码入库 | 向量库黑白池 + 试运行示例 |
| **正则规则** | guardian 规则库（`开发/cherry文本分类/部署/guardian_benchmark/db/lexicon.db`，R1 提取） | JSON 数组 `[{category, pattern, action, note}]` 或 CSV（`category,pattern,action`） | 已导出 `data/rules/guardian_swear.json`（24,014 条）/ `guardian_ad.json`（517 条） | v0.3.0 已直接导入 SQLite（合计 24,531 条，action=violate）；再次导入用 `POST /admin/rules` | 前端「规则管理」页（JSON/CSV 导入） | SQLite `rules` 表；规则引擎热重载后参与正则消歧 |
| **白名单图片** | 用户提供（合规素材） | PNG / JPEG / GIF 等 | 上传后自动存 `data/whitelist/{md5}.png` | 无需手工命令 | 前端「图片白名单」页多图上传（multipart `files` 字段），`POST /admin/whitelist/images` | SQLite `whitelist_meta` 表（md5 + pHash）+ 磁盘原图 |
| **图片语料**（语义检索用） | 用户自采（R3 指南） | jpg / png 为主（长边 ≥ 256px 为宜；gif/webp/bmp 亦可） | `data/images/black/`、`data/images/white/`（子目录按类别随意嵌套） | `scripts/normalize_assets.py --images-dir data\images`（生成图片清单） | —（清单供向量库构建） | `data/vectors/import_manifest_images.jsonl`（每行一张图，与文本清单分离） |
| **向量库** | 黑白语料清单（文本 + 图片） | JSONL（`{pool,text,image_path,...}`） | `data/vectors/import_manifest.jsonl`（文本，189,262 行：black 96,786 + white 92,476 含群聊白）、`import_manifest_images.jsonl`（图片） | `.venv\Scripts\python.exe scripts\build_vector_db.py`（CLIP 批量编码） | —（由脚本构建，管理端只有状态查看） | `data/vectors/` 下 black/white 双池 npz + meta + done_ids.json |

### 3.2 语料与词库（normalize_assets）

从旧 Node 版数据与 cherry 语料构建统一词库 / 语料 / 向量导入清单（产出不入 git）：

```bash
.venv\Scripts\python.exe scripts\normalize_assets.py --dry-run   # 只统计不写盘
.venv\Scripts\python.exe scripts\normalize_assets.py             # 写盘到 ./data（已 gitignore）
```

参数（`--help` 全量）：`--node-data`（旧 Node 版数据目录）/ `--cherry-data`（cherry 语料目录）/ `--out`（输出目录）/ `--max-rows`（清单行数上限，0=全量）/ `--dry-run` / `--images-dir` / `--image-pool`。

### 3.3 群聊白语料合并（v0.3.0）

R2 已从真实群聊记录清洗出白语料 `data/corpus/white_groupchat.csv`（**新增 53,341 条**，label=0 / category=groupchat / source=node:白群聊.csv，与既有白语料跨库去重）。合并后白池总数 = 39,135 + 53,341 = **92,476 条**。

- 若要让群聊语料参与**语义检索**：执行 `scripts\merge_manifest.py` 将 `white_groupchat.csv` 并入白池清单（池内 `(pool,text)` 去重，幂等），再执行 `build_vector_db.py` 重建白池；
- 若要让群聊语料出现在**「试运行」随机示例**：试运行抽样读取 `data/corpus/white.csv` 头部（每池 ≤400 候选），把群聊语料合入该文件即可；
- 现状：`data/corpus/` 中 `white.csv` 与 `white_groupchat.csv` 两个文件并存，未改动既有人工语料；是否重归一化由部署方按需执行（幂等，`done_ids.json` 断点续跑）。

### 3.4 图片语料（R3 指南要点）

图片语义检索依赖真实图片语料（旧库零图片，需自采）。推荐：

```text
data/images/
├── black/                  # 违规图片池（建议 ≥1000 张，按类别分子目录）
│   ├── 色情/  暴恐/  涉枪涉爆/  政治敏感/ ...
└── white/                  # 安全图片池（建议 ≥1000 张，略多亦可）
    ├── 风景/  日常生活/  商品/  人物/ ...
```

```bash
# ① 只统计不写盘（先看扫描 / 损坏情况）
.venv\Scripts\python.exe scripts\normalize_assets.py --images-dir data\images --dry-run
# ② 生成图片清单（目录名含 black/white 自动归属池；也可 --image-pool black 强制指定）
.venv\Scripts\python.exe scripts\normalize_assets.py --images-dir data\images
# 产物：data/vectors/import_manifest_images.jsonl（不混入文本清单）
# ③ 与文本清单合并后构建向量库（示意）
.venv\Scripts\python.exe scripts\build_vector_db.py --manifest data\vectors\import_manifest_images.jsonl
```

要点与安全红线：

- 格式：JPG / PNG 首选（CLIP 训练分布最接近），长边 ≥ 256px 为宜（224–512 更佳）；GIF 取首帧 / 抽帧编码；
- 归属池：`--image-pool black|white` 显式优先；缺省按文件**目录名**自动归属（路径含 black→黑池、含 white→白池，就近优先），无归属的跳过并计入报告；
- 增量更新：新增图片放入对应目录重跑第 2 步（覆盖写），再对合并清单重跑 `build_vector_db`（`--resume` 默认开，已入库 id 自动跳过）；
- **图片本体不入库**：向量库只存浮点向量 + meta（含图片路径字符串）；`data/` 已 gitignore；
- **儿童性侵素材（CSAM）零容忍**：此类图片一律不得采集存储（违法且有害），黑池用对应文本词向量兜底；
- 采集合规：优先可商用 / CC0 图库照片或自有素材；爬取需遵守目标站 ToS 与版权。

### 3.5 构建向量库（build_vector_db）

```bash
# 本地 Chinese-CLIP（默认，GPU 优先；全量 18.9 万条）
.venv\Scripts\python.exe scripts\build_vector_db.py
# 云端 / 本地 OpenAI 兼容 Embedding API（如 llama.cpp llama-server --embeddings）
.venv\Scripts\python.exe scripts\build_vector_db.py --backend cloud `
    --base-url http://127.0.0.1:5545/v1 --cloud-model WeMM-Embedding-2B-Q4_K_M.gguf `
    --no-api-key
.venv\Scripts\python.exe scripts\build_vector_db.py --dry-run       # 只统计不编码
.venv\Scripts\python.exe scripts\build_vector_db.py --max-rows 5    # 调试小样本
.venv\Scripts\python.exe scripts\build_vector_db.py --model <本地权重目录>  # 离线 / 内网环境
```

- **后端选择**：`--backend local`（默认，Chinese-CLIP）| `--backend cloud`（OpenAI 兼容 API，`--base-url` 必填，`--no-api-key` 用于本地无鉴权服务如 llama.cpp）；
- **权重**：local 后端首次运行自动从 HuggingFace 下载 `OFA-Sys/chinese-clip-vit-base-patch16`（数百 MB）；离线环境用 `--model` 指向已下载权重；cloud 后端无本地权重依赖；
- **断点续跑**：已编码 id 记录在 `data/vectors/done_ids.json`，中断后重跑自动跳过；**全量重建** = 清空 `data/vectors/` 后重跑；
- 其余参数：`--manifest`（清单路径）/ `--out` / `--batch 64`（显存不足调小）/ `--device auto|cpu|cuda` / `--resume / --no-resume` / `--cloud-timeout`（cloud 单次请求超时）；
- 图片清单（`import_manifest_images.jsonl`）同样经此脚本编码入库（`is_image_row` 分支按 `image_path` 走图片编码管线）。

---

## 模型部署与实践

### 4.1 懒加载语义（v0.3.0 架构变化）

v0.3.0 起**启动不再装配 / 下载模型**：

- `AppContext` 只保存建造参数，语义引擎以 lazy 占位；启动日志 `degraded` 会包含 `embedding`、`semantic`，健康页语义引擎显示 `lazy_pending`——**这是正常状态**，不触发任何网络请求；
- **首次审核请求**真正需要语义层时触发**单飞装配**（一次只装一次；本机缓存命中秒级，超过等待窗口快速降级返回，不阻塞请求超时）；
- 显式装配入口：设置页「模型」卡点击「装配 / 重新加载」（`POST /admin/models/load`）；
- 装配失败会细分原因码（见 FAQ 原因码表），不会自动重试，需显式重新装配。

### 4.2 中文 CLIP 下载与装配

管理面板 **设置 → 🤖 模型 卡** 四行状态（数据来自 `GET /admin/models`）：

| 行 | 状态徽标 | 可操作 |
|---|---|---|
| 🧬 Chinese-CLIP | 未配置 / 未下载 / 下载中 / 已就绪 / 出错 / 云端 | 未下载 →「⬇️ 下载模型」；已就绪或出错 →「🔄 装配 / 重新加载」 |
| ⚡ fasttext | 未配置 / 缺失 / 出错 / 就绪 | 配置指引跳下方 `light_model` 分组；「🔌 测试连接」冒烟 |
| 🧠 语义引擎 | 已就绪 / 待装配（lazy_pending） | 首次审核或「装配 / 重新加载」自动装配 |
| 📚 向量库 | 黑白池条数 + 维度 | 任一池非空即就绪 |

下载流程（也可用端点，见[管理端操作手册](#管理端操作手册) `POST /admin/models/download`）：

1. 点「⬇️ 下载模型」→ 后台任务启动（同模型并发互斥，进行中任务会被复用）；
2. 前端 1s 轮询 `GET /admin/models/download/{task_id}` 显示 阶段 / 百分比 / 字节数；
3. 完成后点「🔄 装配 / 重新加载」→ 状态转「已就绪」；此后 degraded 清除。

**缓存目录**：HF 缓存根 = 环境变量 `HF_HOME`（默认未设置时自动设为 `data/models/hf`，transformers 与 huggingface_hub 共用）。

**下载加速 / 代理 / 离线**：

| 场景 | 做法 |
|---|---|
| 中国大陆网络慢 | 设置 `HF_ENDPOINT=https://hf-mirror.com` 后再下载 / 装配 |
| 公司代理 | 设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量（或 `ALL_PROXY`） |
| 完全离线 / 内网 | `build_vector_db.py --model <本地权重目录>`；或配置 `embedding.local.weights_path` 指向本地权重目录，装配走 `local_files_only` 只读缓存、绝不联网 |
| Docker 内下载 | 把 HF 缓存目录挂进卷（`./data:/app/data` 已含 `data/models/hf`）；或镜像内预置权重 |

### 4.3 fasttext（轻量文本风险模型）

fasttext 复用 cherry 文本分类训练产物（非 HF 模型）：

```bash
# 把训练产物拷入 data/models/（推荐，data/ 已 gitignore），或直接指向原路径
#   开发/cherry文本分类/部署/model/fasttext.pt
#   开发/cherry文本分类/部署/model/config.json
```

然后在 `config.yaml` 或管理端「设置 → light_model 分组」配置：

```yaml
light_model:
  model_path: "data/models/fasttext.pt"
  config_path: "data/models/config.json"
```

保存即热应用生效；可在设置页点「🔌 测试连接」（channel=fasttext）验证模型文件存在 + 可加载。

### 4.4 常见模型故障排查表

| 症状 | 排查 / 解决 |
|---|---|
| 设置页模型卡 CLIP 显示「未下载」 | 点「下载模型」；检查网络与 `HF_ENDPOINT`；下载完成需点「装配 / 重新加载」 |
| `lazy_pending` 持续存在 | 属未装配占位；触发首次审核或点「装配」；装配成功即清除 |
| `embedding_assets_missing` | 权重缺失；下载后再装配；或离线配置 `weights_path` 指向已有权重目录 |
| `embedding_credential_error` | 云端后端未配 Key / base_url；设置页 embedding 卡「测试连接」看具体错误；Key 只能走环境变量 |
| `embedding_config_error` | 配置非法（如 `weights_path` 目录不存在）；对照 `config.example.yaml` 检查 |
| `semantic_engine_error` | 语义引擎构造失败；查看启动日志；修复配置后点「装配 / 重新加载」 |
| `llm_unavailable` | 未配置 `SAFEFUSION_LLM_API_KEY`（或 `OPENAI_API_KEY`）/ 超时 / 无效 base_url；设置页 llm 卡「测试连接」 |
| CLIP 装配超慢 / 首次请求卡顿 | 首次装配需加载权重（秒~分钟级）；缓存命中后秒级；可先点「装配 / 重新加载」预热 |
| fasttext「未配置」 | 设置 `light_model.model_path / config_path` 并保存；测试连接验证 |
| 向量库 0 条 | 未跑 `build_vector_db.py`（或清单未含合并语料）；`/admin/models` 向量库行看黑白条数 |

---

## 配置体系

### 5.1 四层优先级与来源标识

v0.3.0 配置存储升级为 **SQLite `settings` 表**（取代 v0.2.1 的 `data/config_overrides.json`），优先级：

```text
内置默认  <  config.yaml  <  数据库 settings（管理端在线设置）  <  环境变量
```

- **环境变量（最高优先）只覆盖内存、绝不写数据库**：`SAFEFUSION_<路径>_<键>` 钉住的叶子在合并时跳过 DB 值（例：`SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD=0.7`）；
- **密钥类只走环境变量**：`SAFEFUSION_LLM_API_KEY`（或 `OPENAI_API_KEY`）、`SAFEFUSION_EMBEDDING_API_KEY`；YAML 里的 `api_key` 键会被忽略并告警，管理端写库一律 422 拒绝，GET 响应只见「环境变量名 + 已配置布尔」；
- **来源徽标**：设置页每个字段旁有小徽标（默认=淡化虚线框 / YAML=灰 / 数据库=蓝 / 环境变量=橙），数据来自 `GET /admin/config/sources`——「当前生效值来自哪一层」一目了然；
- **11 个配置分组**：server / thresholds / embedding / llm / cache / light_model / logging / image / keyword / semantic / review（`data_dir` 为顶层标量，不构成分组）；
- 语义组 `fuse_mode`（pool/concat/weighted_avg）为真实叶子，随普通配置一并合并生效。

### 5.2 全量热应用（保存即生效）

管理端 `PUT /admin/config/{group}` 写 DB 后**立即应用，无需重启**：

| 分组类型 | 分组 | 行为 |
|---|---|---|
| 参数类 | thresholds / semantic / review / keyword / image | 直接同步运行配置叶子（阈值字典原子替换、词库/规则热重载） |
| 组件重建类 | embedding / llm / light_model / cache | **先试建造**（失败 500、DB 不写、旧实例继续生效）→ 落库 → 锁内原子替换；失败自动回滚旧实例并恢复 DB 旧值 |
| 纯配置类 | server / logging | 仅同步配置叶子（端口 / 日志绑定类变化**下次启动生效**），响应标注 `apply_scope="config"` |

- 保存响应区分三种语义：`applied=true + runtime` =「已保存并生效」；`applied=true + config` =「已保存并生效（端口/日志类配置于下次启动生效）」；`applied=false` =「已保存（写入配置存储，当前部署未热应用，重启后生效）」；
- embedding / llm 后端切换会重建对应引擎实例：语义引擎按「新 embedding + 现有向量库 + 当前阈值」一并重建；
- 已知业务校验：`backend=cloud` 且 `fuse_mode ∈ weighted_avg/pool`（在线维度 ≠ 本地 CLIP 512）→ 422 建议改用 `concat`。

---

## 界面导览

管理面板（:8001）顶栏含 🌙/☀️ 主题切换、分区切换（4 分区覆盖 9 页）、「指南」入口（README 要点 / 审核 API curl / 数据准备清单 / 首次使用向导）。每页顶部有一句话用途 + 主操作按钮；空列表均有「为什么空 + 现在该干什么 + 跳转按钮」。

| 页面 | 一句话 | 主操作 |
|---|---|---|
| **登录** `/login` | 输入管理令牌（X-Admin-Token）登录；底部说明令牌来源与 FAQ | 粘贴令牌 → 登录（用 `GET /admin/config` 验证，401 即「Token 无效」） |
| **试运行** `/trial`（独立分区 🧪） | 现场验证全链路，上手主入口 | 输入文本 / 🎲 一键填充随机示例（黑白池 ≤20 条）→ 🚀发送 → 分层证据面板 + 原始 JSON 折叠；管理端 full 权限 + 耗时 ms |
| **概览** `/overview` | 统计卡 + 7 天趋势 + 系统状态 + 数据状态 | 「🩺 系统状态」组件徽标（绿/黄/红，点击跳设置页对应卡）；「📦 数据状态」词库/向量黑白/白名单/规则计数（可跳转导入）；「⟳ 自动刷新（10s）」开关（默认关，localStorage 记忆） |
| **审核记录** `/audit` | 审核日志查询（服务端分页 + 客户端过滤） | 时间/结论/来源/类别/Key 分组筛选；行详情打开**分层证据面板**（关键词命中/正则/语义 Top5/白名单/LLM 结论/黑白三值）；「⬇️ 导出 CSV」（当前筛选，超 1 万行二次确认提示缩小范围）；「⟳ 自动刷新（10s）」开关 |
| **词库管理** `/keywords` | 词库条数/分类 + 增删查 + 批量导入 | 「🧹 一键去重」（confirm 提示副作用 → 自动备份 zip 到 `data/backups/` → 结果摘要 + 引擎重载状态）；CSV/TXT 导入（面板内嵌每行一条 + 示例块 + GBK/UTF-8 自动识别提示） |
| **图片白名单** `/whitelist` | 白名单图片上传与删除 | 多图上传（自动计算 md5+pHash 入库、原图存 `data/whitelist/`）；缩略图占位 + 服务端路径文本 |
| **规则管理** `/rules` | 正则消歧规则 CRUD 与启停 | JSON/CSV 批量导入（示例内嵌）；新增表单；启用开关（PATCH 热重载即时生效）；删除二次确认 |
| **定时复核** `/review` | 复核状态 + 手动触发 + 最近报告 | 「跑一轮复核」（200 完成 / 202 进行中语义区分）；报告卡显示采样/复核数/一致率/阈值建议（报告写 `data/review_reports/`） |
| **密钥管理** `/keys`（系统设置分区） | API Key 生命周期（standard/full） | 创建（**完整 Key 仅创建时显示一次** + 复制按钮，关闭不再显示）；列表（缩略前缀 + 备注 + 最近使用/限流显示「—」）；停用/删除二次确认；页内「🔌 审核 API 对接」curl 速查 |
| **系统设置** `/settings` | 配置分组在线编辑 + 模型卡 + 安全 | 每字段**来源徽标**（默认/YAML/数据库/环境变量）；embedding/llm/light_model 卡「🔌 测试连接」内联结果；顶部「🤖 模型」卡（下载/装配/轮询）；底部「🔐 安全」卡改密（新密码 ≥10 位，改后旧令牌立即失效并主动登出）；「↺ 恢复默认」（空对象删除 DB 组） |

**首次使用向导**（`GuideWizard`）：首次登录且「词库=0 或模型未就绪」时弹出，三步（令牌环境 → 数据准备 → 模型与向量库），完成度只基于后端真实计数（`/admin/health` + `/admin/models`），全部就绪自动完成；可跳过、可从「指南」重新打开。

**深浅主题**：顶栏 🌙/☀️ 三态切换，默认跟随系统（`prefers-color-scheme`），localStorage `sf_theme` 记忆，首帧前注入避免闪烁。

---

## 审核 API 对接

### 6.1 端点与鉴权

`POST http://<host>:8000/v1/audit` —— 审核入口。

| 项 | 说明 |
|---|---|
| 鉴权 | 请求头 `X-Api-Key: <API Key>`，或 `Authorization: Bearer <API Key>`（两者任选其一；无 Key / 禁用 → 401 `{"error":"invalid api key"}`） |
| 限流 | 每 Key 滑动窗口默认 **60 次 / 60 秒**（环境变量 `SAFEFUSION_RATE_LIMIT` 整体调整；超限 → 429 `{"error":"请求过于频繁"}`） |
| API Key 来源 | 管理面板「密钥管理」页创建（完整 Key 仅创建时显示一次）；standard / full 两组 |
| 分级差异 | `standard` 组：`detail` 为 `null`（只返回基本判定）；`full` 组：返回完整分层明细（关键词/正则/语义 Top5/白名单/LLM/黑白三值） |
| overrides | 请求级阈值覆盖**仅 full 组可用**，standard 携带 → 403 `{"error":"overrides 仅 full 组可用"}` |

### 6.2 请求体（AuditRequest）

```jsonc
{
  "text": "待检测文本（可为 null，纯图片请求）",
  "images": [
    { "base64": "<base64 编码的图片数据>" },   // base64 / url 二选一
    { "url": "https://example.com/x.jpg" }     // http(s) 图片 URL
  ],
  "context": "可选上下文（如发帖场景/平台），用于 LLM 判定与缓存隔离",
  "skip_llm": false,                            // true = 跳过 LLM 兜底层（强制回退语义层）
  "overrides": {                                // 仅 full 组可用（全部可选）
    "semantic_threshold": 0.7,
    "margin_w": 0.05,
    "confidence_low": 0.35,
    "confidence_high": 0.75
  }
}
```

图片白名单、动图抽帧（GIF 均匀抽帧，任一帧违规 → 整体违规）等能力对 images 自动生效。

### 6.3 响应体（AuditResult）

```jsonc
{
  "request_id": "uuid",                 // 请求唯一标识
  "timestamp": "ISO 8601",              // 完成时间
  "has_violation": true,                // 是否判定违规
  "confidence": 0.83,                   // 综合置信度 0~1
  "category": "色情",                    // 违规类别（违规时给出）
  "source": "semantic",                 // 判定来源（basic_rules_pass / keyword / light_model / semantic / llm ...）
  "cache_hit": false,                   // 是否命中缓存直接返回
  "detail": {                           // 仅 full 组；standard 组为 null
    "keyword":      { "hits": [...], "regex_filtered": [...] },   // 关键词命中 + 正则豁免
    "light_model":  { "label": "违规", "score": 0.9, "violation": true },
    "image_whitelist": [ { "frame": 0, "hit": true, "distance": 2 } ],
    "semantic":     { "black_top": [...], "black_avg": 0.8, "white_avg": 0.3, "margin": 0.5 },
    "llm":          { "is_violation": true, "category": "色情", "confidence": 0.9, "reason": "..." }
  }
}
```

异常响应统一为 `{"error": "<中文/脱敏文案>"}`（400 校验 / 401 鉴权 / 403 overrides / 429 限流 / 500 内部错误，脱敏不回栈）。

### 6.4 示例

```bash
# curl（Key 请替换为「密钥管理」页创建的真实 Key——示例用占位符）
curl -X POST http://127.0.0.1:8000/v1/audit \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: sf_<你的API-KEY>" \
  -d '{"text": "抽到 2000 元话费，加群 xxxxxx 领奖"}'
```

```python
# Python（httpx；项目约定不使用 requests）
import httpx

resp = httpx.post(
    "http://127.0.0.1:8000/v1/audit",
    headers={"X-Api-Key": "sf_<你的API-KEY>"},
    json={"text": "抽到 2000 元话费，加群 xxxxxx 领奖"},
    timeout=30,
)
print(resp.json())
```

管理端快速验证：试运行页（`POST /admin/test-audit`）走管理令牌、等价 full 权限并返回完整 detail，无需创建 API Key。

---

## 管理端操作手册

管理 API（:8001）全部端点均需请求头 `X-Admin-Token: <管理令牌>`（缺省 / 错误 → 401）。错误响应 `{"error": "..."}` 脱敏；分页参数 `page`（从 1 起）/ `page_size`（≤500）。

### 8.1 审核 API 与健康（:8000）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/audit` | 审核请求（见第 6 节契约） |
| GET | `/health` | 免认证健康检查：`{status, version, degraded[], cache, uptime_s}` |

### 8.2 管理端点全表（:8001，共 32 个）

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

### 8.3 Key 前缀匹配说明

`PATCH/DELETE /admin/keys/{key}` 的 `key` 支持两种形态：

1. **完整明文 Key**（精确匹配）；
2. **列表脱敏前缀**（`GET /admin/keys` 回显的「前 8 位 + …」）→ 清理尾字符后做**前缀唯一匹配**：唯一命中即操作该 Key；前缀命中多条 → 400「请提供完整 Key」；零命中 → 404。

这样「列表只回显前缀」也能完成停用/删除操作（v0.3.0 T40 补齐的管理缺口）。

---

## 升级与备份

### 9.1 v0.2.1 → v0.3.0

v0.3.0 引入架构变更（配置 DB 化），升级动作：

1. **先备份 `data/`**（见下）；
2. 用 v0.3.0 代码启动即可，**无需手动迁移**：启动时自动检测旧 `data/config_overrides.json` → 一次性导入 `settings` 表（按叶子点分路径逐组 upsert，幂等）→ 原文件改名归档为 `config_overrides.json.migrated`（已存在归档名时追加时间戳防覆盖）；
3. 迁移失败**仅告警不阻止启动**（DB 空时按默认配置继续），可在日志中观察「配置覆盖层迁移异常 / 已一次性导入」字样；
4. 其他行为变化：
   - 配置修改从「重启生效」变为**保存即生效**（server/logging 除外）；
   - 模型从「启动加载」变为**懒加载**（`degraded` 含 `lazy_pending` 属正常，首次审核或手动装配后清除）；
   - 管理端设置「恢复默认」= 删除该组 DB settings（空对象 PUT），会热应用回退默认。

### 9.2 data/ 备份清单

`data/` 为运行时数据（gitignore，不入库），备份时**全部拷贝**即可：

| 内容 | 路径 | 说明 |
|---|---|---|
| 核心数据库 | `data/audit.db` | 词库 / 规则 / 白名单 / 审核日志 / API Key / **配置 settings 表** 全在其中，最重要 |
| 黑白语料 | `data/corpus/*.csv` | black / white / white_groupchat（可再生，但保留免重跑） |
| 词库原始 CSV | `data/keywords/*.csv` | 归一化输入 / 回溯用 |
| 正则规则 JSON | `data/rules/guardian_*.json` | R1 导出物（可重新 `POST /admin/rules` 导入） |
| 白名单图片 | `data/whitelist/*.png` | 上传原图 |
| 向量库 | `data/vectors/` | npz + meta + done_ids.json（体积大；可由清单重建） |
| 模型缓存 | `data/models/hf/` | 已下载的 CLIP 权重（离线复用，可按需忽略） |
| 复核报告 | `data/review_reports/*.json` | 每次复核轮次的报告 |
| 去重备份 | `data/backups/keywords_dedup_*.zip` | 词库一键去重前的自动备份（含 category/word/source 全量 CSV） |

### 9.3 Docker

```bash
docker compose up -d --build
```

- 端口映射：`8000`（审核 API）/ `8001`（管理 API）；
- 密钥：编辑同目录 `.env`（`docker compose` 自动读取），见 `docker-compose.yml` 注释：`SAFEFUSION_LLM_API_KEY` / `SAFEFUSION_EMBEDDING_API_KEY` / `OPENAI_API_KEY` / `ADMIN_PASSWORD`；
- 数据持久化：`./data` 挂载为容器内 `/app/data`（含 audit.db / 词库 / 向量库 / 白名单 / 模型缓存，**配置 settings 表随库持久化**）；
- 本地 CLIP / GPU：镜像默认不含 ML 依赖（torch/transformers），按 `Dockerfile` 注释改为 `uv sync --frozen --no-dev --extra ml`；GPU 需基于 nvidia/cuda 自行定制；
- 容器内下载模型：需可访问 HuggingFace 或设置 `HF_ENDPOINT` / 代理；离线可在镜像构建时预置权重到 `/app/data/models/hf`。

---

## 常见问题 FAQ

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
docs/               # 用户文档（如有）
```

---

## 数据资产

训练与审核数据（旧 Node 版语料、cherry 文本分类语料、已训模型权重、guardian 规则库等）为私有资产，**不在本开源仓库分发**。归一化脚本 `scripts/normalize_assets.py` 仅在本机从 `node/data` 与 cherry 语料目录构建 `data/` 下的统一布局（词库 / 语料 / 向量导入清单），产物路径全部参数化且已被 `.gitignore` 排除。v0.3.0 附带的 `data/rules/guardian_swear.json`、`data/rules/guardian_ad.json` 与群聊白语料同为私有数据，不入库。

## 开发指南

- 提交代码前阅读根目录 `CLAUDE.md` 与 `开发/rules.md`（工程流程、沙箱约定、硬性规则）
- 内部开发日志 `开发/changelog.md` 不回库；用户视角变更见 `CHANGELOG.md`
- 运行 / 测试 / 检查一律使用 `.venv\Scripts\python.exe -m ...`（禁止 `uv run`）
- 前端构建：`cd web && npm install && npm run build`（产物 `web/dist` 由管理服务托管）

## License

[GPL-3.0](LICENSE)