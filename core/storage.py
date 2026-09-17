"""sqlite 存储：她说过的话 + 每次判定流水。

两张表就够：

- ``speech``：发送层钩子写进来的「她开口了」，只记 umo / 时间；
- ``judgement``：每条进入路由的消息的完整判定（分数、概率分解、最终决策），
  看板靠它出分布和流水，误判标记也记在这里。

数据量很小（一条消息一行），同步写足够快；这里刻意不引入线程池，
避免在事件循环里再多一层调度。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS speech (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    ts REAL NOT NULL,
    trigger_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_speech_umo_ts ON speech(umo, ts);

CREATE TABLE IF NOT EXISTS judgement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    ts REAL NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    worth INTEGER NOT NULL DEFAULT 0,
    scores TEXT NOT NULL DEFAULT '{}',
    decision TEXT NOT NULL DEFAULT '',
    probability REAL NOT NULL DEFAULT 0,
    breakdown TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    feedback TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_judgement_umo_ts ON judgement(umo, ts);
"""


class Storage:
    """sqlite 封装（线程安全：一把锁 + check_same_thread=False）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------------- 写 ----------------

    def record_speech(self, umo: str, ts: float | None = None, trigger_id: str = "") -> None:
        """记一次「她开口了」。发送层钩子调用；不区分来源（其他插件不会配合我们）。"""

        with self._lock:
            self._conn.execute(
                "INSERT INTO speech (umo, ts, trigger_id) VALUES (?, ?, ?)",
                (str(umo), float(ts if ts is not None else time.time()), str(trigger_id or "")),
            )
            self._conn.commit()

    def record_judgement(
        self,
        *,
        umo: str,
        ts: float,
        sender: str,
        text: str,
        worth: bool,
        scores: dict[str, Any],
        decision: str,
        probability: float,
        breakdown: dict[str, Any],
        reason: str,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO judgement "
                "(umo, ts, sender, text, worth, scores, decision, probability, breakdown, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(umo),
                    float(ts),
                    str(sender or ""),
                    str(text or "")[:500],
                    1 if worth else 0,
                    json.dumps(scores or {}, ensure_ascii=False),
                    str(decision or ""),
                    float(probability),
                    json.dumps(breakdown or {}, ensure_ascii=False),
                    str(reason or "")[:200],
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def set_feedback(self, judgement_id: int, feedback: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE judgement SET feedback = ? WHERE id = ?",
                (str(feedback or ""), int(judgement_id)),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    # ---------------- 读 ----------------

    def speech_since(self, umo: str, since: float, limit: int = 5000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, trigger_id FROM speech WHERE umo = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (str(umo), float(since), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_speech_ts(self, umo: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM speech WHERE umo = ? ORDER BY ts DESC LIMIT 1", (str(umo),)
            ).fetchone()
        return float(row["ts"]) if row else 0.0

    def user_msg_since(
        self, umo: str, since: float, limit: int = 5000
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM judgement WHERE umo = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (str(umo), float(since), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_judgements(
        self, *, umo: str = "", limit: int = 50, since: float = 0.0
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM judgement WHERE ts >= ?"
        params: list[Any] = [float(since)]
        if umo:
            query += " AND umo = ?"
            params.append(str(umo))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_judgement(row) for row in rows]

    def judgements_since(self, since: float, limit: int = 20000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM judgement WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (float(since), int(limit)),
            ).fetchall()
        return [self._row_to_judgement(row) for row in rows]

    def counts_by_umo(self, since: float) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT umo, COUNT(*) AS n FROM judgement WHERE ts >= ? GROUP BY umo",
                (float(since),),
            ).fetchall()
        return {str(row["umo"]): int(row["n"]) for row in rows}

    @staticmethod
    def _row_to_judgement(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["worth"] = bool(item.get("worth"))
        for key in ("scores", "breakdown"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except (TypeError, ValueError):
                item[key] = {}
        return item

    # ---------------- 维护 ----------------

    def prune(self, keep_days: int) -> int:
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        removed = 0
        with self._lock:
            for table in ("speech", "judgement"):
                cursor = self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                removed += int(cursor.rowcount or 0)
            self._conn.commit()
        return removed

    def clear(self, tables: Iterable[str] = ("speech", "judgement")) -> None:
        with self._lock:
            for table in tables:
                if table in ("speech", "judgement"):
                    self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            speech = int(self._conn.execute("SELECT COUNT(*) FROM speech").fetchone()[0])
            judge = int(self._conn.execute("SELECT COUNT(*) FROM judgement").fetchone()[0])
        return {"speech": speech, "judgement": judge}
