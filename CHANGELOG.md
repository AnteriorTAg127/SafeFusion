# Changelog

本文件记录 SafeFusion 面向用户的版本变更（[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循语义化版本）。内部开发日志见 `开发/changelog.md`（不入库）；真实数据不在本仓库分发，见 README「数据资产」。

## [0.1.0] - 2026-08-26

### Added

- **工程骨架**：uv 依赖管理（`uv.lock` 锁定）、ruff 质量门、配置三层加载（默认值 → YAML → 环境变量，密钥仅经环境变量）、JSON 行结构化日志
- **数据契约层**：审核请求 / 响应模型（`AuditRequest` / `AuditResult` 及 detail 子模型，对齐 PRD §4 契约，含 standard/full 分级字段）
- **存储层**：SQLite DAO（API Key / 词库 / 白名单 / 审核日志四表，WAL + 过滤分页）与自研 numpy 向量库（L2 归一化余弦 Top-K 检索、按池 npz 持久化、增量插入）
- **基础规则层**：
  - 关键词引擎：Aho-Corasick 自动机 + 拼音（全拼 / 首字母）、全半角、繁简、符号替换变体展开，命中回映射原文位置
  - 正则消歧引擎：xs豁免 / 追加违规规则
  - 轻量文本风险模型：复用已训 fasttext.pt（PyTorch 实现，缺模型自动 disabled 不抛异常）
  - 图片管线：URL / base64 解码、GIF 取首帧、MD5 + pHash、白名单匹配
- **语义层**：
  - Embedding 双后端：本地 Chinese-CLIP（GPU 自动 / CPU 降级，权重路径可配）与云端 Embedding API（OpenAI 兼容），可插拔
  - 向量融合：concat / 加权平均 / 池化，输出 L2 归一化
  - 语义引擎：黑白对抗检索 + 三信号置信度，降级原因码区分不可用与安全
- **LLM 兜底**：OpenAI 兼容客户端（输入净化防注入 + 防护系统提示词 + 强制 JSON 结构化输出，失败重试后回退语义层）
- **缓存层**：五级进程内缓存（审核缓存完整键 / 高频 LRU+TTL / 图片去重 MD5+pHash / 短文本 LLM / 永久黑白名单），每级独立开关与统计
- **资产归一化脚本**：`scripts/normalize_assets.py` 一键从旧 Node 数据与 cherry 语料构建统一布局（词库 CSV / 黑白语料 CSV / 向量导入清单 JSONL），支持 dry-run 统计与行数上限
- **部署物**：`Dockerfile`（python:3.12-slim + uv）、`docker-compose.yml`（8000/8001 端口、数据卷、密钥 env 注入）、`.dockerignore`
- **文档**：README（架构、快速开始、API 概览、目录结构）、本 CHANGELOG、配置示例 `config.example.yaml`

### 待开发（v0.1 内）

- 编排决策层（T9）：汇总决策（无短路）、三档置信度动作、请求级参数覆盖
- 审核 API（T10）：`POST /v1/audit`、`GET /health`
- 管理 API（T11）：Key / 词库 / 图片白名单 / 审核日志 / 向量重建五组端点
- 测试专项（T13）与 v0.1 验收（PRD §8）