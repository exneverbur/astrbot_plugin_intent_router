"""意愿来源：装了虚拟世界就用她的意愿，没装就用固定值。

这一层只做一件事：**永远给一个 0~1 的数，永远不抛异常**。
VM 超时、返回非法值、连续失败，都回落到默认值（默认 0.55），
连续失败到阈值就熔断一段时间，期间不再打扰 VM。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MODE_AUTO = "auto"
MODE_VM = "vm"
MODE_STATIC = "static"
MODE_HEURISTIC = "heuristic"


@dataclass
class Willingness:
    """一次意愿查询的结果。"""

    value: float = 0.55
    source: str = "static"
    sleeping: bool = False
    vm_enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4),
            "source": self.source,
            "sleeping": bool(self.sleeping),
            "vm_enabled": bool(self.vm_enabled),
        }


@dataclass
class ProviderStats:
    calls: int = 0
    fallbacks: int = 0
    failures_in_a_row: int = 0
    breaker_until: float = 0.0
    last_error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def note(self, kind: str, detail: str = "") -> None:
        self.events.append({"at": time.time(), "kind": kind, "detail": detail[:200]})
        if len(self.events) > 50:
            self.events = self.events[-50:]

    @property
    def breaker_open(self) -> bool:
        return self.breaker_until > time.time()


class WillingnessProvider:
    """意愿来源适配器。

    ``vm_getter`` 返回 VM 插件实例（None 表示没装）。每次查询都重新取一次，
    这样插件热重载之后也能接上。
    """

    def __init__(
        self,
        *,
        mode: str,
        tunable: Any,
        vm_getter: Callable[[], Any],
    ) -> None:
        self.mode = mode if mode in (MODE_AUTO, MODE_VM, MODE_STATIC, MODE_HEURISTIC) else MODE_AUTO
        self.tunable = tunable
        self.vm_getter = vm_getter
        self.stats = ProviderStats()

    def set_tunable(self, tunable: Any) -> None:
        self.tunable = tunable

    # ---------------- 查询 ----------------

    async def get(self, umo: str) -> Willingness:
        """拿这个会话此刻的意愿。永不抛异常。"""

        default = float(self.tunable.willingness_default)
        mode = self.mode
        vm = self.vm_getter() if mode in (MODE_AUTO, MODE_VM) else None
        if mode == MODE_STATIC or (mode == MODE_AUTO and vm is None):
            return Willingness(value=default, source="static")
        if mode == MODE_HEURISTIC:
            return Willingness(value=self._heuristic(default), source="heuristic")
        if vm is None:
            return Willingness(value=default, source="static")
        if self.stats.breaker_open:
            return Willingness(value=default, source="vm-breaker")
        return await self._from_vm(vm, umo, default)

    async def _from_vm(self, vm: Any, umo: str, default: float) -> Willingness:
        self.stats.calls += 1
        timeout = float(self.tunable.vm_timeout)
        try:
            snapshot = await asyncio.wait_for(self._snapshot(vm, umo), timeout=timeout)
        except asyncio.TimeoutError:
            return self._fail(default, f"虚拟世界响应超过 {timeout:.1f}s")
        except Exception as exc:
            return self._fail(default, f"{type(exc).__name__}: {exc}")

        if snapshot is None:
            # VM 说"这个会话没启用"，那不是错误：用默认值，但不记失败
            return Willingness(value=default, source="vm-disabled")
        raw = snapshot.get("willingness")
        if raw is None:
            raw = snapshot.get("loneliness")
        if raw is None:
            raw = snapshot.get("affect", snapshot.get("social"))
        try:
            value = max(0.0, min(1.0, float(raw if raw is not None else default)))
        except (TypeError, ValueError):
            return self._fail(default, f"意愿值非法：{raw!r}")
        self.stats.failures_in_a_row = 0
        return Willingness(
            value=value,
            source="vm",
            sleeping=bool(snapshot.get("sleeping")),
            vm_enabled=bool(snapshot.get("interject_enabled", True)),
        )

    @staticmethod
    async def _snapshot(vm: Any, umo: str) -> dict[str, Any] | None:
        """优先用 ``social_snapshot``（一次拿到意愿 + 是否在睡觉），没有就退回 ``reply_willingness``。"""

        snapshot = getattr(vm, "social_snapshot", None)
        if callable(snapshot):
            result = snapshot(umo)
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, dict) else None
        getter = getattr(vm, "reply_willingness", None)
        if callable(getter):
            result = getter(umo)
            if asyncio.iscoroutine(result):
                result = await result
            if result is None:
                return None
            return {"willingness": result}
        return None

    def _fail(self, default: float, reason: str) -> Willingness:
        self.stats.fallbacks += 1
        self.stats.failures_in_a_row += 1
        self.stats.last_error = reason
        self.stats.note("fallback", reason)
        if self.stats.failures_in_a_row >= int(self.tunable.vm_breaker_threshold):
            self.stats.breaker_until = time.time() + float(self.tunable.vm_breaker_cooldown)
            self.stats.failures_in_a_row = 0
            self.stats.note(
                "breaker",
                f"连续失败，暂停联动 {float(self.tunable.vm_breaker_cooldown):.0f} 秒",
            )
        return Willingness(value=default, source="fallback")

    def _heuristic(self, default: float) -> float:
        """没装 VM 时的兜底启发式：深夜降意愿。"""

        hour = time.localtime().tm_hour
        if hour >= 2 and hour < 7:
            return max(0.0, default - 0.25)
        if hour >= 23 or hour < 2:
            return max(0.0, default - 0.15)
        return default

    # ---------------- 状态 ----------------

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "calls": self.stats.calls,
            "fallbacks": self.stats.fallbacks,
            "breaker_open": self.stats.breaker_open,
            "breaker_seconds": max(0.0, round(self.stats.breaker_until - time.time(), 1)),
            "last_error": self.stats.last_error,
            "events": list(self.stats.events[-10:]),
        }
