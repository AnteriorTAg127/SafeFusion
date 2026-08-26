"""配置加载模块：内置默认值 → YAML 覆盖 → 环境变量覆盖（密钥仅从环境变量读取）。

职责：
- 定义全部配置模型（``AppConfig`` 及其分组子模型），默认值严格对齐
  ``开发/v0.1/分工.md``「配置键」一节与 PRD v2.1；
- ``load_config(path)`` 按上述三层顺序合并配置；
- 密钥类字段（``llm.api_key`` / ``embedding.cloud.api_key``）只允许来自环境变量，
  YAML 中出现的 ``api_key`` 键会被忽略并告警；
- 任意标量配置可用 ``SAFEFUSION_<路径>_<键>`` 环境变量覆盖，
  例如 ``SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD=0.7``；
- 密钥环境变量规范名：``SAFEFUSION_LLM_API_KEY`` / ``SAFEFUSION_EMBEDDING_API_KEY``，
  未设置时回退到配置的 ``api_key_env`` 所指变量名。
"""

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger("safefusion.config")

#: 环境变量前缀：``SAFEFUSION_<路径>_<键>``
_ENV_PREFIX = "SAFEFUSION_"


class _BaseConfig(BaseModel):
    """配置模型公共基类：拒绝未知键、赋值时校验（供环境变量字符串自动转型）。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ServerConfig(_BaseConfig):
    """服务监听配置。"""

    host: str = Field(default="0.0.0.0", description="审核 API 监听地址")
    port: int = Field(default=8000, description="审核 API 端口（:8000）")
    admin_port: int = Field(default=8001, description="管理 API 端口（:8001）")


class ThresholdsConfig(_BaseConfig):
    """判定阈值（默认值来自 PRD §3.4 与分工文档，上线后按真实数据校准）。"""

    semantic_threshold: float = Field(default=0.67, description="语义层判定违规的相似度阈值")
    margin_w: float = Field(default=0.05, description="黑均分−白均分差值与 margin 的比较基准")
    confidence_low: float = Field(default=0.35, description="置信度低档上界，低于则判定安全")
    confidence_high: float = Field(default=0.75, description="置信度高档下界，高于则判定违规")
    phash_whitelist_distance: int = Field(default=5, description="图片白名单 pHash 汉明距离阈值")
    phash_dedup_distance: int = Field(default=3, description="图片去重缓存近似命中 pHash 阈值")


class EmbeddingLocalConfig(_BaseConfig):
    """本地 Chinese-CLIP 后端配置。"""

    model_name: str = Field(
        default="OFA-Sys/chinese-clip-vit-base-patch16",
        description="HF 模型名或本地权重标识",
    )
    weights_path: str | None = Field(default=None, description="本地权重目录；null 使用 HF 缓存")
    device: str = Field(default="auto", description="auto（GPU 可用则用）| cpu | cuda")


class EmbeddingCloudConfig(_BaseConfig):
    """云端 Embedding API 后端配置（OpenAI 兼容风格）。"""

    base_url: str | None = Field(default=None, description="云端 Embedding API base_url")
    model: str | None = Field(default=None, description="云端 embedding 模型名")
    api_key_env: str | None = Field(
        default=None,
        description="云端 Key 环境变量名；null 时仅认 SAFEFUSION_EMBEDDING_API_KEY",
    )
    api_key: str | None = Field(default=None, description="云端 Key（仅从环境变量解析）")


class EmbeddingConfig(_BaseConfig):
    """Embedding 双后端总配置。"""

    backend: str = Field(default="local", description="local（默认）| cloud")
    local: EmbeddingLocalConfig = Field(
        default_factory=EmbeddingLocalConfig, description="本地后端"
    )
    cloud: EmbeddingCloudConfig = Field(
        default_factory=EmbeddingCloudConfig, description="云端后端"
    )


class LLMConfig(_BaseConfig):
    """LLM 兜底配置（OpenAI 兼容）。"""

    base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI 兼容服务地址")
    model: str = Field(default="gpt-4o-mini", description="兜底模型名")
    api_key_env: str = Field(
        default="OPENAI_API_KEY", description="Key 环境变量名；也认 SAFEFUSION_LLM_API_KEY"
    )
    timeout: float = Field(default=3.0, description="单次调用超时（秒）")
    max_retry: int = Field(default=1, description="JSON 输出解析失败重试次数")
    short_text_max_length: int = Field(default=20, description="短文本 LLM 缓存判定的文本长度上限")
    api_key: str | None = Field(default=None, description="LLM Key（仅从环境变量解析）")


class CacheItemConfig(_BaseConfig):
    """单级缓存配置（capacity = 最大条目数，ttl = 秒）。"""

    enabled: bool = Field(default=True, description="开关；关闭时该级缓存直通")
    capacity: int = Field(description="容量上限（条目数）")
    ttl: float = Field(description="TTL（秒）")


class CacheConfig(_BaseConfig):
    """五级缓存总配置（v0.2 新增 backend/redis 双后端）。"""

    backend: str = Field(
        default="memory",
        description="缓存后端：memory（进程内，默认）| redis",
    )
    redis: "RedisCacheConfig" = Field(
        default_factory=lambda: RedisCacheConfig(),
        description="Redis 后端连接配置",
    )
    audit_cache: CacheItemConfig = Field(
        default_factory=lambda: CacheItemConfig(enabled=True, capacity=2000, ttl=3600),
        description="① 审核缓存：完整键（文本哈希+帧哈希+关键参数）",
    )
    high_freq_cache: CacheItemConfig = Field(
        default_factory=lambda: CacheItemConfig(enabled=True, capacity=1000, ttl=300),
        description="② 高频缓存：无上下文请求（LRU+TTL）",
    )
    dedup_cache: CacheItemConfig = Field(
        default_factory=lambda: CacheItemConfig(enabled=True, capacity=5000, ttl=86400),
        description="③ 图片去重缓存：仅单图无文本请求",
    )
    short_text_llm_cache: CacheItemConfig = Field(
        default_factory=lambda: CacheItemConfig(enabled=True, capacity=2000, ttl=86400),
        description="④ 短文本 LLM 缓存",
    )
    permanent_lists: bool = Field(
        default=True, description="⑤ 永久黑白名单：启动加载，管理端写入即失效"
    )


class RedisCacheConfig(_BaseConfig):
    """Redis 缓存后端连接配置（v0.2 新增）。"""

    url: str = Field(default="redis://127.0.0.1:6379/0", description="Redis 连接 URL")
    prefix: str = Field(default="sf:", description="缓存键统一前缀")


class ImageConfig(_BaseConfig):
    """图片处理配置（v0.2 新增动图抽帧）。"""

    animated: "AnimatedImageConfig" = Field(
        default_factory=lambda: AnimatedImageConfig(), description="动图抽帧配置"
    )


class AnimatedImageConfig(_BaseConfig):
    """动图（GIF）抽帧配置（v0.2 新增，PRD v0.2 M3）。"""

    enabled: bool = Field(default=True, description="false 时退回 v0.1 首帧降级行为")
    frames: int = Field(default=5, description="均匀抽帧数（3~5，可配）")
    mode: str = Field(default="uniform", description="抽帧模式：uniform 均匀 | first 首帧")


class KeywordConfig(_BaseConfig):
    """关键词层配置（v0.2 新增正则规则开关）。"""

    regex_rules_enabled: bool = Field(
        default=True, description="正则消歧规则库开关；false 时规则层跳过"
    )


class SemanticConfig(_BaseConfig):
    """语义层扩展配置（v0.2 新增 Rerank 四信号）。"""

    rerank_enabled: bool = Field(default=False, description="Rerank 开关（默认关）")
    rerank_w_top: float = Field(default=0.5, description="黑库最高相似度权重")
    rerank_w_margin: float = Field(default=0.3, description="黑白均值差权重")
    rerank_w_rerank: float = Field(default=0.2, description="Rerank 分数权重")
    rerank_top_k: int = Field(default=5, description="Rerank 候选数")


class ReviewConfig(_BaseConfig):
    """定时复核配置（v0.2 新增 M7）。"""

    interval_min: int = Field(default=240, description="复核周期（分钟）；0 禁用自动调度")
    band_low: float = Field(default=0.35, description="采样下界（置信度中带）")
    band_high: float = Field(default=0.75, description="采样上界（置信度中带）")
    sample_size: int = Field(default=50, description="每轮采样上限")
    auto_tune: bool = Field(default=False, description="是否自动采纳阈值建议（默认仅出报告）")


class LightModelConfig(_BaseConfig):
    """轻量文本风险模型配置（复用已训 fasttext.pt）。"""

    model_path: str | None = Field(
        default=None, description="fasttext.pt 路径；null = 未启用（组件 disabled）"
    )
    config_path: str | None = Field(
        default=None, description="模型配套 config.json 路径；null = 未启用"
    )


class LoggingConfig(_BaseConfig):
    """日志配置。"""

    level: str = Field(default="INFO", description="日志级别 DEBUG/INFO/WARNING/ERROR")
    json_lines: bool = Field(default=True, description="true = JSON 行；false = 标准文本格式")


class AppConfig(_BaseConfig):
    """应用总配置：server / data_dir / thresholds / embedding / llm / cache / light_model / logging / image / keyword / semantic / review。"""  # noqa: E501

    server: ServerConfig = Field(default_factory=ServerConfig, description="服务监听配置")
    data_dir: str = Field(default="./data", description="运行时数据目录（相对路径基于启动目录）")
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig, description="判定阈值")
    embedding: EmbeddingConfig = Field(
        default_factory=EmbeddingConfig, description="Embedding 双后端"
    )
    llm: LLMConfig = Field(default_factory=LLMConfig, description="LLM 兜底")
    cache: CacheConfig = Field(default_factory=CacheConfig, description="五级缓存")
    light_model: LightModelConfig = Field(
        default_factory=LightModelConfig, description="轻量文本风险模型"
    )
    logging: LoggingConfig = Field(default_factory=LoggingConfig, description="日志配置")
    image: ImageConfig = Field(default_factory=ImageConfig, description="图片处理（动图抽帧）")
    keyword: KeywordConfig = Field(
        default_factory=KeywordConfig, description="关键词层（正则规则开关）"
    )
    semantic: SemanticConfig = Field(
        default_factory=SemanticConfig, description="语义层（Rerank 四信号）"
    )
    review: ReviewConfig = Field(default_factory=ReviewConfig, description="定时复核")


def load_config(path: str | None = None) -> AppConfig:
    """按「默认值 → YAML → 环境变量」顺序加载配置并解析密钥。

    Args:
        path: YAML 配置文件路径；为 None 时仅使用默认值 + 环境变量。

    Returns:
        合并完成的 AppConfig。返回前会执行密钥解析（仅环境变量来源）。

    Raises:
        FileNotFoundError: 指定了 path 但文件不存在。
        ValueError: YAML 顶层不是映射字典，或包含未知配置键
            （由 pydantic extra=forbid 触发）。
    """
    data: dict[str, Any] = {}
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.is_file():
            raise FileNotFoundError(f"配置文件不存在: {cfg_path}（参考 config.example.yaml）")
        with cfg_path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("配置文件顶层必须是映射（YAML 字典）")
        data = loaded

    # ① 密钥类字段不允许来自 YAML：递归删除 api_key 键（含嵌套）
    _strip_secret_keys_from_yaml(data)

    # ② 默认值 + YAML 覆盖（缺失键自动取默认）
    cfg = AppConfig.model_validate(data)

    # ③ 环境变量覆盖（路径逐一匹配已知标量键；密钥类除外）
    _apply_env_overrides(cfg)

    # ④ 密钥解析：仅环境变量来源
    _resolve_secret_keys(cfg)
    return cfg


def _strip_secret_keys_from_yaml(data: dict[str, Any]) -> None:
    """递归移除 YAML 中的密钥字段（``api_key``），密钥只允许来自环境变量。"""

    if not isinstance(data, dict):
        return
    for key, value in list(data.items()):
        if key == "api_key":
            if value is not None:
                _logger.warning("配置中禁止填写 api_key（密钥只从环境变量读取），已忽略")
            del data[key]
        elif isinstance(value, dict):
            _strip_secret_keys_from_yaml(value)


def _iter_scalar_targets(
    model: Any, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, tuple[Any, str]]]:
    """遍历配置模型的所有标量叶子，产出 (环境变量后缀, (持有字段的模型实例, 字段名))。"""

    for name, field in type(model).model_fields.items():
        ann = field.annotation
        if ann is not None and isinstance(ann, type) and issubclass(ann, BaseModel):
            yield from _iter_scalar_targets(getattr(model, name), prefix + (name,))
        else:
            yield "_".join((*prefix, name)).lower(), (model, name)


def _apply_env_overrides(cfg: AppConfig) -> None:
    """将 ``SAFEFUSION_<路径>_<键>`` 环境变量应用到配置。

    密钥类键名以 ``_api_key`` 结尾者跳过，由 ``_resolve_secret_keys`` 解析；
    未识别的 SAFEFUSION_* 变量记录警告；字符串值经 pydantic 赋值校验自动转型。
    """

    targets = dict(_iter_scalar_targets(cfg))
    for name, raw in os.environ.items():
        if not name.startswith(_ENV_PREFIX):
            continue
        suffix = name[len(_ENV_PREFIX) :].lower()
        if suffix.endswith("_api_key"):
            continue
        target = targets.get(suffix)
        if target is None:
            _logger.warning("忽略未识别的配置环境变量: %s", name)
            continue
        holder, leaf = target
        setattr(holder, leaf, raw)


def _resolve_secret_keys(cfg: AppConfig) -> None:
    """从环境变量解析密钥：先认规范名 SAFEFUSION_<模块>_API_KEY，回退到 api_key_env 指定变量名。"""

    llm_env = os.environ.get("SAFEFUSION_LLM_API_KEY") or os.environ.get(cfg.llm.api_key_env)
    cfg.llm.api_key = llm_env or None

    cloud_env = os.environ.get("SAFEFUSION_EMBEDDING_API_KEY")
    if cloud_env is None and cfg.embedding.cloud.api_key_env:
        cloud_env = os.environ.get(cfg.embedding.cloud.api_key_env)
    cfg.embedding.cloud.api_key = cloud_env or None
