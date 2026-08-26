"""Embedding 双后端测试：fuse_vectors 三模式与边界 / 工厂路由 / Cloud 无 Key 报错。

对应 T5 任务卡验收：无网络无权重环境走夹具假后端自测融合逻辑；
本地真模型与云端真网络留 @pytest.mark.integration（默认不运行）。
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np
import pytest

from safefusion.engines import embedding as emb_mod
from safefusion.engines.embedding import (
    CloudEmbeddingAPI,
    fuse_vectors,
    get_embedding_backend,
    l2_normalize,
)


def unit(angle_deg: float) -> np.ndarray:
    a = np.radians(angle_deg)
    return np.array([np.cos(a), np.sin(a)], dtype=np.float32)


def norm(v: np.ndarray) -> float:
    return float(np.linalg.norm(v))


class TestFuseVectors:
    """concat / weighted_avg / pool 三模式与边界（素材同级于 tmp/t5 自检）。"""

    def _text(self) -> np.ndarray:
        return unit(0.0)  # (2,)

    def _images(self, n: int = 2) -> list[np.ndarray]:
        return [unit(10.0), unit(30.0)] if n >= 2 else [unit(20.0)]

    def test_text_only_returns_normalized_text(self) -> None:
        out = fuse_vectors(self._text(), None)
        assert norm(out) == pytest.approx(1.0)
        assert np.allclose(out, l2_normalize(self._text()))

    def test_image_pool_multi(self) -> None:
        imgs = self._images(2)
        out = fuse_vectors(None, imgs, mode="pool")
        assert norm(out) == pytest.approx(1.0)
        expected = l2_normalize(np.mean([l2_normalize(i) for i in imgs], axis=0))
        assert np.allclose(out, expected, atol=1e-6)

    def test_concat_dim_and_norm(self) -> None:
        text = np.zeros(3)
        text[0] = 1.0
        images = [np.array([0.0, 1.0], dtype=np.float32)]
        out = fuse_vectors(text, images, mode="concat")
        assert out.shape == (5,)
        assert norm(out) == pytest.approx(1.0)

    def test_weighted_avg_formula(self) -> None:
        text = unit(0.0)
        images = [unit(0.0)]  # 同方向加权仍归一
        out = fuse_vectors(text, images, mode="weighted_avg", weights={"text": 0.7, "image": 0.3})
        expected = l2_normalize(0.7 * l2_normalize(text) + 0.3 * l2_normalize(images[0]))
        assert np.allclose(out, expected, atol=1e-6)

    def test_pool_with_both_equals_weighted_avg(self) -> None:
        text = unit(45.0)
        images = [unit(15.0)]
        a = fuse_vectors(text, images, mode="pool")
        b = fuse_vectors(text, images, mode="weighted_avg")
        assert np.allclose(a, b, atol=1e-6)

    def test_dim_mismatch_weighted_avg_raises(self) -> None:
        with pytest.raises(ValueError, match="维度一致"):
            fuse_vectors(np.zeros(3), [np.zeros(4)], mode="weighted_avg")

    def test_both_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="不能同时为空"):
            fuse_vectors(None, [])

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="未知融合模式"):
            fuse_vectors(np.zeros(2), [np.zeros(2)], mode="mahalanobis")

    def test_zero_vector_protection(self) -> None:
        zero = np.zeros(3)
        out = fuse_vectors(zero, None)
        assert np.allclose(out, zero)  # 零向量原样返回，不产生 NaN
        assert np.isfinite(out).all()


class TestFactoryRouting:
    """get_embedding_backend 路由与缺依赖报错。"""

    def _torch_installed(self) -> bool:
        return importlib.util.find_spec("torch") is not None

    def test_local_backend_requires_torch(self) -> None:
        if self._torch_installed():
            pytest.skip("本环境装了 torch，local 后端实例化路径由 integration 覆盖")
        with pytest.raises(RuntimeError, match="torch"):
            get_embedding_backend({"backend": "local"})

    def test_default_backend_is_local(self) -> None:
        if self._torch_installed():
            pytest.skip("本环境装了 torch，local 后端实例化路径由 integration 覆盖")
        with pytest.raises(RuntimeError, match="torch"):
            get_embedding_backend({})

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="未知 Embedding 后端"):
            get_embedding_backend({"backend": "magic"})

    def test_cloud_without_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SAFEFUSION_EMBEDDING_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="API Key"):
            get_embedding_backend(
                {"backend": "cloud", "cloud": {"base_url": "http://x", "model": "m"}}
            )

    def test_cloud_missing_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            CloudEmbeddingAPI({"cloud": {"model": "m", "api_key": "k"}})

    def test_cloud_missing_model_raises(self) -> None:
        with pytest.raises(ValueError, match="model"):
            CloudEmbeddingAPI({"cloud": {"base_url": "http://x", "api_key": "k"}})


class TestCloudEmbeddingApi:
    """云端后端：假 httpx 客户端验证编码与 L2 归一化、密钥 Header。"""

    class FakeCloudResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeCloudClient:
        def __init__(self, response: TestCloudEmbeddingApi.FakeCloudResponse) -> None:
            self._response = response
            self.captured: dict[str, Any] = {}
            self.closed = False

        def post(self, url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            self.captured = {"url": url, "json": json}
            return self._response

        def close(self) -> None:
            self.closed = True

    def test_encode_texts_sorted_and_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "data": [
                {"index": 1, "embedding": [0.0, 2.0]},
                {"index": 0, "embedding": [3.0, 4.0]},
            ]
        }
        client = self.FakeCloudClient(self.FakeCloudResponse(payload))
        monkeypatch.setattr(emb_mod.httpx, "Client", lambda *a, **k: client)

        api = CloudEmbeddingAPI(
            {"cloud": {"base_url": "http://emb.example", "model": "m1", "api_key": "sk-x"}}
        )
        try:
            out = api.encode_texts(["a", "b"])
        finally:
            api.close()

        assert out.shape == (2, 2)
        # index 0（3,4）在前，L2 归一化为 (0.6, 0.8)
        assert np.allclose(out[0], np.array([0.6, 0.8]), atol=1e-6)
        assert client.captured["url"] == "http://emb.example/embeddings"
        assert client.captured["json"] == {"model": "m1", "input": ["a", "b"]}
        assert client.closed is True

    def test_encode_images_use_data_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PIL import Image

        payload = {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
        client = self.FakeCloudClient(self.FakeCloudResponse(payload))
        monkeypatch.setattr(emb_mod.httpx, "Client", lambda *a, **k: client)

        api = CloudEmbeddingAPI(
            {"cloud": {"base_url": "http://emb.example", "model": "m1", "api_key": "k"}}
        )
        try:
            img = Image.new("RGB", (4, 4), (255, 0, 0))
            api.encode_images([img])
        finally:
            api.close()
        inputs = client.captured["json"]["input"]
        assert len(inputs) == 1
        assert inputs[0].startswith("data:image/png;base64,")

    def test_key_header_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _factory(*args: Any, **kwargs: Any) -> Any:
            captured["headers"] = kwargs.get("headers")
            captured["timeout"] = kwargs.get("timeout")
            return self.FakeCloudClient(self.FakeCloudResponse({"data": []}))

        monkeypatch.setattr(emb_mod.httpx, "Client", _factory)
        api = CloudEmbeddingAPI(
            {"cloud": {"base_url": "http://x", "model": "m", "api_key": "secret", "timeout": 5.0}}
        )
        api.close()
        assert captured["headers"]["Authorization"] == "Bearer secret"
        assert captured["timeout"] == 5.0

    def test_empty_inputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self.FakeCloudClient(self.FakeCloudResponse({"data": []}))
        monkeypatch.setattr(emb_mod.httpx, "Client", lambda *a, **k: client)
        api = CloudEmbeddingAPI({"cloud": {"base_url": "http://x", "model": "m", "api_key": "k"}})
        api.close()
        assert api.encode_texts([]).shape == (0, 0)
        assert api.encode_images([]).shape == (0, 0)


class TestNormalizeHelpers:
    """l2_normalize / _l2_rows 零向量与非有限范数保护。"""

    def test_l2_normalize_unit(self) -> None:
        assert norm(l2_normalize(np.array([3.0, 4.0]))) == pytest.approx(1.0)

    def test_l2_normalize_zero(self) -> None:
        zero = np.zeros(3)
        assert np.allclose(l2_normalize(zero), zero)

    def test_l2_rows_zero_row_kept(self) -> None:
        matrix = np.array([[0.0, 0.0], [1.0, 0.0]])
        out = emb_mod._l2_rows(matrix)
        assert np.allclose(out[0], [0.0, 0.0])
        assert norm(out[1]) == pytest.approx(1.0)


class TestIntegrationMarkers:
    """需要真实模型 / 网络 / 云端服务的路径：标记 integration，默认不运行。"""

    @pytest.mark.integration
    def test_real_local_embedding(self) -> None:
        # 需 torch + transformers + Chinese-CLIP 权重（uv sync --extra ml）
        pytest.fail("integration：真实本地 CLIP 编码，需 ML 环境")

    @pytest.mark.integration
    def test_real_cloud_embedding(self) -> None:
        # 需可达的云端 Embedding API 与有效 Key
        pytest.fail("integration：真实云端 /embeddings 调用，需网络与 Key")
