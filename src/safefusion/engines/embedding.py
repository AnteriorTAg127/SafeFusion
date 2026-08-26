"""多模态 Embedding 双后端模块。

提供（对齐 PRD §3.3 与 ``开发/v0.1/分工.md`` 统一接口契约）：

- :class:`BaseEmbedding` —— Embedding 后端抽象基类；
- :class:`LocalChineseCLIP` —— 本地 Chinese-CLIP 后端（默认，GPU 优先 CPU 降级）；
- :class:`CloudEmbeddingAPI` —— OpenAI 兼容云端 Embedding API 后端；
- :func:`get_embedding_backend` —— 双后端工厂（``cfg["backend"]`` 选 local/cloud）；
- :func:`fuse_vectors` —— 分模态编码后的向量融合（concat / weighted_avg / pool）。

设计要点：

- 两后端输出一律 L2 归一化，零向量原样返回（保护，不产生 NaN）；
- 本地后端依赖的 ``torch`` / ``transformers`` 延迟到实例化时才导入，
  缺失时实例化抛 ``RuntimeError`` 并给出安装指引（``uv sync --extra ml``）；
- 云端后端使用同步 ``httpx.Client`` 按 OpenAI 风格调用
  ``{base_url}/embeddings``，图像转 base64 data URI；密钥只从环境变量读取
  （``embedding.cloud.api_key_env`` 指定变量名，兜底 ``SAFEFUSION_EMBEDDING_API_KEY``），
  缺失时实例化抛 ``RuntimeError``。
"""

from __future__ import annotations

import base64
import io
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx
import numpy as np
from PIL import Image

_logger = logging.getLogger("safefusion.engines.embedding")

#: 云端输入图像的 MIME 类型映射（Pillow 格式名 → data URI MIME）
_FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}
#: 云端请求默认超时（秒）
_DEFAULT_CLOUD_TIMEOUT = 10.0
#: 云端 Key 规范名兜底环境变量（与 config.py 的 _resolve_secret_keys 一致）
_STANDARD_KEY_ENV = "SAFEFUSION_EMBEDDING_API_KEY"
#: fuse_vectors 默认融合权重（text / image）
_DEFAULT_FUSE_WEIGHTS: dict[str, float] = {"text": 0.4, "image": 0.6}


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    """L2 归一化单个向量，零向量 / 非有限范数时原样返回（保护，不产生 NaN）。"""
    vector = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(vector)
    if norm == 0 or not np.isfinite(norm):
        return vector
    return vector / norm


def _l2_rows(matrix: np.ndarray) -> np.ndarray:
    """逐行 L2 归一化矩阵；空矩阵或零向量行原样保留，不产生 NaN。"""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.shape[0] == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe = np.where((norms == 0) | ~np.isfinite(norms), 1.0, norms)
    return matrix / safe


def _features_to_numpy(features: Any) -> np.ndarray:
    """把 CLIP 特征输出归一化为 L2 向量矩阵。

    transformers 4.x 的 ``get_text_features/get_image_features`` 直接返回张量
    ``(n, d)``；5.x 返回 ``BaseModelOutputWithPooling`` 数据类（``.pooler_output``
    即投影后张量）。shim 兼容两者（T15 报告阻塞项，主模型 2026-08-26）。
    """

    raw = getattr(features, "pooler_output", features)
    return _l2_rows(np.asarray(raw.cpu().numpy()))


class BaseEmbedding(ABC):
    """Embedding 后端抽象基类。

    子类只需实现 :meth:`encode_texts` 与 :meth:`encode_images`，两者都必须
    返回形状 ``(n, d)`` 的 ``float32`` 矩阵且输出已 L2 归一化。

    ``supports_mixed_input`` 声明后端是否天然支持文本+图片联合编码；
    不影响本类接口——两类编码始终分别调用，由调用方按需融合。
    """

    #: 是否支持文本+图片合并为一次编码请求
    supports_mixed_input: bool = False

    @abstractmethod
    def encode_texts(self, texts: list[str]) -> np.ndarray:
        """编码文本列表，返回形状 ``(n, d)`` 的 L2 归一化向量矩阵。"""

    @abstractmethod
    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        """编码图片列表，返回形状 ``(n, d)`` 的 L2 归一化向量矩阵。"""


class LocalChineseCLIP(BaseEmbedding):
    """本地 Chinese-CLIP 后端（默认，模型 ``OFA-Sys/chinese-clip-vit-base-patch16``）。

    模型名 / 权重路径 / 设备均从配置读取（``embedding.local`` 分组）：

    - ``model_name``：HF 模型名或本地权重标识（默认 OFA-Sys/chinese-clip-vit-base-patch16）；
    - ``weights_path``：本地权重目录；配置后优先于 model_name 加载；
    - ``device``：``auto``（torch.cuda.is_available 自动选择 cuda/cpu）| ``cpu`` | ``cuda``。

    ``torch`` 与 ``transformers`` 延迟到实例化时才导入；缺失时抛
    ``RuntimeError``（含 ``uv sync --extra ml`` 安装指引）。
    模型支持图文联合编码，故 ``supports_mixed_input=True``，但本类仍按
    契约分别实现 :meth:`encode_texts` / :meth:`encode_images`。
    """

    supports_mixed_input = True

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or {}
        local = _as_dict(cfg.get("local") or {})
        self.model_name: str = str(
            local.get("model_name") or "OFA-Sys/chinese-clip-vit-base-patch16"
        )
        self.weights_path: str | None = local.get("weights_path")
        self.device: str = str(local.get("device") or "auto")

        # 懒加载：torch / transformers 仅在实例化时导入
        try:
            import torch  # type: ignore[import-not-found]
            from transformers import (  # type: ignore[import-not-found]
                AutoConfig,
                ChineseCLIPModel,
                ChineseCLIPProcessor,
                CLIPModel,
                CLIPProcessor,
            )
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "本地 Chinese-CLIP 后端需要 torch 与 transformers，当前环境未安装。"
                "请执行 `uv sync --extra ml`（或 `pip install torch transformers`）"
                "安装 ML 依赖后重试。"
            ) from exc

        self._torch = torch
        device = self.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device

        model_id = self.weights_path or self.model_name
        _logger.info("加载 Chinese-CLIP 模型 %s（device=%s）", model_id, device)
        # Chinese-CLIP 的文本编码器是 BERT 结构，CLIPModel 无法装载其权重
        # （大量 MISSING/UNEXPECTED 键 → 文本特征为随机初始化垃圾）。按
        # config.model_type 选择专用类 ChineseCLIPModel/ChineseCLIPProcessor，
        # 其余模型回退 CLIP*（主模型修复，2026-08-26，M2 实测捕获）。
        try:
            model_cfg = AutoConfig.from_pretrained(model_id)
            is_chinese_clip = getattr(model_cfg, "model_type", "") == "chinese_clip"
        except Exception:
            is_chinese_clip = "chinese" in model_id.lower() and "clip" in model_id.lower()
        if is_chinese_clip:
            self._model = ChineseCLIPModel.from_pretrained(model_id)
            self._processor = ChineseCLIPProcessor.from_pretrained(model_id)
        else:
            self._model = CLIPModel.from_pretrained(model_id)
            self._processor = CLIPProcessor.from_pretrained(model_id)
        self._model.to(device)
        self._model.eval()
        self._output_dim: int = int(self._model.config.projection_dim)
        # 文本编码器最大序列长度（Chinese-CLIP BERT=512；后续 encode_texts 显式截断）
        text_cfg = getattr(self._model.config, "text_config", None)
        self._text_max: int = (
            int(getattr(text_cfg, "max_position_embeddings", 512))
            if text_cfg is not None
            else 512
        )

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        """编码文本列表，返回形状 ``(n, d)`` 的 L2 归一化向量矩阵。"""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        # 显式 max_length：长文本（违规语料含数百字）必须截断到文本编码器
        # 上限，否则 batch 与单条编码都会抛序列长度错误而被跳过。
        inputs = self._processor(
            text=texts,
            padding=True,
            truncation=True,
            max_length=self._text_max,
            return_tensors="pt",
        )
        inputs = {key: value.to(self._device) for key, value in inputs.items()}
        with self._torch.no_grad():
            features = self._model.get_text_features(**inputs)
        return _features_to_numpy(features)

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        """编码图片列表，返回形状 ``(n, d)`` 的 L2 归一化向量矩阵。"""
        if not images:
            return np.zeros((0, 0), dtype=np.float32)
        inputs = self._processor(images=images, return_tensors="pt")
        inputs = {key: value.to(self._device) for key, value in inputs.items()}
        with self._torch.no_grad():
            features = self._model.get_image_features(**inputs)
        return _features_to_numpy(features)


class CloudEmbeddingAPI(BaseEmbedding):
    """OpenAI 兼容云端 Embedding 后端。

    同步 ``httpx.Client`` POST ``{base_url}/embeddings``，请求体为 OpenAI 风格
    ``{"model": ..., "input": [...]}``：文本直接作为字符串列表，图像转
    base64 data URI 字符串列表。云端接口不支持图文合并编码，故
    ``supports_mixed_input=False``，图文融合由调用方经 :func:`fuse_vectors` 完成。

    配置（``embedding.cloud`` 分组）：

    - ``base_url``：服务地址（必填，拼 ``/embeddings`` 调用）；
    - ``model``：embedding 模型名（必填）；
    - ``api_key_env``：Key 环境变量名（可选）；未设置时兜底读
      ``SAFEFUSION_EMBEDDING_API_KEY``。二者均缺失实例化抛 ``RuntimeError``。

    图像 base64 编码在内存进行（Pillow BytesIO），不入盘、不写日志。
    """

    supports_mixed_input = False

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or {}
        cloud = _as_dict(cfg.get("cloud") or {})
        self.base_url: str = str(cloud.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("云端 Embedding 后端需要配置 embedding.cloud.base_url")
        self.model: str = str(cloud.get("model") or "")
        if not self.model:
            raise ValueError("云端 Embedding 后端需要配置 embedding.cloud.model")

        api_key = self._resolve_api_key(cloud)
        if not api_key:
            env_hint = cloud.get("api_key_env") or _STANDARD_KEY_ENV
            raise RuntimeError(
                "云端 Embedding 后端未配置 API Key（密钥只允许来自环境变量）。"
                f"请设置环境变量 {env_hint}（或 YAML 中 embedding.cloud.api_key_env"
                " 指向的变量名）后重启服务。"
            )

        timeout_ms = float(cloud.get("timeout", _DEFAULT_CLOUD_TIMEOUT))
        self._client = httpx.Client(
            timeout=timeout_ms,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        #: 云端输出维度未知，首次编码后记录（空输入时不可用）
        self._output_dim: int | None = None

    @staticmethod
    def _resolve_api_key(cloud: dict[str, Any]) -> str | None:
        """解析云端 Key：cfg 已解析值 → api_key_env 指定变量 → 规范名兜底。"""
        api_key = cloud.get("api_key")
        if not api_key:
            env_name = cloud.get("api_key_env")
            if env_name:
                api_key = os.environ.get(str(env_name))
        if not api_key:
            api_key = os.environ.get(_STANDARD_KEY_ENV)
        return api_key or None

    @staticmethod
    def _image_to_data_uri(image: Image.Image) -> str:
        """将 PIL 图像转为 base64 data URI 字符串（内存 BytesIO，不落盘）。"""
        fmt = (image.format or "PNG").upper()
        save_fmt = fmt if fmt in _FORMAT_TO_MIME else "PNG"
        mime = _FORMAT_TO_MIME.get(save_fmt, "image/png")
        buf = io.BytesIO()
        image.save(buf, format=save_fmt)
        payload = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:{mime};base64,{payload}"

    def _encode(self, inputs: list[str]) -> np.ndarray:
        """发起一次 /embeddings 请求并返回 L2 归一化向量矩阵（按 index 排序）。"""
        payload: dict[str, Any] = {"model": self.model, "input": inputs}
        resp = self._client.post(f"{self.base_url}/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()
        items = sorted(data["data"], key=lambda item: int(item["index"]))
        matrix = np.asarray([item["embedding"] for item in items], dtype=np.float32)
        vectors = _l2_rows(matrix)
        self._output_dim = int(vectors.shape[1])
        return vectors

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        """编码文本列表，返回形状 ``(n, d)`` 的 L2 归一化向量矩阵。"""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        return self._encode(texts)

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        """编码图片列表（转 base64 data URI），返回 ``(n, d)`` 归一化矩阵。"""
        if not images:
            return np.zeros((0, 0), dtype=np.float32)
        return self._encode([self._image_to_data_uri(image) for image in images])

    def close(self) -> None:
        """关闭底层 httpx 客户端连接。"""
        self._client.close()


def get_embedding_backend(cfg: dict[str, Any] | Any = None) -> BaseEmbedding:
    """Embedding 后端工厂。

    Args:
        cfg: embedding 分组配置（含 ``backend`` / ``local`` / ``cloud`` 键；
            接受 dict 或 T1 的 ``EmbeddingConfig`` pydantic 模型）。``backend``
            取值 ``local``（默认）| ``cloud``。

    Returns:
        对应后端实例。local 后端在缺失 torch/transformers 时实例化抛
        ``RuntimeError``；cloud 后端在缺失 Key 时实例化抛 ``RuntimeError``。

    Raises:
        ValueError: backend 取值未知。
    """
    cfg_dict = _as_dict(cfg)
    backend = cfg_dict.get("backend", "local")
    if backend == "local":
        return LocalChineseCLIP(cfg_dict)
    if backend == "cloud":
        return CloudEmbeddingAPI(cfg_dict)
    raise ValueError(f"未知 Embedding 后端: {backend!r}（可选 local/cloud）")


def fuse_vectors(
    text_vec: np.ndarray | None,
    image_vecs: list[np.ndarray] | None,
    mode: str = "weighted_avg",
    weights: dict[str, float] | None = None,
) -> np.ndarray:
    """融合文本与图片向量为单个查询向量（分模态编码时的统一融合入口）。

    横向（图像侧）先做池化：单图取其自身，多图取平均；池化后再 L2 归一化。
    边界：仅文本 / 仅图（含空列表）时直接返回该侧归一化向量；两侧同时为空抛
    ``ValueError``。

    Args:
        text_vec: 文本向量 (d,)，可为 None。
        image_vecs: 图片向量列表 [(d,), ...]，可为 None 或空列表。
        mode:
            - ``"concat"``：文本与图像池化结果按权重缩放后拼接，再 L2
              归一化；输出维度为两者维度之和。
            - ``"weighted_avg"``：文本与图像池化结果按权重加权平均，再 L2
              归一化；要求两者维度一致，否则抛 ``ValueError``。
            - ``"pool"``：图像平均池化（多图→一图）；仅图时返回池化向量，
              图文并存时按权重融合（算法与 weighted_avg 一致）。
        weights: 融合权重 ``{"text": 0.4, "image": 0.6}`` 默认，可配。

    Returns:
        一维 L2 归一化向量。

    Raises:
        ValueError: 两侧同时为空；未知融合模式；weighted_avg/pool 维度不一致。
    """
    w = {**_DEFAULT_FUSE_WEIGHTS, **(weights or {})}
    w_text = float(w.get("text", _DEFAULT_FUSE_WEIGHTS["text"]))
    w_image = float(w.get("image", _DEFAULT_FUSE_WEIGHTS["image"]))

    has_text = text_vec is not None
    has_images = image_vecs is not None and len(image_vecs) > 0
    if not has_text and not has_images:
        raise ValueError("fuse_vectors: text_vec 与 image_vecs 不能同时为空")

    if mode not in ("concat", "weighted_avg", "pool"):
        raise ValueError(f"未知融合模式: {mode!r}（可选 concat/weighted_avg/pool）")

    # 图像侧池化：单图自身，多图平均；两侧都保留一步归一化
    image_vec: np.ndarray | None = None
    if has_images:
        stacked = np.stack(
            [l2_normalize(np.asarray(vec, dtype=np.float32)) for vec in image_vecs],
            axis=0,
        )
        image_vec = l2_normalize(np.mean(stacked, axis=0))

    text_vec_n = l2_normalize(text_vec) if has_text else None

    # 单边：直接返回该侧归一化向量
    if has_text and not has_images:
        return text_vec_n
    if has_images and not has_text:
        return image_vec

    if image_vec is not None and text_vec_n is not None:
        if mode == "concat":
            fused = np.concatenate([w_text * text_vec_n, w_image * image_vec])
            return l2_normalize(fused)
        if text_vec_n.shape != image_vec.shape:
            raise ValueError(
                "fuse_vectors: weighted_avg/pool 要求文本与图像向量维度一致，"
                f"实际 text={text_vec_n.shape} image={image_vec.shape}"
            )
        fused = w_text * text_vec_n + w_image * image_vec
        return l2_normalize(fused)

    raise ValueError("fuse_vectors: 内部状态异常（两侧存在性校验失败）")


def _as_dict(value: Any) -> dict[str, Any]:
    """将 dict 或 pydantic BaseModel 归一为普通 dict（pydantic 模型则 model_dump）。"""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"期望 dict 或 pydantic 配置模型，实际为 {type(value).__name__}")
