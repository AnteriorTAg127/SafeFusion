"""应用上下文聚合壳：集中持有配置与全部组件实例，并提供装配入口。

``AppContext.build(config)`` 按依赖顺序装配全部组件（T9 集成核心）：

- 存储：SQLite ``Database``（``data_dir/audit.db``，目录自动创建）+ 自研
  ``NumpyVectorStore``（``data_dir/vectors`` 下有 ``black.npz`` / ``white.npz``
  则 load，否则构造空库并确保 save 目录存在）；
- 基础组件：``CacheLayer``（config.cache 的 dict 形态）、``KeywordEngine``
  （从 Database.list_keywords 加载词库 + ``config.keyword.regex_rules_enabled``
  开启时从 rules 表加载正则消歧规则，空词库/空规则正常）、``LightTextModel``
  （model_path/config_path 为 None 或缺文件时 disabled）、
  ``WhitelistMatcher``（注入 Database 实例）；
- 多模态组件：``get_embedding_backend``（local 缺 torch / cloud 缺 Key 抛
  RuntimeError → embedding=None 并 warning）、``SemanticEngine``（仅当
  embedding 与 store 均可用）、``LLMClient``（config.llm 的 dict）。

装配策略：**组件级降级，全程不抛**。任一组件装配失败时该字段保持 None 并记
warning，``degraded`` 字段汇总全部未成功装配（None / disabled / unavailable）
的组件名清单，供运维与 /health 观测。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..cache.caches import CacheLayer
from ..engines.embedding import get_embedding_backend
from ..engines.image_pipeline import WhitelistMatcher
from ..engines.keyword_engine import KeywordEngine
from ..engines.light_model import LightTextModel
from ..engines.llm_client import LLMClient
from ..engines.semantic import SemanticEngine
from ..logging_setup import get_logger
from ..storage.database import Database
from ..storage.vector_store import NumpyVectorStore

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..engines.embedding import BaseEmbedding
    from ..storage.vector_store import BaseVectorStore

_logger = get_logger("core.context")


def _mark(degraded: list[str], name: str) -> None:
    """向降级清单追加组件名（去重）。"""

    if name not in degraded:
        degraded.append(name)


@dataclass
class AppContext:
    """应用级依赖容器。

    装配顺序（由 :meth:`build` 保证）：先构建存储与引擎组件实例，再填充本容器
    字段，随后交给编排器 :class:`~safefusion.core.orchestrator.AuditOrchestrator`
    使用；组件缺失时为 None 表示降级 / 未启用，``degraded`` 汇总降级组件名。
    """

    config: "AppConfig | None" = None
    database: "Database | None" = None
    store: "BaseVectorStore | None" = None
    embedding: "BaseEmbedding | None" = None
    keyword_engine: "KeywordEngine | None" = None
    light_model: "LightTextModel | None" = None
    whitelist: "WhitelistMatcher | None" = None
    semantic: "SemanticEngine | None" = None
    llm: "LLMClient | None" = None
    cache_layer: "CacheLayer | None" = None
    degraded: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, config: "AppConfig") -> "AppContext":
        """装配全部组件并返回完整 AppContext（组件级降级，全程不抛）。

        Args:
            config: 已加载的应用总配置。

        Returns:
            装配完成的 AppContext；``degraded`` 字段列出所有未成功装配的组件名
            （database / cache_layer / keyword_engine / light_model / whitelist /
            store / embedding / semantic / llm 中取子集）。
        """

        data_dir = Path(config.data_dir)
        degraded: list[str] = []

        # ① 存储：SQLite DAO（audit.db，父目录自动创建）
        database: Database | None = None
        try:
            database = Database(data_dir / "audit.db")
        except Exception as exc:
            _logger.warning("Database 装配失败（降级为 None）: %s", exc)
            _mark(degraded, "database")

        # ② 缓存层（config.cache 的 dict 形态；CacheLayer 自带键名别名兼容）
        cache_layer: CacheLayer | None = None
        try:
            cache_layer = CacheLayer(config.cache.model_dump())
        except Exception as exc:
            _logger.warning("CacheLayer 装配失败（降级为 None）: %s", exc)
            _mark(degraded, "cache_layer")

        # ③ 关键词引擎 + 词库加载（空词库正常：自动机可空构建，scan 返回空列表）
        keyword_engine: KeywordEngine | None = None
        try:
            keyword_engine = KeywordEngine()
            if database is not None:
                rows = database.list_keywords()
                categories: dict[str, list[str]] = {}
                for row in rows:
                    categories.setdefault(row["category"], []).append(row["word"])
                # 正则消歧规则（PRD v0.2 M4）：开关开启时从 rules 表加载
                # （可为空表），关闭时规则层整体跳过（disambiguate 透传）
                rules: list[dict] | None = None
                if config.keyword.regex_rules_enabled:
                    rules = database.list_rules(active_only=True)
                try:
                    keyword_engine.reload(categories, rules)
                except ValueError as exc:
                    # 规则表存在引擎无法编译的条目：词库照常加载，规则层降级关闭
                    _logger.warning("正则规则加载失败（本次启动规则层关闭，词库正常）: %s", exc)
                    keyword_engine.reload(categories, None)
        except Exception as exc:
            _logger.warning("KeywordEngine 装配失败（降级为 None）: %s", exc)
            keyword_engine = None
            _mark(degraded, "keyword_engine")

        # ④ 轻量文本风险模型（路径为 None / 缺文件 / 缺 torch → disabled）
        light_model: LightTextModel | None = None
        try:
            light_model = LightTextModel(
                config.light_model.model_path, config.light_model.config_path
            )
        except Exception as exc:
            _logger.warning("LightTextModel 装配失败（降级为 None）: %s", exc)
            light_model = None
        if light_model is None or light_model.disabled:
            _mark(degraded, "light_model")

        # ⑤ 图片白名单匹配器（DB 即持久化）
        whitelist: WhitelistMatcher | None = None
        if database is not None:
            whitelist = WhitelistMatcher(database)
        else:
            _mark(degraded, "whitelist")

        # ⑥ Embedding 双后端：local 缺 torch / cloud 缺 Key 抛 RuntimeError → None
        embedding: BaseEmbedding | None = None
        try:
            embedding = get_embedding_backend(config.embedding)
        except Exception as exc:
            _logger.warning("Embedding 后端不可用（降级为 None）: %s", exc)
            _mark(degraded, "embedding")

        # ⑦ 自研向量库：有持久化文件则 load，否则空库（save 确保目录存在）
        store: BaseVectorStore | None = None
        try:
            vectors_dir = data_dir / "vectors"
            if (vectors_dir / "black.npz").is_file() or (vectors_dir / "white.npz").is_file():
                store = NumpyVectorStore.load(str(vectors_dir))
            else:
                store = NumpyVectorStore(str(vectors_dir))
                store.save()  # 空库也确保 save 目录存在
        except Exception as exc:
            _logger.warning("NumpyVectorStore 装配失败（降级为 None）: %s", exc)
            _mark(degraded, "store")

        # ⑧ 语义引擎：仅当 embedding 与 store 均可用时装配
        semantic: SemanticEngine | None = None
        if embedding is not None and store is not None:
            try:
                # thresholds 合并 config.semantic（rerank 四信号权重等 v0.2 键），
                # 保证 Rerank 开关经配置即生效（T19 交接接线，主模型 2026-08-26）。
                sem_th = {**config.thresholds.model_dump(), **config.semantic.model_dump()}
                semantic = SemanticEngine(embedding, store, thresholds=sem_th)
            except Exception as exc:
                _logger.warning("SemanticEngine 装配失败（降级为 None）: %s", exc)
                semantic = None
        else:
            _logger.warning(
                "语义引擎未装配：embedding=%s store=%s（降级为 None）",
                embedding is not None,
                store is not None,
            )
        if semantic is None:
            _mark(degraded, "semantic")

        # ⑨ LLM 兜底客户端（密钥仅环境变量，缺失 → available=False）
        llm: LLMClient | None = None
        try:
            llm = LLMClient(config.llm.model_dump(exclude={"api_key"}))
        except Exception as exc:
            _logger.warning("LLMClient 装配失败（降级为 None）: %s", exc)
            llm = None
        if llm is None or not llm.available:
            _mark(degraded, "llm")

        return cls(
            config=config,
            database=database,
            store=store,
            embedding=embedding,
            keyword_engine=keyword_engine,
            light_model=light_model,
            whitelist=whitelist,
            semantic=semantic,
            llm=llm,
            cache_layer=cache_layer,
            degraded=degraded,
        )

    # ------------------------------------------- 热重载（PRD v0.2 M4，免重启）

    def reload_keywords(self) -> bool:
        """从数据库重新加载词库 + 正则规则到 KeywordEngine 并原子替换（热重载）。

        供管理端写入词库 / 规则后调用，免重启即时生效；规则层是否参与由
        ``config.keyword.regex_rules_enabled`` 决定。词库 / 规则在锁外构建、
        锁内一次性替换（``KeywordEngine.reload`` 原子语义）：重载失败时旧实例
        继续生效，本方法不抛异常。

        Returns:
            True = 已成功原子替换；False = 引擎未装配 / 数据库不可用 / 重载
            失败（旧实例继续生效）。
        """

        if self.keyword_engine is None or self.database is None:
            return False
        try:
            categories: dict[str, list[str]] = {}
            for row in self.database.list_keywords():
                categories.setdefault(row["category"], []).append(row["word"])
            rules = self._load_rules_rows()
            self.keyword_engine.reload(categories, rules)
        except Exception as exc:
            _logger.warning("关键词热重载失败（保留旧实例）: %s", exc)
            return False
        return True

    def reload_rules(self) -> bool:
        """仅重载正则消歧规则（词库一并从数据库刷新，复用 :meth:`reload_keywords`）。

        Returns:
            同 :meth:`reload_keywords`。
        """

        return self.reload_keywords()

    def _load_rules_rows(self) -> list[dict] | None:
        """按配置开关读取 rules 表活性规则行。

        Returns:
            规则行列表；``regex_rules_enabled=False`` 或数据库不可用时返回
            None（规则层关闭）。
        """

        if self.config is None or not self.config.keyword.regex_rules_enabled:
            return None
        if self.database is None:
            return None
        return self.database.list_rules(active_only=True)
