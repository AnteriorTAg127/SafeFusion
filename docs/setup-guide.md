# 安装与配置指南

> 本文是 README「快速开始」的详细展开，包含依赖安装、配置、启动、登录与常见部署问题。

## 1. 安装依赖（uv）

```bash
pip install uv
uv sync            # 核心依赖（FastAPI/uvicorn/numpy/pyahocorasick 等）
uv sync --extra ml # 可选：需要本地 Chinese-CLIP / fasttext 推理时安装 torch/transformers（体积大，首次约数 GB）
```

> 沙箱约定：运行 / 测试 / 检查一律直接调用解释器 `.venv\Scripts\python.exe -m ...`（Windows）或 `.venv/bin/python -m ...`（Linux/macOS），**不要使用 `uv run`**。

## 2. 配置

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

## 3. 启动服务

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

## 4. 登录管理面板

浏览器访问 **http://127.0.0.1:8001/**，输入管理令牌（`X-Admin-Token`）。

**令牌从哪里来（按优先级）**：

| 来源 | 说明 |
|---|---|
| `config.yaml` 的 `admin_token`（或 `admin.token`） | 显式配置 |
| 环境变量 `ADMIN_PASSWORD` | 推荐做法；内存最高优先，**不写入数据库** |
| 数据库 `settings` 表 `admin.token` | 由「设置 → 安全 → 修改密码」持久化（改密后重启且未设环境变量时生效） |
| 启动自动生成 | 以上都未配置时，**启动日志打印一次性令牌**（WARNING 行：「未配置 ADMIN_PASSWORD 环境变量，本次启动自动生成管理令牌（仅此一次输出）：……」）；**重启后变化，需重新获取** |

登录后如词库为空或模型未就绪，会弹出**首次使用三步向导**（令牌 / 数据准备 / 模型与向量库，完成度基于后端真实计数，可跳过、可从顶栏「指南」重新进入）。

## 5. 常见部署问题

| 问题 | 说明 |
|---|---|
| 端口被占用 | 修改 `config.yaml` 的 `server` 分组后重启（端口类配置下次启动生效） |
| 模型下载慢 | 设置 `HF_ENDPOINT=https://hf-mirror.com` 或代理后重新下载 |
| 完全离线 | `build_vector_db.py --model <本地权重目录>`，或配置 `embedding.local.weights_path` 指向本地权重 |
| Docker | 见 README「升级与备份 → Docker」；数据目录 `./data` 已挂载持久化 |
