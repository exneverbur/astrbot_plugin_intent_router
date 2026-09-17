"""安全层：独立于路由模型的规则检查。

路由模型给出的 ``risk`` 只是辅助信号——真正"拦不拦"由这里说了算，
这样模型被群聊内容带偏时也不会把危险内容放过去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

BLOCKED = "blocked"
FLAGGED = "flagged"
OK = "ok"


@dataclass
class SafetyResult:
    level: str = OK
    reason: str = ""
    hits: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.level == BLOCKED

    @property
    def flagged(self) -> bool:
        return self.level == FLAGGED

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "reason": self.reason, "hits": list(self.hits)}


class SafetyLayer:
    """黑名单词 + 正则 + 几条基本规则。"""

    def __init__(
        self,
        *,
        block_words: list[str] | None = None,
        flag_words: list[str] | None = None,
        block_regex: list[str] | None = None,
        max_length: int = 2000,
    ) -> None:
        self.block_words = [str(w) for w in (block_words or []) if str(w).strip()]
        self.flag_words = [str(w) for w in (flag_words or []) if str(w).strip()]
        self.max_length = max(200, int(max_length))
        self._patterns = []
        for pattern in block_regex or []:
            try:
                self._patterns.append(re.compile(str(pattern)))
            except re.error:
                continue

    def check(self, text: str, *, risk: float = 0.0) -> SafetyResult:
        body = str(text or "")
        hits: list[str] = []
        lowered = body.lower()
        for word in self.block_words:
            if word.lower() in lowered:
                return SafetyResult(BLOCKED, f"命中屏蔽词「{word}」", [word])
        for pattern in self._patterns:
            if pattern.search(body):
                return SafetyResult(BLOCKED, f"命中屏蔽规则 {pattern.pattern}", [pattern.pattern])
        if len(body) > self.max_length:
            return SafetyResult(BLOCKED, "消息过长", ["length"])
        for word in self.flag_words:
            if word.lower() in lowered:
                hits.append(word)
        if risk >= 0.8:
            hits.append("model_risk")
        if hits:
            return SafetyResult(FLAGGED, "需要人工过目", hits)
        return SafetyResult()
