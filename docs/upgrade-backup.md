# 升级与备份

## 1. v0.2.1 → v0.3.0

v0.3.0 引入架构变更（配置 DB 化），升级动作：

1. **先备份 `data/`**（见下）；
2. 用 v0.3.0 代码启动即可，**无需手动迁移**：启动时自动检测旧 `data/config_overrides.json` → 一次性导入 `settings` 表（按叶子点分路径逐组 upsert，幂等）→ 原文件改名归档为 `config_overrides.json.migrated`（已存在归档名时追加时间戳防覆盖）；
3. 迁移失败**仅告警不阻止启动**（DB 空时按默认配置继续），可在日志中观察「配置覆盖层迁移异常 / 已一次性导入」字样；
4. 其他行为变化：
   - 配置修改从「重启生效」变为**保存即生效**（server/logging 除外）；
   - 模型从「启动加载」变为**懒加载**（`degraded` 含 `lazy_pending` 属正常，首次审核或手动装配后清除）；
   - 管理端设置「恢复默认」= 删除该组 DB settings（空对象 PUT），会热应用回退默认。

## 2. data/ 备份清单

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

## 3. Docker

```bash
docker compose up -d --build
```

- 端口映射：`8000`（审核 API）/ `8001`（管理 API）；
- 密钥：编辑同目录 `.env`（`docker compose` 自动读取），见 `docker-compose.yml` 注释：`SAFEFUSION_LLM_API_KEY` / `SAFEFUSION_EMBEDDING_API_KEY` / `OPENAI_API_KEY` / `ADMIN_PASSWORD`；
- 数据持久化：`./data` 挂载为容器内 `/app/data`（含 audit.db / 词库 / 向量库 / 白名单 / 模型缓存，**配置 settings 表随库持久化**）；
- 本地 CLIP / GPU：镜像默认不含 ML 依赖（torch/transformers），按 `Dockerfile` 注释改为 `uv sync --frozen --no-dev --extra ml`；GPU 需基于 nvidia/cuda 自行定制；
- 容器内下载模型：需可访问 HuggingFace 或设置 `HF_ENDPOINT` / 代理；离线可在镜像构建时预置权重到 `/app/data/models/hf`。
