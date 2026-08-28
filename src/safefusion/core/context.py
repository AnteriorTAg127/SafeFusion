"""应用上下文聚合壳：集中持有配置与全部组件实例，并提供装配入口。

``AppContext.build(config)`` 按依赖顺序装配全部组件（T9 集成核心）：

- 存储：SQLite ``Database``（``data_dir/audit.db``，目录自动创建；v0.3.0 起
  支持注入已建实例以复用启动迁移用连接）+ 自研 ``NumpyVectorStore``
  （``data_dir/vectors`` 下有 ``black.npz`` / ``white.npz`` 则 load，否则
  构造空库并确保 save 目录存在）；
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

**懒加载（PRD v0.3.0 M6 D1）**：:meth:`build` **不再启动即装载** embedding——
只保存建造参数（``_embedding_spec``），语义引擎以 lazy 占位（degraded 原因码
``lazy_pending``）。首次审核请求真正需要语义层时经 :meth:`ensure_semantic` /
:meth:`ensure_semantic_async` 触发**单飞装配**（线程 + 事件，一次只装一次；
装配在后台线程执行，完成前请求保持降级、不阻塞事件循环；local 后端在
``local_files_only=True`` 下只装载本地缓存，绝不因审核路径意外联网下载）；
装配失败 → 保持 degraded（原因码 ``embedding_error`` 及细分
``embedding_assets_missing`` / ``embedding_credential_error`` 等），不自动
重试；:meth:`load_semantic`（``POST /admin/models/load``）为显式装配入口，
允许既往失败后重试并同步等待结果。

热应用（PRD v0.3.0 M4 C，由 ``core.hot_apply`` 编排调用）：
- :meth:`swap_components` 在进程内锁（``_sync_lock``）内**原子替换**多个组件
  引用并就地同步有效配置叶子（保持 ``config`` 对象身份不变，编排器持有的
  引用即时可见），替换失败由调用方按旧实例快照回滚；embedding 被替换时
  同步刷新懒装配参数并把在飞装配作废（代际计数，避免旧结果覆盖新配置）；
- :meth:`reload_semantic_thresholds` 按当前有效配置重建 ``SemanticEngine``
  阈值字典（单字典赋值 = 原子替换，阈值/语义权重热生效）；
- 组件重建类（embedding/llm/light_model/cache）先经 ``hot_apply.stage_apply``
  在锁外**试建造**（失败则不落库、旧实例继续生效），再锁内替换。
"""

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..cache.caches import CacheLayer
from ..engines.embedding import get_embedding_backend
from ..engines.image_pipeline import WhitelistMatcher
from ..engines.keyword_engine import KeywordEngine
from ..engines.light_model import LightTextModel
from ..engines.llm_client import LLMClient
from ..engines.model_repo import resolve_hf_cache_dir
from ..engines.semantic import SemanticEngine
from ..logging_setup import get_logger
from ..storage.database import Database
from ..storage.vector_store import NumpyVectorStore

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..engines.embedding import BaseEmbedding
    from ..storage.vector_store import BaseVectorStore

_logger = get_logger("core.context")

#: 审核路径懒装配的等待上限（秒）：首个请求等待缓存命中装载（秒级），
#: 超时返回 degraded 不阻塞请求；失败是快速失败（local_files_only），
#: 实际等待远超上限的场景罕见（仅权重已缓存但装载慢）。
LAZY_ASSEMBLY_WAIT_SECONDS = 10.0

#: 语义层懒装配降级原因码（PRD v0.3.0 M6 新增 lazy_pending，编排层可见）
REASON_LAZY_PENDING = "lazy_pending"
REASON_EMBEDDING_ERROR = "embedding_error"
REASON_EMBEDDING_ASSETS_MISSING = "embedding_assets_missing"
REASON_EMBEDDING_CREDENTIAL_ERROR = "embedding_credential_error"
REASON_EMBEDDING_CONFIG_ERROR = "embedding_config_error"
REASON_SEMANTIC_ENGINE_ERROR = "semantic_engine_error"
REASON_UNCONFIGURED = "embedding_unconfigured"


def _mark(degraded: list[str], name: str) -> None:
    """向降级清单追加组件名（去重）。"""

    if name not in degraded:
        degraded.append(name)


def _set_config_leaves(target: "AppConfig", candidate: "AppConfig") -> None:
    """把 ``candidate`` 的配置叶子就地写进 ``target``（保持对象身份不变）。

    只遍历 ``AppConfig`` 中注解为 pydantic 子模型的分组（``data_dir`` 等
    标量字段不参与热应用）；逐叶子 ``setattr``（pydantic ``validate_assignment``
    会做类型校验，非法类型抛错由调用方回滚）。编排器 / 复核调度器持有的
    分组模型引用（如 ``config.review``）与 ``target`` 为同一对象，改动即时可见。
    """

    for name, field_ in type(candidate).model_fields.items():
        ann = field_.annotation
        if ann is None or not (isinstance(ann, type) and issubclass(ann, BaseModel)):
            continue
        target_group = getattr(target, name)
        candidate_group = getattr(candidate, name)
        _sync_group_leaves(target_group, candidate_group)


def _sync_group_leaves(target: BaseModel, candidate: BaseModel, prefix: str = "") -> None:
    """递归把候选分组的叶子同步进目标分组（嵌套子模型逐层下钻）。"""

    for name, field_ in type(candidate).model_fields.items():
        ann = field_.annotation
        if ann is not None and isinstance(ann, type) and issubclass(ann, BaseModel):
            _sync_group_leaves(getattr(target, name), getattr(candidate, name), f"{prefix}{name}.")
        else:
            path = f"{prefix}{name}"
            try:
                setattr(target, name, getattr(candidate, name))
            except Exception as exc:
                _logger.warning("配置热应用叶子设置失败 %s: %s（回滚由调用方处理）", path, exc)
                raise


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
    #: 组件热应用串行化锁（``swap_components`` / degraded 刷新在锁内完成）
    _sync_lock: RLock = field(default_factory=RLock, repr=False, init=False)
    #: embedding 懒装配参数（build 保存的 ``config.embedding.model_dump()``；
    #: None 表示未配置 / 非懒管理容器）
    _embedding_spec: dict[str, Any] | None = field(default=None, repr=False, init=False)
    #: 懒装配状态（单飞：同刻只一个后台装配线程；线程安全）
    _assembly_lock: RLock = field(default_factory=RLock, repr=False, init=False)
    _assembly_event: "threading.Event | None" = field(default=None, repr=False, init=False)
    _assembly_attempted: bool = field(default=False, repr=False, init=False)
    _assembly_error: str | None = field(default=None, repr=False, init=False)
    _assembly_generation: int = field(default=0, repr=False, init=False)
    _last_assembly: dict[str, Any] | None = field(default=None, repr=False, init=False)

    @classmethod
    def build(cls, config: "AppConfig", database: "Database | None" = None) -> "AppContext":
        """装配全部组件并返回完整 AppContext（组件级降级，全程不抛）。

        Args:
            config: 已加载的应用总配置。
            database: 可选已建 ``Database`` 实例（v0.3.0 启动迁移复用连接）；
                None 时按 ``data_dir/audit.db`` 自行创建。

        Returns:
            装配完成的 AppContext；``degraded`` 字段列出所有未成功装配的组件名
            （database / cache_layer / keyword_engine / light_model / whitelist /
            store / embedding / semantic / llm 中取子集）。
        """

        data_dir = Path(config.data_dir)
        degraded: list[str] = []

        # ① 存储：SQLite DAO（audit.db，父目录自动创建；可注入已建实例）
        database: Database | None = database
        if database is None:
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

        # ⑥ Embedding 懒加载（PRD v0.3.0 M6 D1）：build **不实例化**模型
        # （不触网 / 不装载 / 不下载），仅保存建造参数；语义引擎以 lazy
        # 占位（degraded 原因码 lazy_pending = embedding + semantic 待单飞装配）。
        # 首次审核请求经 ensure_semantic / 管理端 /admin/models/load 触发装配。
        embedding: BaseEmbedding | None = None
        embedding_spec: dict[str, Any] | None = config.embedding.model_dump()
        _mark(degraded, "embedding")
        if store is None:
            _logger.warning("向量库不可用（store=None）：语义引擎将无法装配（即使 embedding 就绪）")

        # ⑧ 语义引擎：懒占位（永不在此装配；见 ensure_semantic / load_semantic）
        semantic: SemanticEngine | None = None
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

        ctx = cls(
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
        ctx._embedding_spec = embedding_spec
        return ctx

    # ------------------------------------------- 懒装配（PRD v0.3.0 M6 D1）

    def semantic_pending(self) -> bool:
        """语义层是否处于**可自动装配的等待态**（已保存建造参数、未装配、未尝试）。

        既往装配失败（``_assembly_attempted``）后返回 False——自动路径不再
        重试（避免每次审核都触发无谓尝试），显式重试走 :meth:`load_semantic`。
        """

        return (
            self._embedding_spec is not None
            and self.semantic is None
            and not self._assembly_attempted
        )

    def semantic_degraded_reason(self) -> str | None:
        """语义层降级原因码（编排层 / /admin/health 展示）。

        - ``semantic`` 就绪 → None；
        - 非懒管理容器（无建造参数，如编排器测试桩）→ None（调用方沿用
          ``semantic_disabled`` 兼容口径）；
        - 待首次装配（含装配进行中）→ ``lazy_pending``；
        - 已尝试但失败 → 具体错误码（``embedding_error`` /
          ``embedding_assets_missing`` / ``embedding_credential_error`` /
          ``embedding_config_error`` / ``semantic_engine_error``）。
        """

        if self.semantic is not None:
            return None
        if self._embedding_spec is None:
            return None
        if self._assembly_attempted or self._assembly_error:
            return self._assembly_error or REASON_EMBEDDING_ERROR
        return REASON_LAZY_PENDING

    def _ensure_started(self) -> "threading.Event | None":
        """单飞装配启动器：确保至多一个后台装配线程在飞，返回对应事件。

        仅当语义层处于可自动装配等待态时启动；并发调用共享同一事件。
        """

        with self._assembly_lock:
            if self.semantic is not None or self._embedding_spec is None:
                return None
            if self._assembly_attempted:
                return None  # 既往失败不自动重试（需 load_semantic 显式触发）
            event = self._assembly_event
            if event is None:
                event = threading.Event()
                self._assembly_event = event
                threading.Thread(
                    target=self._assembly_worker,
                    args=(event, self._assembly_generation),
                    daemon=True,
                    name="safefusion-semantic-assembly",
                ).start()
            return event

    def ensure_semantic(self, timeout: float = 0.0) -> "SemanticEngine | None":
        """懒装配（同步版，供同步调用方 / 可阻塞场景）：单飞触发装配。

        - ``timeout=0``（默认）：立即返回当前语义引擎（首次请求先于装配返回，
          装配在后台继续，后续请求拿已装配实例）；适合事件循环线程（不阻塞）；
        - ``timeout>0``：阻塞等待至多 ``timeout`` 秒（线程安全 ``Event.wait``），
          装配完成即返回实例，超时 / 失败返回 None（保持 degraded）。

        Returns:
            装配后的 ``SemanticEngine``；未就绪 / 装配失败返回 None（不抛异常）。
        """

        event = self._ensure_started()
        if event is not None and timeout > 0:
            event.wait(timeout)
        return self.semantic

    async def ensure_semantic_async(
        self, timeout: float = LAZY_ASSEMBLY_WAIT_SECONDS
    ) -> "SemanticEngine | None":
        """懒装配（异步版，审核路径使用）：单飞触发装配并在事件循环内非阻塞等待。

        首个语义请求在缓存命中装载期间（秒级）可拿到已装配引擎；装配超时 /
        失败时快速返回 None（装配事件 set 即退出轮询，不空转），请求保持降级
        不阻塞。

        Args:
            timeout: 等待上限（秒）；<=0 表示不等待（纯触发）。

        Returns:
            装配后的 ``SemanticEngine``；未就绪返回 None。
        """

        event = self._ensure_started()
        if event is not None and timeout > 0:
            deadline = monotonic() + timeout
            while not event.is_set() and monotonic() < deadline:
                await asyncio.sleep(0.03)
        return self.semantic

    def load_semantic(self, timeout: float | None = 300.0) -> dict[str, Any]:
        """显式装配（``POST /admin/models/load``）：同步等待并返回结果摘要。

        与 :meth:`ensure_semantic` 的区别：
        - 允许既往失败后**重试**（重置 attempted 标记，另起一个新装配线程）；
        - 进行中装配直接等待（同一单飞）；
        - 返回结构化结果（status / reason / message / duration_s），供管理端展示。

        Args:
            timeout: 等待上限（秒）；None 表示无限等待。调用方应在线程池执行
                （管理端经 ``run_in_threadpool``），避免阻塞事件循环。

        Returns:
            ``{"status": "ok"|"failed"|"timeout"|"stale", "reason", "message",
            "duration_s", "semantic_ready"}``。
        """

        with self._assembly_lock:
            if self.semantic is not None:
                return self._assembly_summary("ok", None, "语义层已就绪")
            if self._embedding_spec is None:
                return self._assembly_summary(
                    "failed", REASON_UNCONFIGURED, "未配置 embedding 后端（config.embedding）"
                )
            event = self._assembly_event
            if event is None:
                self._assembly_attempted = False  # 允许重试
                event = threading.Event()
                self._assembly_event = event
                threading.Thread(
                    target=self._assembly_worker,
                    args=(event, self._assembly_generation),
                    daemon=True,
                    name="safefusion-semantic-assembly",
                ).start()
        if event is not None:
            event.wait(None if timeout is None else max(0.0, timeout))
        with self._assembly_lock:
            last = self._last_assembly
            if last is not None and last.get("status") == "ok":
                return self._assembly_summary(
                    "ok", None, "语义层装配成功", duration_s=last.get("duration_s")
                )
            if last is not None:
                return self._assembly_summary(
                    last.get("status", "failed"),
                    last.get("reason"),
                    last.get("message") or "装配失败",
                    duration_s=last.get("duration_s"),
                )
            return self._assembly_summary("timeout", None, "装配等待超时（后台仍在进行？）")

    def embedding_status(self) -> dict[str, Any]:
        """Embedding / 语义层装配状态（``GET /admin/models`` 契约）。"""

        spec = self._embedding_spec or {}
        backend = str(spec.get("backend") or "local")
        ready = self.embedding is not None
        if self._embedding_spec is None:
            status, reason = "not_configured", None
        elif ready:
            status, reason = "ready", None
        elif self._assembly_event is not None:
            status, reason = "loading", REASON_LAZY_PENDING
        elif self._assembly_attempted or self._assembly_error:
            status, reason = "error", (self._assembly_error or REASON_EMBEDDING_ERROR)
        else:
            status, reason = "pending", REASON_LAZY_PENDING
        return {
            "backend": backend,
            "ready": ready,
            "status": status,
            "reason": reason,
            "semantic_ready": self.semantic is not None,
        }

    def _assembly_summary(
        self,
        status: str,
        reason: str | None,
        message: str,
        **extra: Any,
    ) -> dict[str, Any]:
        """组装装配结果摘要（不携带任何内部状态对象）。"""

        return {
            "status": status,
            "reason": reason,
            "message": message,
            "semantic_ready": self.semantic is not None,
            "duration_s": extra.get("duration_s"),
        }

    def _assembly_worker(self, event: "threading.Event", generation: int) -> None:
        """后台装配线程执行体：装配 → 记录结果 → 通知等待者。"""

        started = monotonic()
        result = self._assemble_once(generation)
        result["duration_s"] = round(monotonic() - started, 3)
        with self._assembly_lock:
            self._last_assembly = result
            self._assembly_event = None
        event.set()

    def _assemble_once(self, generation: int) -> dict[str, Any]:
        """执行一次 embedding + SemanticEngine 装配（线程内调用，不抛异常）。

        - local 后端以 ``local_files_only=True`` 装载（只读缓存，绝不因装配
          联网下载；权重缺失快速失败 → ``embedding_assets_missing``）；
        - 装配成功：锁内原子替换 ``embedding`` / ``semantic`` 并刷新 degraded，
          旧实例尽力关闭；失败：保持 degraded（原因码细分），不中断调用方。
        """

        spec = self._embedding_spec
        if not spec:
            reason = REASON_UNCONFIGURED
            return {"status": "failed", "reason": reason, "message": "未配置 embedding 后端"}
        data_dir = self.config.data_dir if self.config is not None else "./data"
        cache_dir = resolve_hf_cache_dir(data_dir)
        try:
            backend = get_embedding_backend(
                spec,
                cache_dir=str(cache_dir),
                local_files_only=True,  # 装配不联网（显式下载走 /admin/models/download）
            )
        except Exception as exc:
            code, message = self._classify_embedding_error(exc, cache_dir)
            with self._assembly_lock:
                self._assembly_error = code
                self._assembly_attempted = True
            return {"status": "failed", "reason": code, "message": message}

        store = self.store
        semantic: SemanticEngine | None = None
        if store is not None:
            try:
                thresholds = self._current_semantic_thresholds()
                semantic = SemanticEngine(backend, store, thresholds=thresholds)
            except Exception as exc:
                _logger.warning(
                    "SemanticEngine 装配失败（embedding 已就绪但语义层保持降级）: %s", exc
                )
                semantic = None
        with self._sync_lock:
            if generation != self._assembly_generation:
                # 在飞装配期间 embedding 配置被热应用替换：丢弃本次结果
                _best_effort_close(backend, "embedding")
                stale = "装配期间配置已变更，结果丢弃"
                return {"status": "stale", "reason": None, "message": stale}
            old_embedding = self.embedding
            self.embedding = backend
            self.semantic = semantic
            self._assembly_error = None if semantic is not None else REASON_SEMANTIC_ENGINE_ERROR
            self._assembly_attempted = True
            if semantic is not None:
                self.degraded = [d for d in self.degraded if d not in ("embedding", "semantic")]
            else:
                self.degraded = [d for d in self.degraded if d != "embedding"]
                if "semantic" not in self.degraded:
                    self.degraded.append("semantic")
            _best_effort_close(old_embedding, "embedding")
        if semantic is not None:
            _logger.info(
                "语义层懒装配成功（backend=%s, embedding=%s）",
                spec.get("backend"),
                type(backend).__name__,
            )
            return {"status": "ok", "reason": None, "message": "语义层装配成功"}
        return {
            "status": "failed",
            "reason": REASON_SEMANTIC_ENGINE_ERROR,
            "message": "embedding 已就绪但语义引擎不可用（向量库缺失或构造失败）",
        }

    @staticmethod
    def _classify_embedding_error(exc: Exception, cache_dir: Path) -> tuple[str, str]:
        """把 embedding 装配异常归类为原因码 + 用户可读消息（脱敏截断）。"""

        message = (str(exc).strip() or type(exc).__name__)[:200]
        lowered = message.lower()
        exc_name = type(exc).__name__.lower()
        if "local_entry_not_found" in exc_name or "oserror" in exc_name:
            code = REASON_EMBEDDING_ASSETS_MISSING
            message = (
                f"本地 Chinese-CLIP 权重未缓存（{cache_dir}）；请先经 "
                "POST /admin/models/download 下载，或配置 "
                "embedding.local.weights_path 指向本地权重目录"
            )
        elif "api key" in lowered or "密钥" in message:
            code = REASON_EMBEDDING_CREDENTIAL_ERROR
        elif "base_url" in lowered or "model" in lowered and "不存在" not in message:
            code = REASON_EMBEDDING_CONFIG_ERROR
        else:
            code = REASON_EMBEDDING_ERROR
        return code, message

    def _current_semantic_thresholds(self) -> dict[str, Any]:
        """按当前有效配置合并语义引擎阈值字典（与 reload_semantic_thresholds 同口径）。"""

        merged = dict(SemanticEngine._DEFAULT_THRESHOLDS)
        if self.config is not None:
            merged.update(self.config.thresholds.model_dump())
            merged.update(self.config.semantic.model_dump())
            weights = dict(SemanticEngine._DEFAULT_THRESHOLDS["weights"])
            if isinstance(merged.get("weights"), dict):
                weights.update(merged["weights"])
            merged["weights"] = weights
        return merged

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

    # ------------------------------------------- 热应用（PRD v0.3.0 M4 C）

    def swap_components(
        self,
        replacements: dict[str, object] | None = None,
        candidate: "AppConfig | None" = None,
    ) -> list[str]:
        """锁内原子替换组件引用并（可选）就地同步有效配置叶子。

        供 ``core.hot_apply`` 在「试建造成功 → 落库 → 应用」流程的最后一步
        调用；``replacements`` 中的实例须已由调用方预建造完成（锁外建造，
        锁内只做引用赋值，持锁时间极短）。被替换字段的旧实例若持有可关闭
        资源（云端 embedding 的 httpx 客户端等）则尽力关闭，失败仅告警。

        Args:
            replacements: ``{组件字段名: 新实例}`` 映射；可为 None / 空。
            candidate: 新有效配置；非 None 时先就地同步 ``self.config`` 叶子
                （对象身份不变，编排器引用即时可见），再替换组件。

        Returns:
            实际替换的组件字段名列表（config 叶子同步不计入）。

        Raises:
            Exception: setattr 失败（如配置叶子类型非法）——由调用方按旧实例
                快照回滚。
        """

        with self._sync_lock:
            if candidate is not None and self.config is not None:
                _set_config_leaves(self.config, candidate)
            replaced: list[str] = []
            for name, instance in (replacements or {}).items():
                old = getattr(self, name, None)
                setattr(self, name, instance)
                replaced.append(name)
                _best_effort_close(old, name)
            # v0.3.0 M6 懒装配一致性：embedding 被替换（热应用）时刷新建造参数，
            # 作废在飞装配（代际计数）并重置失败标记（新配置可再懒装配）
            if "embedding" in replaced:
                self._assembly_generation += 1
                self._assembly_event = None
                self._assembly_attempted = False
                self._assembly_error = None
                if self.config is not None:
                    self._embedding_spec = self.config.embedding.model_dump()
            self._refresh_degraded(replaced)
            return replaced

    def reload_semantic_thresholds(self) -> None:
        """按当前有效配置重建 ``SemanticEngine`` 阈值字典（单字典赋值原子替换）。

        阈值 / 语义权重（Rerank 四信号权重、fuse_mode）热生效；语义引擎未
        装配（None）时为空操作。合并口径与 :meth:`build` 一致：
        ``默认阈值 < config.thresholds < config.semantic``。
        """

        sem = self.semantic
        if sem is None or self.config is None:
            return
        merged = dict(SemanticEngine._DEFAULT_THRESHOLDS)
        merged.update(self.config.thresholds.model_dump())
        merged.update(self.config.semantic.model_dump())
        weights = dict(SemanticEngine._DEFAULT_THRESHOLDS["weights"])
        if isinstance(merged.get("weights"), dict):
            weights.update(merged["weights"])
        merged["weights"] = weights
        sem.thresholds = merged  # 单字典赋值 = 原子替换（GIL 下读者见完整快照）

    def _refresh_degraded(self, names: list[str]) -> None:
        """按被替换组件刷新 ``degraded`` 清单（只处理本次涉及的组件名）。"""

        for name in names:
            instance = getattr(self, name, None)
            if name == "light_model":
                ok = instance is not None and not instance.disabled
            elif name == "llm":
                ok = instance is not None and instance.available
            else:
                ok = instance is not None
            if ok:
                self.degraded = [d for d in self.degraded if d != name]
            elif name not in self.degraded:
                self.degraded.append(name)


def _best_effort_close(instance: object, name: str) -> None:
    """尽力关闭旧组件持有的资源（云端 embedding httpx 等）；失败仅告警。"""

    if instance is None:
        return
    closer = getattr(instance, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception as exc:
            _logger.warning("旧组件 %s 资源关闭失败（忽略）: %s", name, exc)
