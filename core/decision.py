"""纯决策：把路由模型的分数 + 意愿 + 密度，算成"回 / 不回 / 插一句"。

这里不碰 IO，全部是纯函数，方便直接把边界情况全测一遍。
两条路径严格分开：

- **正常回复**：模型说值得回（worth），只做轻冷却 + 轻惩罚，意愿太低就排队延迟；
- **主动插嘴**：完全不相关但确实是个好梗，才有极低概率插一句，且要过硬熔断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REPLY = "reply"
PROACTIVE = "proactive"
IGNORE = "ignore"
BLOCKED = "blocked"
SAFETY = "safety"


@dataclass
class Verdict:
    """路由模型对一条消息的判断（分数统一 0~1）。"""

    idx: int = 0
    sender: str = ""
    to: str = ""
    directed: bool = False
    worth: bool = False
    reply_score: float = 0.0
    rare_interject_score: float = 0.0
    confidence: float = 0.0
    interest_match: float = 0.0
    relevance_to_bot: float = 0.0
    risk: float = 0.0
    suggested_action: str = "ignore"
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def scores(self) -> dict[str, float]:
        return {
            "reply_score": self.reply_score,
            "rare_interject_score": self.rare_interject_score,
            "confidence": self.confidence,
            "interest_match": self.interest_match,
            "relevance_to_bot": self.relevance_to_bot,
            "risk": self.risk,
        }


@dataclass
class Density:
    """密度相关的三个量（都由 registry 算好）。"""

    weighted_recent: float = 0.0
    """: 时间衰减加权的"她最近说过多少"。"""

    normalized_load: float = 0.0
    """: bot 发言占群活跃度的比例。"""

    silence_seconds: float = 0.0
    """: 距离她上一次开口过了多久（用来给沉默一点点加成）。"""


@dataclass
class Outcome:
    decision: str = IGNORE
    probability: float = 0.0
    base: float = 0.0
    penalty: float = 0.0
    delay: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "probability": round(self.probability, 4),
            "base": round(self.base, 4),
            "penalty": round(self.penalty, 4),
            "delay": round(self.delay, 2),
            "reason": self.reason,
        }


def density_penalty(density: Density, tunable: Any) -> float:
    """连续惩罚：说得越多越收敛，冷群几乎不惩罚。"""

    load_penalty = max(
        float(tunable.load_penalty_floor),
        1.0 - float(density.normalized_load) * float(tunable.load_penalty_slope),
    )
    penalty = float(tunable.penalty_base) ** max(0.0, float(density.weighted_recent))
    return max(0.0, min(1.0, penalty * load_penalty))


def breakers_hit(
    counts: dict[str, int], tunable: Any
) -> str:
    """硬熔断：三个尺度里任意一个超了就停。返回命中的那档说明（没命中返回空串）。"""

    checks = (
        ("近 10 分钟", counts.get("short", 0), int(tunable.breaker_short_count)),
        ("近 1 小时", counts.get("mid", 0), int(tunable.breaker_mid_count)),
        ("近 24 小时", counts.get("long", 0), int(tunable.breaker_long_count)),
    )
    for label, count, limit in checks:
        if limit > 0 and count >= limit:
            return f"{label}已说 {count} 次（上限 {limit}）"
    return ""


def decide_reply(
    verdict: Verdict, *, willingness: float, density: Density, tunable: Any
) -> Outcome:
    """正常回复路径：worth=true 时用，只做轻冷却 + 轻惩罚。"""

    penalty = density_penalty(density, tunable)
    priority = verdict.reply_score * (0.8 + 0.2 * penalty)
    outcome = Outcome(
        decision=REPLY,
        base=verdict.reply_score,
        penalty=penalty,
        probability=min(1.0, max(0.0, priority)),
        reason=verdict.reason,
    )
    if willingness < float(tunable.reply_willingness_line):
        # 意愿太低：不拒绝，但让她晚一点回，别显得秒答
        outcome.delay = float(tunable.reply_queue_delay)
        outcome.reason = (outcome.reason + "；意愿偏低，排队延迟").strip("；")
    return outcome


def decide_proactive(
    verdict: Verdict,
    *,
    willingness: float,
    density: Density,
    counts: dict[str, int],
    tunable: Any,
) -> Outcome:
    """主动插嘴路径：worth=false 但确实是个好梗，才有极低概率插一句。"""

    penalty = density_penalty(density, tunable)
    outcome = Outcome(base=float(tunable.base_p), penalty=penalty)
    if not bool(tunable.proactive_enabled):
        outcome.reason = "主动插嘴已关闭"
        return outcome
    if float(verdict.rare_interject_score) < float(tunable.rare_threshold):
        outcome.reason = f"稀有度不足（{verdict.rare_interject_score:.2f}）"
        return outcome
    if float(verdict.confidence) < float(tunable.confidence_threshold):
        outcome.reason = f"把握不足（{verdict.confidence:.2f}）"
        return outcome
    hit = breakers_hit(counts, tunable)
    if hit:
        outcome.decision = BLOCKED
        outcome.reason = f"硬熔断：{hit}"
        return outcome
    if willingness < float(tunable.willingness_floor):
        outcome.decision = BLOCKED
        outcome.reason = f"意愿过低（{willingness:.2f}）"
        return outcome
    silence_bonus = min(
        float(tunable.silence_bonus_cap),
        max(0.0, density.silence_seconds / 3600.0) * float(tunable.silence_bonus_cap),
    )
    probability = (
        float(tunable.base_p)
        * float(verdict.rare_interject_score)
        * willingness
        * penalty
        * (1.0 + silence_bonus)
    )
    outcome.probability = min(float(tunable.hard_cap), max(0.0, probability))
    outcome.decision = PROACTIVE
    outcome.reason = verdict.reason or "值得偶尔插一句"
    return outcome


def feedback_hint(outcome: Outcome) -> str:
    """给日志/看板用的一句人话。"""

    if outcome.decision == PROACTIVE:
        return f"主动插嘴（p={outcome.probability:.3f}）"
    if outcome.decision == REPLY:
        return f"放行回复（优先级 {outcome.probability:.2f}）"
    if outcome.decision == BLOCKED:
        return f"熔断不给回：{outcome.reason}"
    return f"不回复：{outcome.reason}"
