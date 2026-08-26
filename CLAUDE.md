# CLAUDE.md — SafeFusion 项目协作规范

本文件是所有 AI Agent（主模型 / 子代理）在本仓库工作前的必读规范。

## 项目定位

SafeFusion —— **语义检索 + 关键词规则** 双通道融合的多模态违规内容识别引擎，覆盖文本与图片。

- 语言：Python（具体版本以 PRD 为准）
- 协议：GPL-3.0
- 当前状态：**需求评审阶段**（PRD 经用户确认定稿前，禁止编写任何业务代码）
- 重要：本项目**不是 AstrBot 插件**。开发流程参考 `astrbot-create-plugin` skill 的工程方法并做了去 AstrBot 化改造，不引入任何 AstrBot 依赖。

## 强制开发流程（顺序不可跳过）

| 阶段 | 内容 | 产物 |
|---|---|---|
| 1 | 创建开发目录与规则文件 | `开发/rules.md` |
| 2 | 需求评审：多轮向用户提问，直到能写出完整 PRD 且用户确认 | `开发/v0.1/prd.md` |
| 3 | 主模型负责任务拆解，写分工文档（每个模块一张任务卡） | `开发/v0.1/分工.md` |
| 4 | 建立内部变更日志 | `开发/changelog.md` |
| 5 | 派子代理按任务卡构建代码（无依赖的模块可并行） | 源码 + changelog 记录 |
| 6 | 子代理扫描代码、识别测试点、编写测试用例 | `开发/v0.1/test/test_0.md` |
| 7 | 执行测试并记录结果 | 同上追加结果 |
| 8 | 失败用例根因分析与修复方案 | `开发/v0.1/debug/debug_0.md` |
| 9 | 派子代理执行修复 | 修复 + changelog 记录 |
| 10 | 回到阶段 7 循环，直到全部用例通过 | — |
| 11 | ruff check + ruff format 全量通过 | `开发/v0.1/ruff_report.md` |
| 12 | 更新根目录 README.md 与 CHANGELOG.md（提交 git） | 文档 |
| 13 | 清理不应提交的文件，核对 .gitignore 与密钥安全 | 干净的工作区 |

## 目录约定

```text
开发/            过程文档（rules、changelog、v0.x/prd、分工、test、debug）
                 —— 已被 .gitignore 排除，永不提交 git
src/safefusion/  主包（结构以 PRD 为准，编码阶段建立）
tests/           pytest 用例
docs/            用户文档（如有）
data/            运行时数据 / 词库 / 本地模型缓存（不入库）
```

注意区分两个 changelog：

- `开发/changelog.md` — 内部开发日志，记录每次子代理变更，**不提交 git**
- `CHANGELOG.md`（根目录）— 面向用户的版本变更说明，**提交 git**

## 子代理协作方式（主模型规划 + 子代理构建）

- 主模型负责规划、拆解、集成与审查；子代理只按分工文档中的任务卡编码。
- 任务卡必须包含：目标、产出文件路径、输入输出接口约定、验收标准、关联的 PRD 条目编号。
- **每个子代理开工前必须先读取 `开发/rules.md`**；完工后必须更新 `开发/changelog.md`。
- 按文件边界拆分任务，避免多个子代理修改同一文件；先基础设施（包骨架、配置加载）后功能模块，集成层最后做。

## 工程命令（uv 环境，含沙箱约定）

- 依赖管理统一使用 **uv**：`uv sync`（生成/复用 `.venv` 与 `uv.lock`）、加 ML 依赖用 `uv sync --extra ml`（torch/transformers，本地 CLIP 与 fasttext 推理需要）。
- **沙箱约定（重要）**：uv 查询解释器走管道 stdio，会被文件沙箱拒绝——只有主代理可提权执行 `uv sync`；**一切运行/测试/检查一律直接调用解释器，禁止 `uv run`**：
  ```text
  .venv\Scripts\python.exe -m pytest          # 测试
  .venv\Scripts\python.exe -m ruff check src  # 静态检查
  .venv\Scripts\python.exe -m uvicorn ...     # 启动服务
  ```
- uv 缓存已通过项目级 `uv.toml` 收敛到 `.uv-cache/`（已 gitignore）；`uv.lock` 必须提交。
- 子代理禁止自行安装依赖；缺依赖时在报告中说明，由主代理统一处理。

## 硬性规则

1. 密钥 / API Key 只能来自环境变量或 gitignored 配置文件；严禁写入任何会提交的文件、文档或日志。
2. `开发/` 目录永不提交 git。
3. 异步网络请求使用 aiohttp / httpx，禁止使用 requests。
4. 持久化数据放 `data/` 目录，不放源码目录。
5. 提交信息格式 `<type>: <摘要>`，type ∈ feat / fix / docs / test / chore / refactor。
6. 需求细节不确定时回到用户确认，禁止臆断后大规模开工。
7. 对用户呈现的结论须注明依据（PRD 条目 / 测试结果 / 官方文档）。
