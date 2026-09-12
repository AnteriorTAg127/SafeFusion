"""多提供者失败切换与熔断工具（PRD v0.4.0 M3）。

提供：
- :class:`ProviderError`：表示单个提供者调用失败（包装底层异常，供上层切换）；
- :class:`FailoverCircuit`：每提供者一个熔断器——连续失败达到
  ``max_failures`` 后进入冷却（``cooldown_seconds``），冷却期内
  :meth:`is_available` 返回 False（上层跳过该提供者）；成功一次即清零。
  熔断可整体禁用（``enabled=False`` 时恒可用，仅做失败切换不冷却）；
- :func:`call_with_failover` / :func:`call_with_failover_async`：**通用失败切换
  执行器**——按 :func:`order_providers` 顺序遍历可用提供者，逐个调用直到成功；
  全部失败抛 :class:`AllProvidersFailed`（携带最后一次异常为 ``__cause__``）。
  Embedding / LLM / ReRank 三处包装器统一复用本执行器，避免各写一份切换循环
  （Brooks-Lint R3：Knowledge Duplication）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any, TypeVar

T = TypeVar("T")


class ProviderError(RuntimeError):
    """单个提供者调用失败（由包装器捕获后触发失败切换）。"""


@dataclass
class FailoverCircuit:
    """单提供者熔断器。

    Attributes:
        enabled: False 时不做熔断（每次失败立即切换，不冷却）。
        max_failures: 连续失败多少次后进入冷却。
        cooldown_seconds: 冷却时长（秒）。
    """

    enabled: bool = True
    max_failures: int = 3
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        self._consecutive_failures: int = 0
        self._open_until: float = 0.0

    def is_available(self) -> bool:
        """当前提供者是否可用（未熔断 / 冷却已过）。"""
        if not self.enabled:
            return True
        if self._consecutive_failures < self.max_failures:
            return True
        return monotonic() >= self._open_until

    def record_success(self) -> None:
        """调用成功：清零连续失败计数（并解除冷却）。"""
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        """记录一次失败：达到阈值时进入冷却。"""
        if not self.enabled:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.max_failures:
            self._open_until = monotonic() + self.cooldown_seconds

    def snapshot(self) -> dict[str, Any]:
        """熔断器状态快照（供日志 / 管理端展示）。"""
        return {
            "enabled": self.enabled,
            "max_failures": self.max_failures,
            "cooldown_seconds": self.cooldown_seconds,
            "consecutive_failures": self._consecutive_failures,
            "open": not self.is_available(),
        }


#: 单条提供者登记：(name, priority, backend, circuit)
ProviderEntry = tuple[str, int, Any, FailoverCircuit]


class AllProvidersFailed(ProviderError):
    """所有提供者均不可用 / 调用失败（软失败亦计入）。

    ``__cause__`` 指向最后一次调用异常（可能为 None——全部被熔断跳过时）。
    """


def order_providers(
    providers: Sequence[ProviderEntry], active: str | None = None
) -> list[ProviderEntry]:
    """按「``active`` 手动指定者优先，其余按 priority 升序」排序（不改动入参）。"""
    return sorted(providers, key=lambda item: (item[0] != active, item[1]))


def _iter_available(
    providers: Sequence[ProviderEntry], active: str | None
) -> Iterator[ProviderEntry]:
    """按顺序产出当前可用（未熔断）的提供者登记。"""
    for entry in order_providers(providers, active):
        if entry[3].is_available():
            yield entry


def call_with_failover(
    providers: Sequence[ProviderEntry],
    call: Callable[[Any], T],
    *,
    active: str | None = None,
    is_failure: Callable[[T], bool] | None = None,
    logger: Any = None,
    label: str = "提供者",
) -> T:
    """同步失败切换执行器：首个成功结果即返回。

    Args:
        providers: ``(name, priority, backend, circuit)`` 列表。
        call: 对单个 backend 的调用（返回值类型即提供者输出）。
        active: 手动指定首选提供者名（该提供者优先于 priority）。
        is_failure: 软失败判定（如 LLM 返回 None 视为失败）；None 表示任何
            无异常返回都算成功。
        logger: 可选 logger，用于记录每次切换（失败仅告警，不中断）。
        label: 日志前缀（如 "Embedding 提供者"）。

    Returns:
        首个成功提供者的返回值。

    Raises:
        AllProvidersFailed: 无可用提供者或全部调用失败；其 ``__cause__``
            为最后一次调用异常（可能为 None）。
    """

    last_exc: Exception | None = None
    for name, _priority, backend, circuit in _iter_available(providers, active):
        try:
            result = call(backend)
        except Exception as exc:
            circuit.record_failure()
            last_exc = exc
            if logger is not None:
                logger.warning("%s %s 调用失败: %s", label, name, exc)
            continue
        if is_failure is not None and is_failure(result):
            circuit.record_failure()
            if logger is not None:
                logger.warning("%s %s 返回无效结果，切换到下一个", label, name)
            continue
        circuit.record_success()
        return result
    raise AllProvidersFailed(f"所有{label}均不可用") from last_exc


async def call_with_failover_async(
    providers: Sequence[ProviderEntry],
    call: Callable[[Any], Awaitable[T]],
    *,
    active: str | None = None,
    is_failure: Callable[[T], bool] | None = None,
    logger: Any = None,
    label: str = "提供者",
) -> T:
    """异步失败切换执行器（语义同 :func:`call_with_failover`，``call`` 为协程）。"""
    last_exc: Exception | None = None
    for name, _priority, backend, circuit in _iter_available(providers, active):
        try:
            result = await call(backend)
        except Exception as exc:
            circuit.record_failure()
            last_exc = exc
            if logger is not None:
                logger.warning("%s %s 调用失败: %s", label, name, exc)
            continue
        if is_failure is not None and is_failure(result):
            circuit.record_failure()
            if logger is not None:
                logger.warning("%s %s 返回无效结果，切换到下一个", label, name)
            continue
        circuit.record_success()
        return result
    raise AllProvidersFailed(f"所有{label}均不可用") from last_exc
