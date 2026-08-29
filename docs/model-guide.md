# 模型部署与实践

## 1. 懒加载语义（v0.3.0 架构变化）

v0.3.0 起**启动不再装配 / 下载模型**：

- `AppContext` 只保存建造参数，语义引擎以 lazy 占位；启动日志 `degraded` 会包含 `embedding`、`semantic`，健康页语义引擎显示 `lazy_pending`——**这是正常状态**，不触发任何网络请求；
- **首次审核请求**真正需要语义层时触发**单飞装配**（一次只装一次；本机缓存命中秒级，超过等待窗口快速降级返回，不阻塞请求超时）；
- 显式装配入口：设置页「模型」卡点击「装配 / 重新加载」（`POST /admin/models/load`）；
- 装配失败会细分原因码（见 FAQ 原因码表），不会自动重试，需显式重新装配。

## 2. 中文 CLIP 下载与装配

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

## 3. fasttext（轻量文本风险模型）

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

## 4. 常见模型故障排查表

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
