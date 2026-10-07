"""
FRIDAY paper ledger (Phase 10B): the accounting the Risk Governor reads.

PAPER MONEY ONLY. The ledger holds a notional bankroll and the positions a
strategy opened with it, so the governor's limits act on real numbers:
equity, its peak, the day's starting equity and what is at risk where.

- A position can only be opened from a governor decision that is APPROVED
  or REDUCED, in PAPER mode, for that decision's approved stake. There is
  no other way in: no decision, no position.
- A position is a number of units bought at a price (for a $1-payoff share,
  the price is between 0 and 1): units = stake / price. Its value is
  units x the current price; without a current price it is carried at cost.
- Closing realises units x exit price. Cash can never go below zero.
- Equity = cash + the value of open positions. The peak and the day's
  starting equity are tracked here (UTC days) and survive a restart.

The ledger places nothing anywhere. Phase 12's paper engine is its only
intended caller; it has no network path.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .governor import APPROVED, REDUCED, Portfolio, Position
from .state import DEFAULT_RISK_DB, RiskState

LEDGER_VERSION = "10C.1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_positions(
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL, strategy TEXT NOT NULL, market_id TEXT NOT NULL, instrument TEXT, venue TEXT,
    label TEXT, stake REAL NOT NULL, entry_price REAL NOT NULL, units REAL NOT NULL, decision_id INTEGER,
    status TEXT NOT NULL,                     -- OPEN | CLOSED
    closed_at TEXT, exit_price REAL, proceeds REAL, pnl REAL, close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions(status);
CREATE TABLE IF NOT EXISTS equity_days(day TEXT PRIMARY KEY, start_equity REAL NOT NULL, end_equity REAL NOT NULL);
"""


class LedgerError(Exception):
    """A request the ledger refuses (no approved decision, bad numbers, not enough cash)."""


class PaperLedger:
    def __init__(self, path: str | Path = DEFAULT_RISK_DB, bankroll: float = 1000.0,
                 clock: Callable[[], datetime] | None = None):
        if not (isinstance(bankroll, (int, float)) and math.isfinite(bankroll) and bankroll > 0):
            raise ValueError("the paper bankroll must be a positive number")
        self.path = Path(str(path)).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._state = RiskState(self.path)     # same file: tells the ledger when a drawdown halt was cleared
        self._entry = threading.Lock()         # one evaluate-then-open at a time (10C)
        with self._db() as db:
            db.executescript(_SCHEMA)
            if db.execute("SELECT 1 FROM ledger_state WHERE key='cash'").fetchone() is None:
                now = self.clock().isoformat(timespec="seconds")
                for k, v in (("cash", bankroll), ("bankroll", bankroll), ("peak_equity", bankroll),
                             ("day", now[:10]), ("day_start_equity", bankroll), ("started_at", now)):
                    db.execute("INSERT INTO ledger_state(key, value) VALUES(?,?)", (k, json.dumps(v)))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _get(db, key):
        r = db.execute("SELECT value FROM ledger_state WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT OR REPLACE INTO ledger_state(key, value) VALUES(?,?)", (key, json.dumps(value)))

    # ------------------------------------------------------------ positions

    def open(self, decision: dict[str, Any], price: float, instrument: str = "", label: str = "") -> dict[str, Any]:
        """Open a PAPER position for exactly the stake the governor approved."""
        if decision.get("verdict") not in (APPROVED, REDUCED):
            raise LedgerError(f"the governor did not approve this ({decision.get('verdict')}: {decision.get('reason')})")
        if str(decision.get("mode", "")).upper() != "PAPER":
            raise LedgerError("the ledger holds paper positions only")
        if decision.get("decision_id") is None:
            raise LedgerError("a dry-run decision cannot open a position (it was not recorded)")
        stake = decision.get("approved_stake")
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (stake, price)):
            raise LedgerError("stake and price must be positive finite numbers")
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM paper_positions WHERE decision_id=?", (decision["decision_id"],)).fetchone():
                raise LedgerError("this decision has already been used to open a position")
            cash = self._get(db, "cash")
            if stake > cash + 1e-9:
                raise LedgerError(f"not enough paper cash (${cash:.2f}) for a ${stake:.2f} stake")
            now = self.clock().isoformat(timespec="seconds")
            cur = db.execute(
                "INSERT INTO paper_positions(opened_at, strategy, market_id, instrument, venue, label, stake,"
                " entry_price, units, decision_id, status) VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN')",
                (now, str(decision["strategy"])[:80], str(decision["market_id"])[:80], str(instrument)[:120],
                 str(decision.get("venue") or "")[:40], str(label)[:200], float(stake), float(price),
                 float(stake) / float(price), int(decision["decision_id"])))
            self._set(db, "cash", round(cash - stake, 6))
            return self._row(db, cur.lastrowid)

    def open_checked(self, governor, proposal, price: float, prices: dict[int, float] | None = None,
                     instrument: str = "", label: str = "") -> tuple[dict[str, Any], dict[str, Any] | None]:
        """10C: the one safe way in. The governor's decision and the opening happen under a single
        lock, so two proposals arriving together cannot both be sized against the same room.
        Returns (decision, position); the position is None when the governor refused."""
        with self._entry:
            decision = governor.evaluate(proposal, self.portfolio(prices))
            if decision["verdict"] not in (APPROVED, REDUCED):
                return decision, None
            return decision, self.open(decision, price, instrument, label)

    def close(self, position_id: int, exit_price: float, reason: str = "") -> dict[str, Any]:
        if not (isinstance(exit_price, (int, float)) and math.isfinite(exit_price) and exit_price >= 0):
            raise LedgerError("the exit price must be a finite number >= 0")
        with self._lock, self._db() as db:
            r = db.execute("SELECT * FROM paper_positions WHERE position_id=?", (int(position_id),)).fetchone()
            if r is None or r["status"] != "OPEN":
                raise LedgerError("no open position with that id")
            proceeds = r["units"] * float(exit_price)
            db.execute("UPDATE paper_positions SET status='CLOSED', closed_at=?, exit_price=?, proceeds=?, pnl=?,"
                       " close_reason=? WHERE position_id=?",
                       (self.clock().isoformat(timespec="seconds"), float(exit_price), round(proceeds, 6),
                        round(proceeds - r["stake"], 6), str(reason)[:200], int(position_id)))
            self._set(db, "cash", round(self._get(db, "cash") + proceeds, 6))
            return self._row(db, int(position_id))

    @staticmethod
    def _row(db, position_id: int) -> dict[str, Any]:
        return dict(db.execute("SELECT * FROM paper_positions WHERE position_id=?", (position_id,)).fetchone())

    def positions(self, status: str | None = "OPEN", limit: int = 200) -> list[dict[str, Any]]:
        where, args = ("WHERE status=?", [status]) if status else ("", [])
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM paper_positions {where} ORDER BY position_id DESC LIMIT ?",
                              (*args, max(1, min(int(limit), 2000)))).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ valuation

    def portfolio(self, prices: dict[int, float] | None = None) -> Portfolio:
        """The governor's view. `prices` maps position_id -> current price; a position without one
        is carried at cost. Updates the peak and rolls the day (UTC)."""
        prices = prices or {}
        reset = self._state.take_peak_reset()  # the owner resumed after a drawdown halt
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM paper_positions WHERE status='OPEN'").fetchall()
            cash = self._get(db, "cash")
            value = 0.0
            for r in rows:
                p = prices.get(r["position_id"])
                ok = isinstance(p, (int, float)) and math.isfinite(p) and p >= 0
                value += r["units"] * p if ok else r["stake"]
            equity = round(cash + value, 6)
            peak = equity if reset else max(self._get(db, "peak_equity"), equity)
            self._set(db, "peak_equity", peak)
            today = self.clock().strftime("%Y-%m-%d")
            if self._get(db, "day") != today:
                self._set(db, "day", today)
                self._set(db, "day_start_equity", equity)
            day_start = self._get(db, "day_start_equity")
            db.execute("INSERT INTO equity_days(day, start_equity, end_equity) VALUES(?,?,?)"
                       " ON CONFLICT(day) DO UPDATE SET end_equity=excluded.end_equity", (today, day_start, equity))
        return Portfolio(equity=equity, peak_equity=peak, day_start_equity=day_start,
                         positions=tuple(Position(r["market_id"], r["strategy"], r["stake"]) for r in rows))

    def summary(self, prices: dict[int, float] | None = None) -> dict[str, Any]:
        pf = self.portfolio(prices)
        with self._db() as db:
            bankroll, cash = self._get(db, "bankroll"), self._get(db, "cash")
            started = self._get(db, "started_at")
            closed = db.execute("SELECT pnl FROM paper_positions WHERE status='CLOSED'").fetchall()
            days = [dict(r) for r in db.execute("SELECT * FROM equity_days ORDER BY day DESC LIMIT 90").fetchall()]
        pnls = [r["pnl"] for r in closed]
        peak, worst = bankroll, 0.0
        for d in reversed(days):
            peak = max(peak, d["end_equity"])
            worst = max(worst, (peak - d["end_equity"]) / peak if peak else 0.0)
        return {"ledger_version": LEDGER_VERSION, "mode": "PAPER", "bankroll": bankroll, "started_at": started,
                "equity": round(pf.equity, 2), "cash": round(cash, 2), "at_risk": round(pf.exposure, 2),
                "total_pnl": round(pf.equity - bankroll, 2),
                "total_return": round((pf.equity - bankroll) / bankroll, 4),
                "daily_pnl": round(pf.equity - pf.day_start_equity, 2), "peak_equity": round(pf.peak_equity, 2),
                "drawdown": round((pf.peak_equity - pf.equity) / pf.peak_equity, 4) if pf.peak_equity else 0.0,
                "max_drawdown": round(max(worst, (pf.peak_equity - pf.equity) / pf.peak_equity), 4),
                "open_positions": len(pf.positions), "closed_trades": len(pnls),
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else None,
                "best_trade": round(max(pnls), 2) if pnls else None,
                "worst_trade": round(min(pnls), 2) if pnls else None,
                "equity_days": list(reversed(days))}
