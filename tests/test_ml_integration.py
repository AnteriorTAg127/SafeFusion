"""集成（ML）用例清单 — SafeFusion v0.2（T21 定义，默认不运行）。

这些用例需要真实 ML 环境（torch/transformers + Chinese-CLIP 权重 + 已构建
向量库 + fasttext 结构轻量模型），**只定义不实跑**：默认 pytest 配置
（``addopts = "-m 'not integration'"``）将其排除；由主模型在提权 / 用户环境
执行。执行方式：:

    .venv\\Scripts\\python.exe -m pytest tests/test_ml_integration.py
    -m integration -q -p no:cacheprovider

依赖注记（详见 开发/v0.2/test/test_0.md §4）：
- ML 依赖：``uv sync --extra ml``（torch/transformers）；
- Chinese-CLIP 权重：HF_HOME / TRANSFORMERS_CACHE 自动下载，或
  ``embedding.local.weights_path`` 指向本地权重目录（离线）；
- 向量库：``scripts/build_vector_db.py`` 产物 ``data/vectors/{black,white}.{npz,meta.json}``
  （13.6 万条清单，验收 black 96786 / white 39135）；
- 轻量模型：``light_model.model_path/config_path`` 指向 fasttext 结构模型
  （开发/cherry文本分类/部署/model/fasttext.pt）。

每例内部以 ``pytest.importorskip`` / ``pytest.skip`` 守卫：依赖缺失时跳过而非
崩溃，便于在 ML 环境渐进点亮。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _require_deps() -> None:
    pytest.importorskip("torch")
    pytest.importorskip("transformers")


@pytest.mark.integration
def test_real_chinese_clip_text_encode() -> None:
    """真实 Chinese-CLIP 文本编码：中文语义向量输出（vit-base-patch16 → 512 维）。

    依赖：uv sync --extra ml + HF_HOME 权重（或本地 weights_path）。
    """
    _require_deps()
    from safefusion.engines.embedding import LocalChineseCLIP

    model_name = os.environ.get("SF_CLIP_MODEL") or "OFA-Sys/chinese-clip-vit-base-patch16"
    backend = LocalChineseCLIP(
        model_name=model_name, device=os.environ.get("SF_CLIP_DEVICE", "cpu")
    )
    vecs = backend.encode_texts(["正常内容", "违规样例"])
    assert vecs.ndim == 2
    assert vecs.shape[0] == 2
    assert vecs.shape[1] == 512  # vit-base-patch16 的 CLIP 文本维度


@pytest.mark.integration
def test_real_chinese_clip_image_encode() -> None:
    """真实 Chinese-CLIP 图像编码：与文本同维（多模态链路前置）。

    依赖：同上 + 有效图片字节（本地生成即可）。
    """
    _require_deps()
    import io

    from PIL import Image

    from safefusion.engines.embedding import LocalChineseCLIP

    model_name = os.environ.get("SF_CLIP_MODEL") or "OFA-Sys/chinese-clip-vit-base-patch16"
    backend = LocalChineseCLIP(
        model_name=model_name, device=os.environ.get("SF_CLIP_DEVICE", "cpu")
    )
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, format="PNG")
    buf.seek(0)
    vecs = backend.encode_images([Image.open(buf).convert("RGB")])
    assert vecs.shape[1] == 512  # 与文本向量同维，可做图文融合


@pytest.mark.integration
def test_real_light_model_predict() -> None:
    """真实轻量文本模型（fasttext 结构 fasttext.pt）预测：输出 violation 判定。

    依赖：light_model.model_path/config_path 指向已训练模型文件
    （开发/cherry文本分类/部署/model/）；未配置时跳过。
    """
    model_path = os.environ.get("SF_LIGHT_MODEL_PATH")
    config_path = os.environ.get("SF_LIGHT_MODEL_CONFIG")
    if not model_path or not config_path:
        pytest.skip("未提供 SF_LIGHT_MODEL_PATH / SF_LIGHT_MODEL_CONFIG（fasttext.pt 路径）")
    _require_deps()
    from safefusion.engines.light_model import LightTextModel

    model = LightTextModel(model_path, config_path)
    assert not model.disabled
    result = model.predict("这是一条正常文本内容")
    assert result is not None
    assert "violation" in result and "probability" in result


@pytest.mark.integration
def test_real_vector_db_build_smoke() -> None:
    """真实向量库构建冒烟：build_vector_db --max-rows 10（真实 CLIP 编码 + 入库）。

    依赖：ML 依赖 + 权重 + ``data/vectors/import_manifest.jsonl``（T12 归一化产物）。
    """
    manifest = Path("data/vectors/import_manifest.jsonl")
    if not manifest.is_file():
        pytest.skip("缺少 data/vectors/import_manifest.jsonl（先运行 scripts/normalize_assets.py）")
    _require_deps()
    import importlib.util
    import tempfile

    script = Path(__file__).resolve().parents[1] / "scripts" / "build_vector_db.py"
    spec = importlib.util.spec_from_file_location("build_vector_db", script)
    assert spec is not None and spec.loader is not None
    bv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bv)

    with tempfile.TemporaryDirectory() as tmp:
        rc = bv.main(["--manifest", str(manifest), "--out", tmp, "--max-rows", "10"])
        assert rc == 0
        from safefusion.storage.vector_store import NumpyVectorStore

        store = NumpyVectorStore.load(tmp)
        assert store.count("black") + store.count("white") == 10


@pytest.mark.integration
def test_real_semantic_four_signal() -> None:
    """真实四信号语义置信度：真实向量库（build 产物）+ 真实 CLIP + Rerank 开启。

    依赖：uv sync --extra ml + 权重 + ``data/vectors/{black,white}.npz`` 已构建。
    验收（PRD v0.2 §6.5）：rerank_black_max 参与置信度且方向合理。
    """
    vectors_dir = Path("data/vectors")
    if not (vectors_dir / "black.npz").is_file() or not (vectors_dir / "white.npz").is_file():
        pytest.skip("缺少 data/vectors 向量库（先运行 scripts/build_vector_db.py 全量构建）")
    _require_deps()
    import numpy as np

    from safefusion.engines.embedding import LocalChineseCLIP
    from safefusion.engines.rerank import get_rerank_backend
    from safefusion.engines.semantic import SemanticEngine
    from safefusion.storage.vector_store import NumpyVectorStore

    model_name = os.environ.get("SF_CLIP_MODEL") or "OFA-Sys/chinese-clip-vit-base-patch16"
    backend = LocalChineseCLIP(
        model_name=model_name, device=os.environ.get("SF_CLIP_DEVICE", "cpu")
    )
    store = NumpyVectorStore.load(str(vectors_dir))
    sem_cfg = {
        "rerank_enabled": True,
        "rerank_w_top": 0.5,
        "rerank_w_margin": 0.3,
        "rerank_w_rerank": 0.2,
    }
    engine = SemanticEngine(backend, store, thresholds=sem_cfg)
    out = engine.audit("一条明确的违规内容样例", [])
    assert out["reason"] is None  # 语义层真实可用（未降级）
    assert out["rerank_black_max"] is not None
    assert 0.0 <= out["confidence"] <= 1.0
    assert isinstance(out["black_top"], dict)
    # 真实后端可替换性：按 rerank_enabled 路由出的后端可对候选重排
    backend_obj = get_rerank_backend(sem_cfg, backend)
    reranked = backend_obj.rerank(
        np.zeros(512, dtype=np.float32),
        [{"id": "x", "score": 0.5, "metadata": {"text": "占位"}}],
    )
    assert isinstance(reranked, list)
    assert reranked[0]["rerank_score"] == pytest.approx(0.5)  # 无内容候选保底原 score


@pytest.mark.integration
def test_real_rerank_backend_local_clip() -> None:
    """真实 Rerank 后端（决策 A）：本地 CLIP 对黑库候选独立二次编码成对重排。

    依赖：uv sync --extra ml + 权重。验收：rerank_score 为余弦值且排序生效。
    """
    _require_deps()
    from safefusion.engines.embedding import LocalChineseCLIP
    from safefusion.engines.rerank import LocalClipRerank

    model_name = os.environ.get("SF_CLIP_MODEL") or "OFA-Sys/chinese-clip-vit-base-patch16"
    backend = LocalChineseCLIP(
        model_name=model_name, device=os.environ.get("SF_CLIP_DEVICE", "cpu")
    )
    query = backend.encode_texts(["查询样例"])[0]
    candidates = [
        {"id": "b1", "score": 0.8, "metadata": {"text": "候选文本甲"}},
        {"id": "b2", "score": 0.6, "metadata": {"text": "略不相关的候选文本乙"}},
    ]
    out = LocalClipRerank(backend).rerank(query, candidates)
    assert len(out) == 2
    assert all("rerank_score" in item for item in out)
    assert out[0]["rerank_score"] >= out[1]["rerank_score"]


@pytest.mark.integration
def test_real_health_no_ml_degradation() -> None:
    """真实装配下 /health 不再含 embedding / semantic / light_model 降级（PRD v0.2 §6.2）。

    依赖：uv sync --extra ml + 权重 + data/vectors + light_model 配置齐全。
    """
    _require_deps()
    from safefusion.config import load_config
    from safefusion.core.context import AppContext

    cfg = load_config(None)
    ctx = AppContext.build(cfg)
    assert "embedding" not in ctx.degraded
    assert "semantic" not in ctx.degraded
    assert "light_model" not in ctx.degraded
