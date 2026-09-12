"""多提供者失败自动切换与熔断测试（PRD v0.4.0 M3 / T51）。

覆盖：
- FailoverCircuit 连续失败计数 / 冷却跳过 / 成功清零 / 禁用熔断；
- MultiProvider 按优先级调用、active_provider 首选、全部失败上抛最后异常；
- MultiLLM 包装 judge 返回 None 时切换下一提供者；
- MultiRerank 包装 rerank 抛异常时切换下一提供者，全部失败返回保底候选。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from safefusion.engines import failover as failover_mod
from safefusion.engines.embedding import MultiEmbedding
from safefusion.engines.failover import (
    AllProvidersFailed,
    FailoverCircuit,
    ProviderError,
    call_with_failover,
    call_with_failover_async,
    order_providers,
)
from safefusion.engines.llm_client import MultiLLM, build_llm
from safefusion.engines.rerank import CloudRerank, MultiRerank


class _OkProvider:
    """成功提供者：记录调用次数并返回固定值。"""

    def __init__(self, value: Any = "ok") -> None:
        self.value = value
        self.calls = 0

    def call(self) -> Any:
        self.calls += 1
        return self.value


class _FailProvider:
    """失败提供者：调用时抛 ProviderError（模拟后端不可用）。"""

    def __init__(self, message: str = "boom") -> None:
        self.message = message
        self.calls = 0

    def call(self) -> Any:
        self.calls += 1
        raise ProviderError(self.message)


class TestFailoverCircuit:
    """熔断器：连续失败达到阈值进入冷却，冷却期跳过，成功清零。"""

    def test_success_resets_failures(self) -> None:
        circuit = FailoverCircuit(max_failures=3, cooldown_seconds=60)
        circuit.record_failure()
        circuit.record_failure()
        circuit.record_success()
        assert circuit.is_available()

    def test_cooling_after_max_failures(self) -> None:
        circuit = FailoverCircuit(max_failures=2, cooldown_seconds=60)
        circuit.record_failure()
        assert circuit.is_available()
        circuit.record_failure()
        assert not circuit.is_available()

    def test_cooling_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """冷却到期后恢复可用（假时钟推进，不依赖真实 sleep 与时钟粒度）。

        原用例 ``cooldown_seconds=0.05`` + ``sleep(0.06)`` 的余量仅 10ms，**小于**
        ``time.monotonic()`` 的时钟粒度（Windows GetTickCount64 = 15.625ms）：
        量化后可能仍判定「冷却中」——同一份代码在两次全量运行间翻转
        （v0.5.0 实测先 689 passed、后 1 failed/688 passed）。改为注入假时钟后
        判定完全确定，且顺带覆盖边界（未到期 / 刚到期）。
        """

        clock = {"t": 1000.0}
        monkeypatch.setattr(failover_mod, "monotonic", lambda: clock["t"])
        circuit = FailoverCircuit(max_failures=1, cooldown_seconds=30)
        circuit.record_failure()
        assert not circuit.is_available()
        clock["t"] += 29.9  # 冷却未到
        assert not circuit.is_available()
        clock["t"] += 0.2  # 累计 30.1s ≥ 30s → 到期恢复
        assert circuit.is_available()

    def test_disabled_never_opens(self) -> None:
        circuit = FailoverCircuit(enabled=False, max_failures=1, cooldown_seconds=60)
        circuit.record_failure()
        circuit.record_failure()
        assert circuit.is_available()


class _Multi:
    """可复用多提供者包装：把提供者 call 方法串成一条调用链。"""

    def __init__(self, providers: list[Any], active: str | None = None) -> None:
        self.providers = providers
        self.active = active

    def execute(self) -> Any:
        ordered = sorted(self.providers, key=lambda p: (p.name != self.active, p.priority))
        last_exc: Exception | None = None
        for provider in ordered:
            circuit = provider.circuit
            if not circuit.is_available():
                continue
            try:
                value = provider.call()
                circuit.record_success()
                return value
            except Exception as exc:
                circuit.record_failure()
                last_exc = exc
        raise ProviderError("所有提供者均不可用") from last_exc


class _P:
    def __init__(self, name: str, priority: int, inner: Any, failover: dict | None = None) -> None:
        self.name = name
        self.priority = priority
        self.inner = inner
        self.circuit = FailoverCircuit(**(failover or {}))

    def call(self) -> Any:
        return self.inner.call()

    # LLM/Rerank 包装器按鸭子类型调用对应方法，这里把调用委托给 inner
    async def judge(self, text: Any, images: Any, context: Any, **kwargs: Any) -> Any:
        return self.inner.call()

    def rerank(self, query_vec: Any, candidates: Any) -> Any:
        return self.inner.call()


class TestMultiProviderOrder:
    """按优先级顺序 + active_provider 首选。"""

    def test_priority_order(self) -> None:
        first = _P("first", 1, _OkProvider("a"))
        second = _P("second", 2, _OkProvider("b"))
        multi = _Multi([first, second])
        assert multi.execute() == "a"
        assert first.inner.calls == 1
        assert second.inner.calls == 0

    def test_active_provider_first(self) -> None:
        first = _P("first", 1, _OkProvider("a"))
        second = _P("second", 2, _OkProvider("b"))
        multi = _Multi([first, second], active="second")
        assert multi.execute() == "b"
        assert second.inner.calls == 1
        assert first.inner.calls == 0

    def test_failover_to_next(self) -> None:
        first = _P("first", 1, _FailProvider("first down"))
        second = _P("second", 2, _OkProvider("b"))
        multi = _Multi([first, second])
        assert multi.execute() == "b"
        assert first.inner.calls == 1
        assert second.inner.calls == 1

    def test_all_fail_raises(self) -> None:
        first = _P("first", 1, _FailProvider("a"))
        second = _P("second", 2, _FailProvider("b"))
        multi = _Multi([first, second])
        with pytest.raises(ProviderError):
            multi.execute()

    def test_circuit_skips_cooling_provider(self) -> None:
        first = _P(
            "first", 1, _FailProvider("a"), failover={"max_failures": 1, "cooldown_seconds": 60}
        )
        second = _P("second", 2, _OkProvider("b"))
        multi = _Multi([first, second])
        assert multi.execute() == "b"
        # 第一次调用：first 失败一次后熔断；第二次调用直接跳过 first
        assert multi.execute() == "b"
        assert first.inner.calls == 1


class TestMultiLLM:
    """LLM 多提供者：返回 None 时切换下一个，全部失败返回 None。"""

    async def test_none_falls_to_next(self) -> None:
        first = _P("first", 1, _OkProvider(None))
        second = _P("second", 2, _OkProvider({"is_violation": True}))
        llm = MultiLLM(
            [
                (first.name, first.priority, first, first.circuit),
                (second.name, second.priority, second, second.circuit),
            ]
        )
        result = await llm.judge("text", [], None)
        assert result == {"is_violation": True}
        assert first.inner.calls == 1
        assert second.inner.calls == 1

    async def test_all_none_returns_none(self) -> None:
        first = _P("first", 1, _OkProvider(None))
        second = _P("second", 2, _OkProvider(None))
        llm = MultiLLM(
            [
                (first.name, first.priority, first, first.circuit),
                (second.name, second.priority, second, second.circuit),
            ]
        )
        assert await llm.judge("text", [], None) is None


class TestMultiRerank:
    """Rerank 多提供者：异常切换，全部失败返回保底候选（原 score）。"""

    def test_exception_falls_to_next(self) -> None:
        first = _P("first", 1, _FailProvider("remote down"))
        second = _P("second", 2, _OkProvider([{"id": "b", "score": 0.5, "rerank_score": 0.9}]))
        rerank = MultiRerank(
            [
                (first.name, first.priority, first, first.circuit),
                (second.name, second.priority, second, second.circuit),
            ]
        )
        out = rerank.rerank(np.zeros(2), [{"id": "b", "score": 0.5, "metadata": {}}])
        assert out[0]["rerank_score"] == 0.9

    def test_all_fail_fallback_original_score(self) -> None:
        first = _P("first", 1, _FailProvider("a"))
        second = _P("second", 2, _FailProvider("b"))
        rerank = MultiRerank(
            [
                (first.name, first.priority, first, first.circuit),
                (second.name, second.priority, second, second.circuit),
            ]
        )
        candidates = [{"id": "b", "score": 0.7, "metadata": {}}]
        out = rerank.rerank(np.zeros(2), candidates)
        assert out[0]["rerank_score"] == 0.7

    def test_cloud_rerank_parse_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = CloudRerank(
            {
                "base_url": "http://rerank.local/v1",
                "model": "m",
                "api_key_env": "RERANK_TEST_KEY",
                "allow_no_key": True,
            }
        )
        monkeypatch.setenv("RERANK_TEST_KEY", "x")

        class _Resp:
            def raise_for_status(self) -> None:
                return

            def json(self) -> dict[str, Any]:
                return {"results": [{"index": 1, "relevance_score": 0.9}]}

        calls: list[dict[str, Any]] = []

        def _post(url: str, json: dict[str, Any]) -> _Resp:
            calls.append(json)
            return _Resp()

        monkeypatch.setattr(backend._client, "post", _post)
        out = backend.rerank(
            np.zeros(2),
            [
                {"id": "a", "score": 0.1, "metadata": {"text": "x"}},
                {"id": "b", "score": 0.2, "metadata": {"text": "y"}},
            ],
        )
        assert calls[0]["documents"] == ["x", "y"]
        assert out[0]["id"] == "b"
        assert out[0]["rerank_score"] == 0.9
        assert out[1]["rerank_score"] == 0.1
        backend.close()


class _EmbeddingStub:
    """MultiEmbedding 可用的最小 Embedding 桩。"""

    def __init__(self, text_value: Any = None, fail: bool = False) -> None:
        self.text_value = text_value
        self.fail = fail
        self.text_calls = 0
        self.image_calls = 0

    def encode_texts(self, texts: list[str]) -> Any:
        self.text_calls += 1
        if self.fail:
            raise ProviderError("embedding down")
        return self.text_value

    def encode_images(self, images: list[Any]) -> Any:
        self.image_calls += 1
        if self.fail:
            raise ProviderError("embedding down")
        return self.text_value


class TestMultiEmbedding:
    """Embedding 多提供者：按优先级切换、active 首选、全部失败上抛。"""

    def test_priority_order(self) -> None:
        first = _EmbeddingStub("a")
        second = _EmbeddingStub("b")
        multi = MultiEmbedding(
            [
                ("first", 1, first, FailoverCircuit()),
                ("second", 2, second, FailoverCircuit()),
            ]
        )
        assert multi.encode_texts(["x"]) == "a"
        assert first.text_calls == 1
        assert second.text_calls == 0

    def test_active_provider_first(self) -> None:
        first = _EmbeddingStub("a")
        second = _EmbeddingStub("b")
        multi = MultiEmbedding(
            [
                ("first", 1, first, FailoverCircuit()),
                ("second", 2, second, FailoverCircuit()),
            ],
            active_provider="second",
        )
        assert multi.encode_texts(["x"]) == "b"
        assert second.text_calls == 1
        assert first.text_calls == 0

    def test_failover_to_next(self) -> None:
        first = _EmbeddingStub(fail=True)
        second = _EmbeddingStub("ok")
        multi = MultiEmbedding(
            [
                ("first", 1, first, FailoverCircuit()),
                ("second", 2, second, FailoverCircuit()),
            ]
        )
        assert multi.encode_texts(["x"]) == "ok"
        assert first.text_calls == 1
        assert second.text_calls == 1

    def test_all_fail_raises(self) -> None:
        first = _EmbeddingStub(fail=True)
        second = _EmbeddingStub(fail=True)
        multi = MultiEmbedding(
            [
                ("first", 1, first, FailoverCircuit()),
                ("second", 2, second, FailoverCircuit()),
            ]
        )
        with pytest.raises(ProviderError):
            multi.encode_texts(["x"])


class TestBuildLlm:
    """build_llm 工厂：providers 空返回单客户端，非空返回 MultiLLM。"""

    def test_empty_providers_returns_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        class _FakeClient:
            def __init__(self, cfg: dict[str, Any]) -> None:
                calls.append(cfg)
                self.available = True

        monkeypatch.setattr("safefusion.engines.llm_client.LLMClient", _FakeClient)
        from safefusion.config import AppConfig

        cfg = AppConfig()
        out = build_llm(cfg.llm)
        assert isinstance(out, _FakeClient)
        assert calls[-1]["base_url"] == "https://api.openai.com/v1"

    def test_providers_returns_multi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        class _FakeClient:
            def __init__(self, cfg: dict[str, Any]) -> None:
                calls.append(cfg)
                self.available = True

        monkeypatch.setattr("safefusion.engines.llm_client.LLMClient", _FakeClient)
        from safefusion.config import AppConfig

        cfg = AppConfig.model_validate(
            {
                "llm": {
                    "providers": [
                        {"name": "a", "base_url": "http://a", "model": "m1", "priority": 1},
                        {"name": "b", "base_url": "http://b", "model": "m2", "priority": 2},
                    ],
                    "active_provider": "b",
                }
            }
        )
        out = build_llm(cfg.llm)
        from safefusion.engines.llm_client import MultiLLM

        assert isinstance(out, MultiLLM)
        assert len(calls) == 2
        assert calls[1]["model"] == "m2"


class _StubBackend:
    """通用提供者桩：可成功返回值或抛异常，记录调用次数。"""

    def __init__(self, *, value: Any = None, fail: bool = False, message: str = "boom") -> None:
        self.value = value
        self.fail = fail
        self.message = message
        self.calls = 0

    def call(self, payload: Any = None) -> Any:
        self.calls += 1
        if self.fail:
            raise ProviderError(self.message)
        return self.value


class TestSharedFailoverExecutor:
    """通用失败切换执行器（Brooks-Lint P1：三处 provider 切换逻辑的唯一实现）。"""

    def test_order_active_first_then_priority(self) -> None:
        a = _StubBackend(value="a")
        b = _StubBackend(value="b")
        c = _StubBackend(value="c")
        providers = [
            ("a", 3, a, FailoverCircuit()),
            ("b", 1, b, FailoverCircuit()),
            ("c", 2, c, FailoverCircuit()),
        ]
        assert [e[0] for e in order_providers(providers, None)] == ["b", "c", "a"]
        assert [e[0] for e in order_providers(providers, "a")] == ["a", "b", "c"]

    def test_first_success_returns_and_marks_success(self) -> None:
        ok = _StubBackend(value="ok")
        providers = [("ok", 1, ok, FailoverCircuit())]
        assert call_with_failover(providers, lambda b: b.call()) == "ok"
        assert ok.calls == 1

    def test_failover_to_next_on_exception(self) -> None:
        bad = _StubBackend(fail=True)
        good = _StubBackend(value="good")
        providers = [
            ("bad", 1, bad, FailoverCircuit()),
            ("good", 2, good, FailoverCircuit()),
        ]
        assert call_with_failover(providers, lambda b: b.call()) == "good"
        assert (bad.calls, good.calls) == (1, 1)

    def test_all_fail_raises_with_last_cause(self) -> None:
        first = _StubBackend(fail=True, message="first")
        second = _StubBackend(fail=True, message="second")
        providers = [
            ("first", 1, first, FailoverCircuit()),
            ("second", 2, second, FailoverCircuit()),
        ]
        with pytest.raises(AllProvidersFailed) as excinfo:
            call_with_failover(providers, lambda b: b.call())
        assert isinstance(excinfo.value.__cause__, ProviderError)
        assert str(excinfo.value.__cause__) == "second"

    def test_soft_failure_triggers_switch(self) -> None:
        none_backend = _StubBackend(value=None)
        dict_backend = _StubBackend(value={"ok": True})
        providers = [
            ("none", 1, none_backend, FailoverCircuit()),
            ("dict", 2, dict_backend, FailoverCircuit()),
        ]
        result = call_with_failover(providers, lambda b: b.call(), is_failure=lambda v: v is None)
        assert result == {"ok": True}
        assert (none_backend.calls, dict_backend.calls) == (1, 1)

    def test_unavailable_provider_skipped(self) -> None:
        skipped = _StubBackend(value="skipped")
        usable = _StubBackend(value="usable")
        open_circuit = FailoverCircuit(max_failures=1, cooldown_seconds=60)
        open_circuit.record_failure()  # 立即熔断
        providers = [
            ("skipped", 1, skipped, open_circuit),
            ("usable", 2, usable, FailoverCircuit()),
        ]
        assert call_with_failover(providers, lambda b: b.call()) == "usable"
        assert skipped.calls == 0

    async def test_async_executor_returns_and_raises(self) -> None:
        async def _call(backend: _StubBackend) -> Any:
            return backend.call()

        ok = _StubBackend(value="async-ok")
        result = await call_with_failover_async([("ok", 1, ok, FailoverCircuit())], _call)
        assert result == "async-ok"

        bad = _StubBackend(fail=True)
        with pytest.raises(AllProvidersFailed):
            await call_with_failover_async([("bad", 1, bad, FailoverCircuit())], _call)
