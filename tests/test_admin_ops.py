"""管理端新能力测试（PRD v0.3.0 M2/M3/M5/M6 + C5，T30B 任务卡）。

覆盖（完成标准 1）：
- **懒加载不触网**：``AppContext.build`` 不实例化 embedding / 无
  from_pretrained 调用（monkeypatch 工厂 spy）；语义层 lazy 占位
  （degraded 原因码 lazy_pending）且 models/health 端点不触发装配；
- **ensure_semantic 单飞**：并发触发只装配一次；失败保持 degraded 不自动
  重试；``load_semantic`` 允许显式重试并同步返回结果；
- **/admin/models**：chinese-clip 状态（未下载 / 已就绪 / cloud / weights_path）
  + HF 缓存 blobs 数与大小 + fasttext 状态 + 向量库黑白条数/维度 + 语义状态；
- **下载任务生命周期**（假下载器）：POST 202 → 轮询 running→completed /
  failed；同模型并发互斥（复用进行中任务）；404 未知任务；
- **/admin/health**：组件就绪清单 + 降级原因码 + 数据概况 + 缓存统计 +
  degraded 与 :8000 同口径；
- **test-examples**：临时黑白 CSV 抽 20 条去重 ≤200 字符带 pool 标注；缺失
  文件返回空列表；
- **test-audit**：管理令牌鉴权 + 完整 detail（full 语义，关键词命中可见）；
- **test-connection**：embedding cloud 无 Key/base_url、local 未缓存、
  llm 无 Key、fasttext 未配置——全部可读错误、不崩；
- **改密**：current 校验（hmac）、长度 ≥10、旧令牌立即 401、新令牌 200、
  settings 表 admin.token 持久化（重启后按 DB 生效）。

环境约定：懒装配经 ``monkeypatch`` ``safefusion.core.context.get_embedding_backend``
注入假后端；下载经 ``monkeypatch`` ``safefusion.engines.model_repo.download_clip_weights``
注入假下载器；不依赖真实 ML / 网络。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from safefusion.api.admin import create_admin_app
from safefusion.core import context as context_mod
from safefusion.core.context import AppContext
from safefusion.engines import model_repo
from safefusion.engines.image_pipeline import WhitelistMatcher
from safefusion.storage.database import Database
from safefusion.storage.vector_store import VectorItem

from .conftest import build_config
from .fakes import FakeEmbedding

TOKEN = "sf-admin-ops-token"
_MODEL_ID = "OFA-Sys/chinese-clip-vit-base-patch16"


def _headers(token: str | None = TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token} if token is not None else {}


class _DuckCfg:
    """带 data_dir 与可选 admin_token 的配置鸭子类型（管理令牌确定化 / DB 回退测试）。"""

    def __init__(self, tmp_path: Path, token: str | None = TOKEN) -> None:
        self.data_dir = str(tmp_path)
        self.admin_token = token


def _build_context(tmp_path: Path, **overrides: Any) -> tuple[Database, AppContext]:
    """真实降级装配 + 注入共享容器（embedding 默认 local → 懒加载待装配）。"""

    db = Database(tmp_path / "audit.db")
    cfg = build_config(tmp_path, **overrides)
    ctx = AppContext.build(cfg, database=db)
    return db, ctx


def _make_app(
    tmp_path: Path,
    container: AppContext | None = None,
    db: Database | None = None,
    token: str | None = TOKEN,
) -> TestClient:
    db = db or Database(tmp_path / "audit.db")
    app = create_admin_app(
        db,
        WhitelistMatcher(db),
        config=_DuckCfg(tmp_path, token),
        container=container,
    )
    return TestClient(app)


@pytest.fixture
def admin_client(tmp_path: Path) -> TestClient:
    db, ctx = _build_context(tmp_path)
    client = _make_app(tmp_path, container=ctx, db=db)
    yield client
    client.close()
    db.close()


def _seed_store(ctx: AppContext, pool: str, rows: int = 3, dim: int = 4) -> None:
    """向容器向量库注入 (category, text) 条目（真实 NumpyVectorStore）。"""

    assert ctx.store is not None
    ctx.store.add(
        [
            VectorItem(
                id=f"{pool}-{i}",
                pool=pool,
                vector=np.ones(dim, dtype="float32") * (i + 1),
                metadata={"category": "测试", "text": f"{pool} 样本 {i}"},
            )
            for i in range(rows)
        ]
    )
    ctx.store.save()


# ------------------------------------------------------------------ 懒加载


class TestLazyAssembly:
    """懒加载：build 不触网不实例化；单飞装配一次；失败保持 degraded。"""

    def test_build_does_not_instantiate_embedding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        original = context_mod.get_embedding_backend

        def _spy_factory(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            raise AssertionError("build 阶段不应调用 embedding 工厂（懒加载）")

        monkeypatch.setattr(context_mod, "get_embedding_backend", _spy_factory)
        db, ctx = _build_context(tmp_path)
        assert calls == []  # build 未实例化 / 未 from_pretrained
        assert ctx.embedding is None
        assert ctx.semantic is None
        assert "embedding" in ctx.degraded
        assert "semantic" in ctx.degraded
        assert ctx.semantic_degraded_reason() == "lazy_pending"
        # 恢复原工厂供后续测试使用（本用例仅验证 build 行为）
        monkeypatch.setattr(context_mod, "get_embedding_backend", original)
        db.close()

    def test_ensure_semantic_assembles_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, ctx = _build_context(tmp_path)
        fake = FakeEmbedding(texts={"x": np.ones(4, dtype="float32")})
        calls: list[Any] = []

        def _factory(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return fake

        monkeypatch.setattr(context_mod, "get_embedding_backend", _factory)
        # 并发单飞：两个线程同时触发 → 只装配一次
        results: list[Any] = []

        def _call() -> None:
            results.append(ctx.ensure_semantic(timeout=10.0))

        t1 = threading.Thread(target=_call)
        t2 = threading.Thread(target=_call)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert len(calls) == 1  # 单飞：同刻只装一次
        assert ctx.semantic is not None
        assert ctx.semantic.embedding is fake  # 语义引擎绑定新 embedding + 复用 store
        assert ctx.semantic.store is ctx.store
        assert "embedding" not in ctx.degraded
        assert "semantic" not in ctx.degraded
        assert ctx.semantic_degraded_reason() is None
        # 再次触发不再装配
        assert ctx.ensure_semantic(timeout=1.0) is ctx.semantic
        assert len(calls) == 1
        db.close()

    def test_assembly_failure_keeps_degraded_no_auto_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, ctx = _build_context(tmp_path)
        calls: list[Any] = []

        def _boom_factory(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            raise RuntimeError("fake backend 构造失败（模拟不可用后端）")

        monkeypatch.setattr(context_mod, "get_embedding_backend", _boom_factory)
        result = ctx.load_semantic(timeout=10.0)
        assert result["status"] == "failed"
        assert result["reason"] == "embedding_error"
        assert "fake backend" in result["message"]
        assert ctx.semantic is None
        # 保持 degraded 且不自动重试（一次只装一次；第二次 ensure 不触碰工厂）
        assert "embedding" in ctx.degraded
        assert "semantic" in ctx.degraded
        assert ctx.semantic_degraded_reason() == "embedding_error"
        assert ctx.ensure_semantic(timeout=1.0) is None
        assert len(calls) == 1
        # 显式重试成功（/admin/models/load 语义）
        fake = FakeEmbedding(texts={"x": np.ones(4, dtype="float32")})
        monkeypatch.setattr(context_mod, "get_embedding_backend", lambda *a, **k: fake)
        retry = ctx.load_semantic(timeout=10.0)
        assert retry["status"] == "ok"
        assert ctx.semantic is not None
        assert "embedding" not in ctx.degraded
        db.close()

    def test_assets_missing_reason_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, ctx = _build_context(tmp_path)

        def _missing(*args: Any, **kwargs: Any) -> Any:
            raise OSError("Cannot find the requested files in the cached path")

        monkeypatch.setattr(context_mod, "get_embedding_backend", _missing)
        result = ctx.load_semantic(timeout=10.0)
        assert result["status"] == "failed"
        assert result["reason"] == "embedding_assets_missing"
        assert "download" in result["message"]  # 提示走 /admin/models/download
        db.close()

    def test_health_and_models_do_not_trigger_assembly(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []

        def _spy(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            raise AssertionError("状态端点不应触发装配")

        monkeypatch.setattr(context_mod, "get_embedding_backend", _spy)
        health = admin_client.get("/admin/health", headers=_headers()).json()
        assert health["components"]["semantic"]["reason"] == "lazy_pending"
        models = admin_client.get("/admin/models", headers=_headers()).json()
        assert models["semantic"]["reason"] == "lazy_pending"
        assert calls == []


# ------------------------------------------------------------------ /admin/models


class TestModelsEndpoint:
    """GET /admin/models：chinese-clip / fasttext / 向量库 / 语义状态。"""

    def test_local_not_downloaded(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HF_HOME", raising=False)  # 强制默认 {data_dir}/models/hf
        body = admin_client.get("/admin/models", headers=_headers()).json()
        clip = body["chinese_clip"]
        assert clip["backend"] == "local"
        assert clip["status"] == "not_downloaded"
        assert clip["cached_files"] == 0
        assert clip["cache_size_bytes"] == 0
        assert clip["cache_dir"]  # 含 HF 缓存路径
        assert body["semantic"]["ready"] is False
        assert body["semantic"]["reason"] == "lazy_pending"

    def test_local_ready_when_hf_cache_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HF_HOME", raising=False)
        db, ctx = _build_context(tmp_path)
        # 伪造完整 HF 缓存：snapshots 非空 + blobs 若干字节
        cache_root = (
            tmp_path / "models" / "hf" / "hub" / ("models--" + _MODEL_ID.replace("/", "--"))
        )
        (cache_root / "snapshots" / "abc123").mkdir(parents=True)
        (cache_root / "snapshots" / "abc123" / "config.json").write_text("{}", encoding="utf-8")
        (cache_root / "blobs").mkdir(parents=True)
        (cache_root / "blobs" / "weight1").write_bytes(b"x" * 4096)
        (cache_root / "blobs" / "weight2").write_bytes(b"y" * 2048)
        client = _make_app(tmp_path, container=ctx, db=db)
        clip = client.get("/admin/models", headers=_headers()).json()["chinese_clip"]
        assert clip["status"] == "ready"
        assert clip["cached_files"] == 2
        assert clip["cache_size_bytes"] == 4096 + 2048
        client.close()
        db.close()

    def test_cloud_backend_status(self, tmp_path: Path) -> None:
        db, ctx = _build_context(tmp_path, embedding={"backend": "cloud"})
        client = _make_app(tmp_path, container=ctx, db=db)
        clip = client.get("/admin/models", headers=_headers()).json()["chinese_clip"]
        assert clip["backend"] == "cloud"
        assert clip["status"] == "cloud"
        client.close()
        db.close()

    def test_fasttext_and_vector_store_status(self, tmp_path: Path) -> None:
        db, ctx = _build_context(tmp_path)
        _seed_store(ctx, "black", rows=5, dim=8)
        _seed_store(ctx, "white", rows=2, dim=8)
        client = _make_app(tmp_path, container=ctx, db=db)
        body = client.get("/admin/models", headers=_headers()).json()
        ft = body["fasttext"]
        assert ft["configured"] is False
        assert ft["status"] == "not_configured"
        vec = body["vector_store"]
        assert vec["black"] == {"count": 5, "dim": 8}
        assert vec["white"] == {"count": 2, "dim": 8}
        client.close()
        db.close()

    def test_fasttext_ready_and_missing(self, tmp_path: Path) -> None:
        # 已配置可加载（config.json 含必需键 + 模型文件存在）
        cfg_dir = tmp_path / "ft"
        cfg_dir.mkdir()
        (cfg_dir / "model.pt").write_bytes(b"dummy")
        config_json = {
            "nbuckets": 16,
            "emb_dim": 4,
            "ngram_min": 3,
            "ngram_max": 6,
            "pad_ch": "<",
            "end_ch": ">",
            "classes": ["安全", "违规"],
            "class_to_idx": {"安全": 0, "违规": 1},
            "violation_class": "违规",
        }
        (cfg_dir / "config.json").write_text(json.dumps(config_json), encoding="utf-8")
        db, ctx = _build_context(
            tmp_path,
            light_model={
                "model_path": str(cfg_dir / "model.pt"),
                "config_path": str(cfg_dir / "config.json"),
            },
        )
        client = _make_app(tmp_path, container=ctx, db=db)
        ft = client.get("/admin/models", headers=_headers()).json()["fasttext"]
        assert ft["configured"] is True
        assert ft["loadable"] is True
        assert ft["status"] == "ready"
        client.close()
        db.close()

        # 已配置但文件缺失 → missing
        db2, ctx2 = _build_context(
            tmp_path,
            light_model={
                "model_path": str(tmp_path / "nope.pt"),
                "config_path": str(tmp_path / "nope.json"),
            },
        )
        client2 = _make_app(tmp_path, container=ctx2, db=db2)
        ft2 = client2.get("/admin/models", headers=_headers()).json()["fasttext"]
        assert ft2["status"] == "missing"
        client2.close()
        db2.close()

    def test_requires_auth(self, tmp_path: Path) -> None:
        db, ctx = _build_context(tmp_path)
        client = _make_app(tmp_path, container=ctx, db=db)
        assert client.get("/admin/models").status_code == 401
        client.close()
        db.close()


# ------------------------------------------------------------ 下载任务生命周期


class TestDownloadTask:
    """POST /admin/models/download + 进度轮询 + 同模型互斥 + 失败路径（假下载器）。"""

    def _fake_blocking(self, released: threading.Event) -> Any:
        def _fake(model_name: str, cache_dir: str, task: Any) -> None:
            task.update_progress(stage="downloading", progress=10.0, bytes_done=1024, total=10240)
            released.wait(10)
            task.update_progress(progress=100.0, bytes_done=10240)
            task.mark_completed()

        return _fake

    def test_download_running_to_completed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        released = threading.Event()
        monkeypatch.setattr(model_repo, "download_clip_weights", self._fake_blocking(released))
        db, ctx = _build_context(tmp_path)
        client = _make_app(tmp_path, container=ctx, db=db)
        resp = client.post("/admin/models/download", json={}, headers=_headers())
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        assert resp.json()["reused"] is False
        # 进行中：阶段 / 百分比 / 已下载字节
        snap = client.get(f"/admin/models/download/{task_id}", headers=_headers()).json()
        assert snap["status"] == "running"
        assert snap["stage"] == "downloading"
        assert snap["progress"] == 10.0
        assert snap["downloaded_bytes"] == 1024
        assert snap["total_bytes"] == 10240
        # 同模型并发互斥：进行中再次 POST → 复用同一 task
        resp2 = client.post("/admin/models/download", json={}, headers=_headers())
        assert resp2.status_code == 202
        assert resp2.json()["reused"] is True
        assert resp2.json()["task_id"] == task_id
        # 释放 → 完成
        released.set()
        for _ in range(100):
            snap = client.get(f"/admin/models/download/{task_id}", headers=_headers()).json()
            if snap["status"] == "completed":
                break
            time.sleep(0.02)
        assert snap["status"] == "completed"
        assert snap["progress"] == 100.0
        client.close()
        db.close()

    def test_download_failure_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_fail(model_name: str, cache_dir: str, task: Any) -> None:
            task.update_progress(stage="downloading", progress=5.0)
            raise RuntimeError("网络不可达（模拟下载失败）")

        monkeypatch.setattr(model_repo, "download_clip_weights", _fake_fail)
        db, ctx = _build_context(tmp_path)
        client = _make_app(tmp_path, container=ctx, db=db)
        resp = client.post("/admin/models/download", json={}, headers=_headers())
        task_id = resp.json()["task_id"]
        snap: dict[str, Any] = {}
        for _ in range(100):
            snap = client.get(f"/admin/models/download/{task_id}", headers=_headers()).json()
            if snap["status"] == "failed":
                break
            time.sleep(0.02)
        assert snap["status"] == "failed"
        assert "网络不可达" in snap["error"]
        # 未知任务 404
        unknown = client.get("/admin/models/download/no-such-task", headers=_headers())
        assert unknown.status_code == 404
        client.close()
        db.close()

    def test_download_manager_unit_reuse_and_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        released = threading.Event()
        monkeypatch.setattr(model_repo, "download_clip_weights", self._fake_blocking(released))
        manager = model_repo.DownloadManager()
        task1, reused1 = manager.start(_MODEL_ID, "C:/tmp-cache")
        assert reused1 is False
        task2, reused2 = manager.start(_MODEL_ID, "C:/tmp-cache")
        assert reused2 is True
        assert task2.task_id == task1.task_id
        assert manager.get(task1.task_id) is task1
        assert manager.get("missing") is None
        assert manager.running_for(_MODEL_ID) is task1
        released.set()
        for _ in range(100):
            if task1.status == "completed":
                break
            time.sleep(0.02)
        assert task1.status == "completed"
        assert manager.running_for(_MODEL_ID) is None  # 完成后进行中登记清理


# ------------------------------------------------------------------ /admin/health


class TestAdminHealth:
    """GET /admin/health：组件清单 / 降级原因码 / 数据概况 / 缓存统计。"""

    def test_health_fields(self, tmp_path: Path) -> None:
        db, ctx = _build_context(tmp_path)
        _seed_store(ctx, "black", rows=4, dim=6)
        db.add_keywords([("色情", "裸聊", "test")])
        db.add_rules([("色情", "裸聊", "exempt", None)])
        client = _make_app(tmp_path, container=ctx, db=db)
        body = client.get("/admin/health", headers=_headers()).json()
        assert body["status"] == "ok"
        assert set(body["components"]) == {
            "light_model",
            "embedding",
            "semantic",
            "llm",
            "keyword_engine",
            "rules",
            "vector_black",
            "vector_white",
        }
        assert body["components"]["semantic"]["reason"] == "lazy_pending"
        assert body["components"]["vector_black"]["count"] == 4
        assert body["components"]["vector_black"]["dim"] == 6
        # 数据概况
        assert body["data"] == {
            "keywords": 1,
            "vector_black": 4,
            "vector_white": 0,
            "whitelist_images": 0,
            "rules": 1,
        }
        # degraded 与 :8000 同口径（组件名清单），缓存统计复用 stats()
        assert "embedding" in body["degraded"]
        assert "semantic" in body["degraded"]
        assert body["cache"]["audit_cache"]["enabled"] is True
        assert body["uptime_s"] >= 0
        client.close()
        db.close()


# ------------------------------------------------------------------ 试运行


class TestTrialEndpoints:
    """GET /admin/test-examples 与 POST /admin/test-audit（PRD v0.3.0 M2）。"""

    def test_test_examples_sampling(self, tmp_path: Path, admin_client: TestClient) -> None:
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir(parents=True, exist_ok=True)
        dup = "黑白同文去重样本"

        def _write_csv(pool: str, texts: list[str]) -> None:
            lines = ["text,label,category,source"]
            lines += [f"{t},1,测试,test" for t in texts]
            (corpus_dir / f"{pool}.csv").write_text("\n".join(lines), encoding="utf-8")

        # 10 条（含 1 条跨池重复）；总去重后 19 ≤ 20 → 全量返回（无抽样随机性）
        _write_csv("black", [f"black 示例 {i}" for i in range(9)] + [dup])
        _write_csv("white", [f"white 示例 {i}" for i in range(9)] + [dup])
        body = admin_client.get("/admin/test-examples", headers=_headers()).json()
        assert body["total"] == 19
        pools = {item["pool"] for item in body["items"]}
        assert pools <= {"black", "white"}
        assert all(len(item["text"]) <= 200 for item in body["items"])
        # 跨池去重：同文只出现一次（duplicate 行只会来自先读到的 black.csv）
        assert sum(1 for item in body["items"] if item["text"] == dup) == 1
        # 黑 / 白混合
        assert "black" in pools and "white" in pools

    def test_test_examples_missing_corpus_empty(self, admin_client: TestClient) -> None:
        body = admin_client.get("/admin/test-examples", headers=_headers()).json()
        assert body == {"items": [], "total": 0}

    def test_test_audit_full_detail(self, tmp_path: Path) -> None:
        db, ctx = _build_context(tmp_path)
        db.add_keywords([("色情", "裸聊", "test")])
        assert ctx.reload_keywords() is True  # 词库热重载进引擎
        client = _make_app(tmp_path, container=ctx, db=db)
        resp = client.post(
            "/admin/test-audit", json={"text": "快来裸聊，加我微信"}, headers=_headers()
        )
        assert resp.status_code == 200
        body = resp.json()
        # 管理令牌 → full 语义：返回完整 detail
        assert body["detail"] is not None
        assert body["detail"]["keyword"] is not None
        hits = body["detail"]["keyword"]["hits"]
        assert hits and any(h["keyword"] == "裸聊" for h in hits)
        assert body["source"] in ("semantic", "basic_rules_pass")
        # 鉴权要求
        assert client.post("/admin/test-audit", json={"text": "x"}).status_code == 401
        client.close()
        db.close()


# ------------------------------------------------------------------ 测试连接


class TestTestConnection:
    """POST /admin/config/test-connection：三渠道错误路径可读、不崩。"""

    def test_embedding_cloud_missing_key(self, admin_client: TestClient) -> None:
        body = admin_client.post(
            "/admin/config/test-connection",
            json={
                "channel": "embedding",
                "config": {"backend": "cloud", "cloud": {"base_url": "http://x", "model": "m"}},
            },
            headers=_headers(),
        ).json()
        assert body["ok"] is False
        assert "未配置密钥" in body["message"]

    def test_embedding_cloud_missing_base_url(self, admin_client: TestClient) -> None:
        body = admin_client.post(
            "/admin/config/test-connection",
            json={"channel": "embedding", "config": {"backend": "cloud"}},
            headers=_headers(),
        ).json()
        assert body["ok"] is False
        assert "base_url" in body["message"]

    def test_embedding_local_not_cached(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HF_HOME", raising=False)
        body = admin_client.post(
            "/admin/config/test-connection",
            json={"channel": "embedding"},
            headers=_headers(),
        ).json()
        assert body["ok"] is False
        assert "未缓存" in body["message"]
        assert "/admin/models" in body["message"]

    def test_llm_missing_key(self, admin_client: TestClient) -> None:
        body = admin_client.post(
            "/admin/config/test-connection", json={"channel": "llm"}, headers=_headers()
        ).json()
        assert body["ok"] is False
        assert "未配置密钥" in body["message"]

    def test_fasttext_not_configured(self, admin_client: TestClient) -> None:
        body = admin_client.post(
            "/admin/config/test-connection", json={"channel": "fasttext"}, headers=_headers()
        ).json()
        assert body["ok"] is False
        assert "未配置" in body["message"]

    def test_secret_payload_ignored(self, admin_client: TestClient) -> None:
        # 临时参数携带 api_key（红线）：被剥离，不参与合并，不返回密钥值
        body = admin_client.post(
            "/admin/config/test-connection",
            json={
                "channel": "embedding",
                "config": {
                    "backend": "cloud",
                    "cloud": {"base_url": "http://x", "model": "m", "api_key": "sk-secret-xyz"},
                },
            },
            headers=_headers(),
        ).json()
        assert body["ok"] is False
        assert "sk-secret-xyz" not in json.dumps(body, ensure_ascii=False)


# ------------------------------------------------------------------ 改密（C5）


class TestChangePassword:
    """POST /admin/config/password：校验 / 热切 / DB 持久化（T30A 遗留）。"""

    def test_wrong_current_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/admin/config/password",
            json={"current_password": "wrong-old", "new_password": "new-password-123"},
            headers=_headers(),
        )
        assert resp.status_code == 400
        assert "当前密码不正确" in resp.json()["error"]

    def test_short_new_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/admin/config/password",
            json={"current_password": TOKEN, "new_password": "short"},
            headers=_headers(),
        )
        assert resp.status_code == 400
        assert "10" in resp.json()["error"]

    def test_same_as_current_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/admin/config/password",
            json={"current_password": TOKEN, "new_password": TOKEN},
            headers=_headers(),
        )
        assert resp.status_code == 400
        assert "不能与当前密码相同" in resp.json()["error"]

    def test_success_swap_and_persist(self, tmp_path: Path, admin_client: TestClient) -> None:
        new_token = "brand-new-admin-token-999"
        resp = admin_client.post(
            "/admin/config/password",
            json={"current_password": TOKEN, "new_password": new_token},
            headers=_headers(TOKEN),
        )
        assert resp.status_code == 200
        assert resp.json()["persisted"] is True
        # 旧令牌立即失效，新令牌生效
        assert admin_client.get("/admin/config", headers=_headers(TOKEN)).status_code == 401
        assert admin_client.get("/admin/config", headers=_headers(new_token)).status_code == 200
        # DB 持久化（settings 表 admin.token）
        db = Database(tmp_path / "audit.db")
        row = db.get_setting("admin", "token")
        assert row is not None and json.loads(row["value_json"]) == new_token
        # 重启语义：新应用（无 config token / 无 env）从 DB 读回新令牌
        app2 = create_admin_app(
            db,
            WhitelistMatcher(db),
            config=_DuckCfg(tmp_path, token=None),
        )
        client2 = TestClient(app2)
        assert client2.get("/admin/config", headers=_headers(new_token)).status_code == 200
        assert client2.get("/admin/config", headers=_headers(TOKEN)).status_code == 401
        client2.close()
        db.close()

    def test_env_still_highest_at_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # env ADMIN_PASSWORD 仍最高优先（M4 决策 B：env 只覆盖内存不写 DB）
        db, ctx = _build_context(tmp_path)
        client = _make_app(tmp_path, container=ctx, db=db)
        client.post(
            "/admin/config/password",
            json={"current_password": TOKEN, "new_password": "db-persisted-token-1"},
            headers=_headers(TOKEN),
        )
        client.close()
        monkeypatch.setenv("ADMIN_PASSWORD", "env-token-42")
        app2 = create_admin_app(
            db,
            WhitelistMatcher(db),
            config=_DuckCfg(tmp_path, token=None),
        )
        client2 = TestClient(app2)
        assert client2.get("/admin/config", headers=_headers("env-token-42")).status_code == 200
        assert (
            client2.get("/admin/config", headers=_headers("db-persisted-token-1")).status_code
            == 401
        )
        client2.close()
        db.close()
