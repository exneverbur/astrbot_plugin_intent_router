"""发言记录的聚合：时间衰减加权、硬熔断计数、群活跃度。

发送层钩子只负责"记一笔"，怎么用是这里的事。
时间上不再用固定窗口三档计数，而是多尺度半衰期混合：

    0 分钟 → 1.00    5 分钟 → 0.71    15 分钟 → 0.47
    1 小时 → 0.27    6 小时 → 0.10    24 小时 → 0.01

越近的发言权重越高，且是连续衰减，不会在窗口边界上跳变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .decision import Density


def decay_weight(age_minutes: float, tunable: Any) -> float:
    """一条发言按"离现在多久"折算成权重。"""

    age = max(0.0, float(age_minutes))
    short = 0.5 ** (age / max(0.01, float(tunable.half_life_short)))
    mid = 0.5 ** (age / max(0.01, float(tunable.half_life_mid)))
    long = 0.5 ** (age / max(0.01, float(tunable.half_life_long)))
    return (
        float(tunable.mix_short) * short
        + float(tunable.mix_mid) * mid
        + float(tunable.mix_long) * long
    )


@dataclass
class DensitySnapshot:
    weighted_recent: float = 0.0
    normalized_load: float = 0.0
    silence_seconds: float = 0.0
    counts: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.counts = dict(self.counts or {})

    def as_density(self) -> Density:
        return Density(
            weighted_recent=self.weighted_recent,
            normalized_load=self.normalized_load,
            silence_seconds=self.silence_seconds,
        )


class Registry:
    """发言记录的使用方（写入在发送层钩子里直接调 storage）。"""

    def __init__(self, storage: Any, tunable: Any) -> None:
        self.storage = storage
        self.tunable = tunable

    def set_tunable(self, tunable: Any) -> None:
        self.tunable = tunable

    def record_speech(self, umo: str, *, ts: float | None = None, trigger_id: str = "") -> None:
        self.storage.record_speech(umo, ts, trigger_id)

    def snapshot(self, umo: str, now: float | None = None) -> DensitySnapshot:
        """算这一轮要用的密度三件套 + 硬熔断计数。"""

        moment = float(now if now is not None else time.time())
        longest = max(
            float(self.tunable.breaker_long_window),
            float(self.tunable.half_life_long) * 12 * 60,
        )
        since = moment - longest
        speech = self.storage.speech_since(umo, since)
        weighted = 0.0
        counts = {"short": 0, "mid": 0, "long": 0}
        last_bot = 0.0
        for item in speech:
            age_minutes = max(0.0, (moment - float(item.get("ts") or 0.0)) / 60.0)
            weighted += decay_weight(age_minutes, self.tunable)
            age_seconds = age_minutes * 60.0
            if age_seconds <= float(self.tunable.breaker_short_window):
                counts["short"] += 1
            if age_seconds <= float(self.tunable.breaker_mid_window):
                counts["mid"] += 1
            if age_seconds <= float(self.tunable.breaker_long_window):
                counts["long"] += 1
            last_bot = max(last_bot, float(item.get("ts") or 0.0))

        user_since = moment - max(1800.0, float(self.tunable.half_life_mid) * 60)
        user_msgs = self.storage.user_msg_since(umo, user_since)
        user_weighted = 0.0
        for item in user_msgs:
            age_minutes = max(0.0, (moment - float(item.get("ts") or 0.0)) / 60.0)
            user_weighted += decay_weight(age_minutes, self.tunable)
        normalized_load = 0.0 if user_weighted < 1.0 else weighted / user_weighted

        silence = (moment - last_bot) if last_bot else float(self.tunable.breaker_long_window)
        return DensitySnapshot(
            weighted_recent=round(weighted, 4),
            normalized_load=round(normalized_load, 4),
            silence_seconds=round(silence, 1),
            counts=counts,
        )
