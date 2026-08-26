# ============================================================
# SafeFusion —— 审核服务镜像（v0.1，CPU 基础版）
#
# 使用 uv 安装依赖（--frozen 锁定 uv.lock / --no-dev 不含开发组）；
# 本镜像默认不安装 ML 可选依赖（torch/transformers，见 pyproject [ml] extra）：
#   - 需要本地 Chinese-CLIP / fasttext 推理时，改为
#       RUN uv sync --frozen --no-dev --extra ml
#     （torch 镜像体积大，需 GPU 请自行基于 nvidia/cuda 定制，勿直接改基础镜像）
#
# 双服务说明（审核 API :8000 / 管理 API :8001）：
#   两个服务建议分别以两个进程启动（uvicorn 单 worker 各一个端口）；
#   T10 落地 src/safefusion/api/__main__.py 后，统一入口 `python -m safefusion.api`
#   将按 server.port / server.admin_port 同时拉起双服务（见下方 CMD TODO）。
#   当前阶段（T10 未完成）镜像主要用于资产归一化与开发验证环境——
#   直接 `docker compose run --rm safefusion python scripts/normalize_assets.py --dry-run`
#   即可在容器内跑归一化（数据目录通过卷挂载进入）。
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PIP_NO_CACHE_DIR=1

# 安装 uv（备选：FROM ghcr.io/astral-sh/uv 拷贝二进制，二选一）
RUN pip install --no-cache-dir uv

WORKDIR /app

# 先拷贝依赖清单以利用构建缓存层
COPY pyproject.toml uv.lock uv.toml ./
RUN uv sync --frozen --no-dev

# 再拷贝源码与脚本
COPY src/ ./src/
COPY scripts/ ./scripts/

EXPOSE 8000 8001

# TODO(T10)：审核/管理双服务统一入口落地后启用；
# 在此之前容器仅用于资产归一化与开发调试（见文件头说明），
# 双 worker 部署建议：:8000 审核 API 与 :8001 管理 API 各一个 uvicorn 进程。
CMD ["python", "-m", "safefusion.api"]