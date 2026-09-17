"""看板与 ``/router_stats`` 用的聚合。"""

from __future__ import annotations

import time
from typing import Any

from .decision import BLOCKED, IGNORE, PROACTIVE, REPLY

DECISIONS = (REPLY, PROACTIVE, BLOCKED, IGNORE)
BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001))


def histogram(values: list[float]) -> dict[str, int]:
    result: dict[str, int] = {}
    for low, high in BUCKETS:
        label = f"{low:.1f}-{min(1.0, high):.1f}"
        result[label] = 0
    for value in values:
        for low, high in BUCKETS:
            if low <= float(value) < high:
                result[f"{low:.1f}-{min(1.0, high):.1f}"] += 1
                break
    return result


def build_report(
    *,
    storage: Any,
    provider_status: dict[str, Any],
    counters: dict[str, Any],
    tunable: Any,
    umo: str = "",
    window_seconds: float = 7 * 86400,
    recent_limit: int = 50,
) -> dict[str, Any]:
    """一份完整报告：概览 / 分布 / 计数 / 最近流水。"""

    now = time.time()
    since = now - float(window_seconds)
    rows = storage.judgements_since(since)
    if umo:
        rows = [item for item in rows if item.get("umo") == umo]

    by_decision = {name: 0 for name in DECISIONS}
    for item in rows:
        key = str(item.get("decision") or "")
        if key in by_decision:
            by_decision[key] += 1

    willingness_values = [
        float((item.get("breakdown") or {}).get("willingness") or 0.0) for item in rows
    ]
    penalty_values = [
        float((item.get("breakdown") or {}).get("penalty") or 0.0) for item in rows
    ]
    candidates = [item for item in rows if not item.get("worth")]
    proactive_ready = [
        item
        for item in candidates
        if float((item.get("scores") or {}).get("rare_interject_score") or 0.0)
        >= float(tunable.rare_threshold)
    ]
    triggered = by_decision.get(PROACTIVE, 0)
    feedback_good = sum(1 for item in rows if item.get("feedback") == "good")
    feedback_bad = sum(1 for item in rows if item.get("feedback") == "bad")
    latest = rows[0] if rows else {}
    breakdown = latest.get("breakdown") or {}

    return {
        "generated_at": now,
        "window_seconds": window_seconds,
        "umo": umo,
        "storage": storage.stats(),
        "counts": {
            "judged": len(rows),
            "by_decision": by_decision,
            "feedback": {"good": feedback_good, "bad": feedback_bad},
        },
        "proactive": {
            "triggered": triggered,
            "candidates": len(proactive_ready),
            "hit_rate": round(triggered / len(proactive_ready), 4) if proactive_ready else 0.0,
            "false_trigger_hint": feedback_bad,
        },
        "willingness_dist": histogram(willingness_values),
        "density_penalty_dist": histogram(penalty_values),
        "latest": {
            "weighted_recent_W": breakdown.get("weighted_recent", 0.0),
            "normalized_load": breakdown.get("normalized_load", 0.0),
            "willingness": breakdown.get("willingness", 0.0),
        },
        "willingness_provider": provider_status,
        "counters": counters,
        "recent": rows[:recent_limit],
    }
