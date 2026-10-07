"""
FRIDAY Risk state (Phase 10A): the kill switch, the drawdown halt and the
decision log, in a small SQLite file of their own (JARVIS_RISK_DB, default
~/jarvis/data/risk.db) so they survive a restart and do not depend on any
market's data store.

Engaging the kill switch needs nothing. Releasing it — or clearing a
drawdown halt — needs the literal confirmation "RESUME": stopping is easy,
starting again is deliberate.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_RISK_DB = "~/jarvis/data/risk.db"
RESUME_WORD = "RESUME"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decisions(
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, strategy TEXT, market_id TEXT, mode TEXT, verdict TEXT NOT NULL, reason TEXT,
    requested_stake REAL, approved_stake REAL, decision TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_at ON risk_decisions(at);
"""


class RiskState:
    def __init__(self, path: str | Path = DEFAULT_RISK_DB):
        self.path = Path(str(path)).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _get(self, key: str) -> dict[str, Any] | None:
        with self._db() as db:
            r = db.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def _set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO risk_state(key, value) VALUES(?,?)", (key, json.dumps(value)))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------ kill switch

    def kill_switch(self) -> dict[str, Any]:
        return self._get("kill_switch") or {"engaged": False}

    def engage_kill_switch(self, reason: str = "", by: str = "operator") -> dict[str, Any]:
        self._set("kill_switch", {"engaged": True, "reason": (reason or "engaged by " + by)[:300], "by": by[:60],
                                  "at": self._now()})
        return self.kill_switch()

    # ------------------------------------------------------------ drawdown halt

    def drawdown_halt(self) -> dict[str, Any]:
        return self._get("drawdown_halt") or {"engaged": False}

    def set_drawdown_halt(self, reason: str) -> None:
        self._set("drawdown_halt", {"engaged": True, "reason": reason[:300], "at": self._now()})

    def take_peak_reset(self) -> bool:
        """True once after a drawdown halt was cleared: the ledger then restarts its peak at current equity.
        Without this a resumed account would halt again on the very next proposal."""
        if not (self._get("peak_reset") or {}).get("pending"):
            return False
        self._set("peak_reset", {"pending": False, "taken_at": self._now()})
        return True

    def resume(self, confirm: str, by: str = "owner") -> dict[str, Any]:
        """Release the kill switch and clear a drawdown halt. Needs the literal word RESUME."""
        if confirm != RESUME_WORD:
            raise PermissionError(f'resuming needs confirm="{RESUME_WORD}"')
        cleared = {"kill_switch": self.kill_switch()["engaged"], "drawdown_halt": self.drawdown_halt()["engaged"]}
        self._set("kill_switch", {"engaged": False, "released_by": by[:60], "released_at": self._now()})
        self._set("drawdown_halt", {"engaged": False, "cleared_by": by[:60], "cleared_at": self._now()})
        if cleared["drawdown_halt"]:          # 10C: the owner accepts the loss; drawdown is measured from here on
            self._set("peak_reset", {"pending": True, "by": by[:60], "at": self._now()})
        return {"resumed": True, "cleared": cleared}

    # ------------------------------------------------------------ decisions

    def record_decision(self, decision: dict[str, Any]) -> int:
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT INTO risk_decisions(at, strategy, market_id, mode, verdict, reason, requested_stake,"
                " approved_stake, decision) VALUES(?,?,?,?,?,?,?,?,?)",
                (decision["at"], str(decision.get("strategy"))[:80], str(decision.get("market_id"))[:80],
                 str(decision.get("mode"))[:10], decision["verdict"], str(decision.get("reason"))[:200],
                 decision.get("requested_stake"), decision.get("approved_stake"),
                 json.dumps(decision, default=str)[:8000]))
            return cur.lastrowid

    def decisions(self, limit: int = 100, verdict: str | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE verdict=?", [verdict]) if verdict else ("", [])
        with self._db() as db:
            rows = db.execute(f"SELECT decision_id, decision FROM risk_decisions {where} ORDER BY decision_id DESC"
                              " LIMIT ?", (*args, max(1, min(int(limit), 1000)))).fetchall()
        return [{**json.loads(r["decision"]), "decision_id": r["decision_id"]} for r in rows]

    def decision_counts(self, days: float = 30) -> dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(days=float(days))).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute("SELECT verdict, COUNT(*) n FROM risk_decisions WHERE at>=? GROUP BY verdict",
                              (since,)).fetchall()
        return {r["verdict"]: r["n"] for r in rows}
