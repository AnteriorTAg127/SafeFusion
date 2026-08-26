"""轻量文本风险模型：复用 cherry 项目已训的 ``fasttext.pt``（PyTorch 实现）。

推理结构与训练脚本 ``开发/cherry文本分类/训练/fasttext/train_fasttext.py`` 严格一致：

- 特征 = 每个 token 的「整词桶」+ 「字符 n-gram」桶（长度 ``ngram_min..ngram_max``，
  token 首尾以 ``pad_ch`` / ``end_ch`` 填充），全部经 FNV-1a 64 确定性哈希到
  固定大小 ``nbuckets`` 桶（hashing trick，不带词表）；
- 句表示 = 各特征桶 embedding 的均值（bag of n-grams）→ 线性层 → logits →
  softmax 概率；
- 权重由 ``torch.load(map_location="cpu")`` 加载 state dict（``emb.weight`` /
  ``fc.weight`` / ``fc.bias``），超参取自模型配套 ``config.json``。

环境约束（沙箱）：.venv 无 torch，故 **torch 一律在函数内延迟导入**；导入失败或
模型/配置文件缺失时组件 ``disabled=True`` 并记 warning，绝不向外抛异常。

分词说明：训练语料的 ``分词`` 列是 jieba 空格分词结果。推理侧 ``_tokenize`` 优先
尝试 jieba（与训练一致），不可用时降级为按字符切分（字符级 token 仍产生 n-gram
特征，结构一致但分布有差异，详见 T4 报告）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from safefusion.logging_setup import get_logger

logger: logging.Logger = get_logger("engines.light_model")

#: FastTextModel 保存的 state dict 中必须存在的键（与训练脚本类属性一致）
_REQUIRED_STATE_KEYS = ("emb.weight", "fc.weight", "fc.bias")


def fnv1a64(data: bytes) -> int:
    """FNV-1a 64 位哈希（确定性，与训练脚本实现逐字一致）。

    Args:
        data: 待哈希字节串（UTF-8 编码的 token / n-gram）。

    Returns:
        64 位无符号整数哈希值。
    """

    h = 14695981039346656037
    for b in data:
        h ^= b
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def ngram_features(
    token: str,
    *,
    nbuckets: int,
    ngram_min: int,
    ngram_max: int,
    pad_ch: str,
    end_ch: str,
) -> list[int]:
    """计算单个 token 的哈希桶 id 列表（结构与训练脚本 ``ngram_features`` 一致）。

    产出一个整词桶 id（前缀 ``"w:"``）＋ 每个字符 n-gram 的桶 id；token 上
    ``pad_ch + token + end_ch`` 后滑窗取 n-gram。

    Args:
        token: 输入 token（jieba 词或降级后的单字符）。
        nbuckets: 哈希桶数量。
        ngram_min: 字符 n-gram 最小长度。
        ngram_max: 字符 n-gram 最大长度。
        pad_ch: 左填充字符。
        end_ch: 右填充字符。

    Returns:
        按序排列的桶 id 列表（每个 id 位于 ``[0, nbuckets)``）。
    """

    ids = [fnv1a64(("w:" + token).encode("utf-8")) % nbuckets]
    padded = pad_ch + token + end_ch
    for k in range(ngram_min, ngram_max + 1):
        for i in range(len(padded) - k + 1):
            gram = padded[i : i + k]
            ids.append(fnv1a64(gram.encode("utf-8")) % nbuckets)
    return ids


class LightTextModel:
    """fasttext.pt 的轻量文本风险模型（纯 CPU 推理，torch 延迟导入）。

    Args:
        model_path: ``fasttext.pt`` 路径；``None`` 或不存在时组件 disabled。
        config_path: 配套 ``config.json`` 路径；``None`` 或不存在时组件 disabled。
    """

    def __init__(self, model_path: str | None, config_path: str | None) -> None:
        """加载配置与权重；任何失败（缺文件 / torch 缺失 / 权重非法）→ disabled=True。"""

        self.disabled = True
        self._torch: Any = None
        self._emb: Any = None
        self._fc: Any = None
        self._pad_idx = 0
        self._classes: list[str] = []
        self._violation_idx = 0
        self._nbuckets = 0
        self._ngram_min = 3
        self._ngram_max = 6
        self._pad_ch = "<"
        self._end_ch = ">"

        if model_path is None or config_path is None:
            logger.warning("light_model 未配置（model_path/config_path 为 None），组件 disabled")
            return
        model_file = Path(model_path)
        config_file = Path(config_path)
        if not model_file.is_file() or not config_file.is_file():
            logger.warning(
                "light_model 模型或配置文件缺失：%s / %s，组件 disabled",
                model_file,
                config_file,
            )
            return

        try:
            config = self._load_config(config_file)
            torch = self._import_torch()
            state = self._load_state(model_file, torch)
            self._build_modules(torch, state, config)
            self.disabled = False
        except Exception as exc:  # 初始化失败一律降级，绝不向调用方抛异常
            logger.warning("light_model 初始化失败，组件 disabled：%s", exc)
            self.disabled = True

    # ------------------------------------------------------------------ 初始化分段
    @staticmethod
    def _load_config(config_file: Path) -> dict[str, Any]:
        """读取并校验 config.json（nbuckets / emb_dim / ngram / 类别映射等超参）。"""

        with config_file.open("r", encoding="utf-8") as f:
            config = json.load(f)
        required = (
            "nbuckets",
            "emb_dim",
            "ngram_min",
            "ngram_max",
            "pad_ch",
            "end_ch",
            "classes",
            "class_to_idx",
            "violation_class",
        )
        missing = [key for key in required if key not in config]
        if missing:
            raise ValueError(f"config.json 缺少字段: {missing}")
        return config

    @staticmethod
    def _import_torch() -> Any:
        """函数内延迟导入 torch；失败抛出 ImportError 由上层降级。"""

        import torch  # 延迟导入：沙箱 .venv 无 torch 时组件 disabled

        return torch

    @staticmethod
    def _load_state(model_file: Path, torch: Any) -> dict[str, Any]:
        """加载 state dict 并校验必需键与形状来源（emb 行数 == nbuckets+1）。"""

        state = torch.load(model_file, map_location="cpu")
        if not isinstance(state, dict) or not set(_REQUIRED_STATE_KEYS) <= set(state):
            raise ValueError(f"fasttext.pt 不是预期 state dict（缺少键: {_REQUIRED_STATE_KEYS}）")
        emb_weight = state["emb.weight"]
        if emb_weight.shape[0] < 2 or emb_weight.dim() != 2:
            raise ValueError("emb.weight 形状非法")
        return state

    def _build_modules(self, torch: Any, state: dict[str, Any], config: dict[str, Any]) -> None:
        """用 state dict 重建与训练脚本 FastTextModel 等价的 emb + fc 模块。"""

        nn = torch.nn
        nbuckets = int(config["nbuckets"])
        emb_dim = int(config["emb_dim"])
        pad_idx = int(config.get("pad_idx", nbuckets))
        self._pad_idx = pad_idx
        self._nbuckets = nbuckets
        self._ngram_min = int(config["ngram_min"])
        self._ngram_max = int(config["ngram_max"])
        self._pad_ch = str(config["pad_ch"])
        self._end_ch = str(config["end_ch"])
        classes: list[str] = list(config["classes"])
        class_to_idx: dict[str, int] = {str(k): int(v) for k, v in config["class_to_idx"].items()}
        violation_class = str(config["violation_class"])
        if violation_class not in class_to_idx:
            raise ValueError(f"violation_class 不在 class_to_idx 中: {violation_class}")
        self._classes = classes
        self._violation_idx = class_to_idx[violation_class]
        if len(classes) != len(class_to_idx) or set(classes) != set(class_to_idx):
            raise ValueError("classes 与 class_to_idx 不一致")

        emb = nn.Embedding(nbuckets + 1, emb_dim, padding_idx=pad_idx)
        emb.load_state_dict({"weight": state["emb.weight"]})
        fc = nn.Linear(emb_dim, len(classes))
        fc.load_state_dict({"weight": state["fc.weight"], "bias": state["fc.bias"]})
        emb.eval()
        fc.eval()
        self._torch = torch
        self._emb = emb
        self._fc = fc

    # ------------------------------------------------------------------ 推理
    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """jieba 分词（与训练数据一致）；不可用时降级为按字符切分。"""

        try:
            import jieba  # 可选依赖，正式环境建议安装以对齐训练分布

            return list(jieba.cut(text))
        except ImportError:
            return list(text)

    def _feature_ids(self, text: str) -> list[int]:
        """文本 → token → 哈希桶 id 列表（token 级缓存避免重复计算）。"""

        ids: list[int] = []
        cache: dict[str, list[int]] = {}
        for token in self._tokenize(text):
            token_ids = cache.get(token)
            if token_ids is None:
                token_ids = ngram_features(
                    token,
                    nbuckets=self._nbuckets,
                    ngram_min=self._ngram_min,
                    ngram_max=self._ngram_max,
                    pad_ch=self._pad_ch,
                    end_ch=self._end_ch,
                )
                cache[token] = token_ids
            ids.extend(token_ids)
        return ids

    def _forward(self, ids: list[int]) -> Any:
        """与训练脚本 ``FastTextModel.forward`` 相同的 bag-of-ngrams 前向。

        空特征（空文本）时输出 pad 行＋全零 mask，对应训练 collate 的语义：
        embedding 均值为 0，logits 退化为 fc 偏置。
        """

        torch = self._torch
        if not ids:
            x = torch.full((1, 1), self._pad_idx, dtype=torch.long)
            mask = torch.zeros((1, 1), dtype=torch.float32)
        else:
            x = torch.as_tensor([ids], dtype=torch.long)
            mask = torch.ones((1, len(ids)), dtype=torch.float32)
        h = self._emb(x)  # (1, L, D)
        h = (h * mask.unsqueeze(-1)).sum(dim=1)
        h = h / mask.sum(dim=1, keepdim=True).clamp(min=1.0)
        return self._fc(h)  # (1, C)

    def predict(self, text: str) -> dict[str, Any] | None:
        """对单条文本做风险预测。

        Args:
            text: 待检测文本（无需预分词）。

        Returns:
            ``{"label": 类别名, "score": 违规类 softmax 概率(0~1),
            "violation": 是否判定违规}``；组件 disabled 或推理失败时返回 ``None``
            （失败已记 error 日志，调用方按降级处理）。
        """

        if self.disabled or self._emb is None:
            return None
        try:
            logits = self._forward(self._feature_ids(text))
            probs = self._torch.softmax(logits, dim=1)[0]
            label_idx = int(logits.argmax(dim=1)[0])
            score_t = probs[self._violation_idx]
            # detach 消除推理告警；FakeTorch（单测）无 detach 时原样透传
            score = score_t.detach() if hasattr(score_t, "detach") else score_t
            return {
                "label": self._classes[label_idx],
                "score": float(score),
                "violation": label_idx == self._violation_idx,
            }
        except Exception as exc:  # 单条失败不拖垮整批，降级返回 None
            logger.error("light_model 推理失败（文本长度=%d）: %s", len(text), exc)
            return None
