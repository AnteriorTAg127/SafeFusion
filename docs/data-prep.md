# 数据准备向导

> 目标：新用户 30 分钟完成数据准备。所有数据放在 `data/` 目录（已 gitignore，不入库）。管理面板顶栏「指南」内含数据准备清单入口。

## 1. 逐项清单

| 数据项 | 来源 | 格式 | 放置路径 | 生成 / 导入命令 | 导入入口 | 产物 |
|---|---|---|---|---|---|---|
| **词库** | 旧 Node 版 `keywords/` + cherry 分类语料 | CSV（`类别,词` 两列，UTF-8 可带 BOM）；单条 TXT（每行一词 + 类别） | 原始 CSV 放 `data/keywords/*.csv`；入库后存 SQLite | `.venv\Scripts\python.exe scripts\normalize_assets.py`（生成 CSV）；管理端「词库」页导入，或 `POST /admin/keywords/import` | 前端「词库管理」页 | SQLite `keywords` 表（类别+词 唯一，重复自动跳过） |
| **黑白语料** | 旧 Node 版白/违规 CSV + cherry 文本分类语料 | CSV（`text,label,category,source`） | `data/corpus/black.csv`、`data/corpus/white.csv`、`data/corpus/white_groupchat.csv` | `scripts/normalize_assets.py`（生成 corpus 与导入清单）；**白群聊语料合并**见下 | 「试运行」页随机示例（读语料头部抽样 20 条）；`scripts/build_vector_db.py` 编码入库 | 向量库黑白池 + 试运行示例 |
| **正则规则** | guardian 规则库（`开发/cherry文本分类/部署/guardian_benchmark/db/lexicon.db`，R1 提取） | JSON 数组 `[{category, pattern, action, note}]` 或 CSV（`category,pattern,action`） | 已导出 `data/rules/guardian_swear.json`（24,014 条）/ `guardian_ad.json`（517 条） | v0.3.0 已直接导入 SQLite（合计 24,531 条，action=violate）；再次导入用 `POST /admin/rules` | 前端「规则管理」页（JSON/CSV 导入） | SQLite `rules` 表；规则引擎热重载后参与正则消歧 |
| **白名单图片** | 用户提供（合规素材） | PNG / JPEG / GIF 等 | 上传后自动存 `data/whitelist/{md5}.png` | 无需手工命令 | 前端「图片白名单」页多图上传（multipart `files` 字段），`POST /admin/whitelist/images` | SQLite `whitelist_meta` 表（md5 + pHash）+ 磁盘原图 |
| **图片语料**（语义检索用） | 用户自采（R3 指南） | jpg / png 为主（长边 ≥ 256px 为宜；gif/webp/bmp 亦可） | `data/images/black/`、`data/images/white/`（子目录按类别随意嵌套） | `scripts/normalize_assets.py --images-dir data\images`（生成图片清单） | —（清单供向量库构建） | `data/vectors/import_manifest_images.jsonl`（每行一张图，与文本清单分离） |
| **向量库** | 黑白语料清单（文本 + 图片） | JSONL（`{pool,text,image_path,...}`） | `data/vectors/import_manifest.jsonl`（文本，189,262 行：black 96,786 + white 92,476 含群聊白）、`import_manifest_images.jsonl`（图片） | `.venv\Scripts\python.exe scripts\build_vector_db.py`（CLIP 批量编码） | —（由脚本构建，管理端只有状态查看） | `data/vectors/` 下 black/white 双池 npz + meta + done_ids.json |

## 2. 语料与词库（normalize_assets）

从旧 Node 版数据与 cherry 语料构建统一词库 / 语料 / 向量导入清单（产出不入 git）：

```bash
.venv\Scripts\python.exe scripts\normalize_assets.py --dry-run   # 只统计不写盘
.venv\Scripts\python.exe scripts\normalize_assets.py             # 写盘到 ./data（已 gitignore）
```

参数（`--help` 全量）：`--node-data`（旧 Node 版数据目录）/ `--cherry-data`（cherry 语料目录）/ `--out`（输出目录）/ `--max-rows`（清单行数上限，0=全量）/ `--dry-run` / `--images-dir` / `--image-pool`。

## 3. 群聊白语料合并（v0.3.0）

R2 已从真实群聊记录清洗出白语料 `data/corpus/white_groupchat.csv`（**新增 53,341 条**，label=0 / category=groupchat / source=node:白群聊.csv，与既有白语料跨库去重）。合并后白池总数 = 39,135 + 53,341 = **92,476 条**。

- 若要让群聊语料参与**语义检索**：执行 `scripts\merge_manifest.py` 将 `white_groupchat.csv` 并入白池清单（池内 `(pool,text)` 去重，幂等），再执行 `build_vector_db.py` 重建白池；
- 若要让群聊语料出现在**「试运行」随机示例**：试运行抽样读取 `data/corpus/white.csv` 头部（每池 ≤400 候选），把群聊语料合入该文件即可；
- 现状：`data/corpus/` 中 `white.csv` 与 `white_groupchat.csv` 两个文件并存，未改动既有人工语料；是否重归一化由部署方按需执行（幂等，`done_ids.json` 断点续跑）。

## 4. 图片语料（R3 指南要点）

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

## 5. 构建向量库（build_vector_db）

```bash
# 本地 Chinese-CLIP（默认，GPU 优先；全量 18.9 万条）
.venv\Scripts\python.exe scripts\build_vector_db.py
# 云端 / 本地 OpenAI 兼容 Embedding API（如 llama.cpp llama-server --embeddings）
# 最优并发参数（RTX 2080Ti + WeMM-Embedding-2B 实测约 40 条/s，全量约 1.3h）：
.venv\Scripts\python.exe scripts\build_vector_db.py --backend cloud `
    --base-url http://127.0.0.1:5545/v1 --cloud-model WeMM-Embedding-2B-Q4_K_M.gguf `
    --no-api-key --batch 128 --workers 8
.venv\Scripts\python.exe scripts\build_vector_db.py --dry-run       # 只统计不编码
.venv\Scripts\python.exe scripts\build_vector_db.py --max-rows 5    # 调试小样本
.venv\Scripts\python.exe scripts\build_vector_db.py --model <本地权重目录>  # 离线 / 内网环境
```

- **后端选择**：`--backend local`（默认，Chinese-CLIP）| `--backend cloud`（OpenAI 兼容 API，`--base-url` 必填，`--no-api-key` 用于本地无鉴权服务如 llama.cpp）；
- **并发调优**：cloud 后端支持 `--workers N`（默认 8，线程池并发编码，每 worker 独立连接）与 `--batch N`（默认 64）。实测 RTX 2080 Ti + WeMM-Embedding-2B（llama.cpp）最优为 **batch 128 × workers 8 ≈ 40 条/s**；服务端需 `--parallel ≥ workers` 且 `-c = parallel × 2048`（llama.cpp 共享上下文，每并发需约 2048 token）；可用 `scripts/bench_embedding.py` 网格测试找本机最优；
- **权重**：local 后端首次运行自动从 HuggingFace 下载 `OFA-Sys/chinese-clip-vit-base-patch16`（数百 MB）；离线环境用 `--model` 指向已下载权重；cloud 后端无本地权重依赖；
- **断点续跑**：已编码 id 记录在 `data/vectors/done_ids.json`，中断后重跑自动跳过；**全量重建** = 清空 `data/vectors/` 后重跑；
- 其余参数：`--manifest`（清单路径）/ `--out` / `--device auto|cpu|cuda` / `--resume / --no-resume` / `--cloud-timeout`（cloud 单次请求超时）；
- 图片清单（`import_manifest_images.jsonl`）同样经此脚本编码入库（`is_image_row` 分支按 `image_path` 走图片编码管线）。
