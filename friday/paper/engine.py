"""
FRIDAY paper trading engine (Phase 12A): opens and closes PAPER positions at
the prices FRIDAY itself recorded, under the Risk Governor.

PAPER MONEY ONLY. The engine has no network path and no way to place an
order anywhere. It moves numbers in the paper ledger.

What "a fill" means here, and why it is believable:
- A position buys one outcome's shares. To bet against an outcome, an order
  names the opposite outcome's token. There is no short selling.
- Entry is at FRIDAY's own last recorded ASK for that token (what a buyer
  really pays), and only if that quote is fresh and the spread is not wide.
  No fresh quote, no trade.
- Exit is at the recorded BID (what a seller really gets): when the bid falls
  to the stop, when the time limit passes, or at the target price when the
  bid reaches it. A market that has resolved is closed at its payoff, 1 or 0.
- With no fresh quote and no resolution, a position stays open and is counted
  as "waiting for a price". It is never closed at an invented price.
- A RESTING order (12B) is a buy that waits at a price below the ask. It is
  counted as filled only when a LATER recorded ask is strictly below its
  price: the market traded through it. Touching the price is not enough,
  because FRIDAY cannot know its place in the queue. This is the pessimistic
  rule: a resting order tends to fill exactly when the price is moving
  against it, and the paper record includes that.
- Every entry goes through `PaperLedger.open_checked`, so the governor's
  decision and the opening are one step. Exits never need the governor: a
  position can always be closed, kill switch or not.

The entry and exit spreads are recorded for every trade, so the paper record
measures the real cost a backtest could only assume.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PAPER_VERSION = "12C.1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_plans(
    position_id INTEGER PRIMARY KEY, token_id TEXT NOT NULL, market_id TEXT NOT NULL,
    entry_ask REAL NOT NULL, entry_bid REAL, entry_quote_at TEXT NOT NULL,
    stop_distance REAL NOT NULL, stop_price REAL NOT NULL, target_price REAL, expires_at TEXT NOT NULL,
    exit_bid REAL, exit_ask REAL, exit_quote_at TEXT, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_resting(
    order_id INTEGER PRIMARY KEY AUTOINCREMENT, placed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    strategy TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT NOT NULL, label TEXT,
    limit_price REAL NOT NULL, stake REAL NOT NULL, stop REAL NOT NULL, target REAL, max_hold_hours REAL NOT NULL,
    placed_bid REAL, placed_ask REAL,
    status TEXT NOT NULL,                     -- PENDING | FILLED | EXPIRED | REFUSED
    done_at TEXT, note TEXT, position_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_resting_status ON paper_resting(status);
CREATE TABLE IF NOT EXISTS paper_promotions(     -- 12C: the owner's decision to approve a forward test that succeeded
    strategy TEXT PRIMARY KEY, promoted_at TEXT NOT NULL, promoted_by TEXT, evidence TEXT NOT NULL,
    revoked_at TEXT, revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_refusals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, strategy TEXT, market_id TEXT, reason TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class PaperSettings:
    max_quote_age_minutes: float = 15.0     # an entry needs a quote this fresh
    max_spread: float = 0.05                # no entry across a spread wider than this
    min_price: float = 0.05                 # no entry in the tails
    max_price: float = 0.95
    exit_quote_age_minutes: float = 60.0    # an exit may use a quote up to this old

    def __post_init__(self):
        if not (0 < self.max_quote_age_minutes <= 240 and 0 < self.exit_quote_age_minutes <= 1440):
            raise ValueError("quote ages must be positive (entry up to 240 minutes, exit up to 1440)")
        if not (0 < self.max_spread <= 0.25 and 0 <= self.min_price < self.max_price <= 1):
            raise ValueError("max_spread must be in (0, 0.25] and 0 <= min_price < max_price <= 1")


@dataclass(frozen=True)
class Order:
    """What a strategy asks for. The engine and the governor decide whether and how much."""
    strategy: str
    market_id: str
    token_id: str                 # the outcome to BUY
    stake: float                  # USD the strategy would like at risk; the governor may cut it
    stop: float                   # price distance against the position (1R)
    target: float | None          # price distance in favour; None = no fixed target
    max_hold_hours: float
    label: str = ""


class PaperEngine:
    def __init__(self, governor, ledger, store, settings: PaperSettings | None = None,
                 resolver: Callable[[list[str]], list[dict[str, Any]]] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.governor, self.ledger, self.store = governor, ledger, store
        self.settings = settings or PaperSettings()
        self.resolver = resolver              # asks the venue whether markets have resolved (read-only)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = ledger.path
        self._lock = threading.Lock()
        self.last_tick: dict[str, Any] | None = None
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

    # ------------------------------------------------------------ quotes

    def _quote(self, token_id: str, max_age_minutes: float) -> dict[str, Any] | None:
        now = self.clock().isoformat(timespec="seconds")
        q = self.store.quote_near(token_id, now, max_age_minutes / 60.0)
        if not q:
            return None
        ok = lambda x: isinstance(x, (int, float)) and math.isfinite(x) and 0.0 <= x <= 1.0   # noqa: E731
        bid, ask = q.get("best_bid"), q.get("best_ask")
        return {"bid": bid if ok(bid) else None, "ask": ask if ok(ask) else None, "at": q["observed_at"]}

    def prices(self) -> dict[int, float]:
        """What each open position could be sold for now: the recorded bid. No bid, no entry here
        (the ledger then carries that position at cost)."""
        out = {}
        for p in self.ledger.positions("OPEN"):
            if p["instrument"]:
                q = self._quote(p["instrument"], self.settings.exit_quote_age_minutes)
                if q and q["bid"] is not None:
                    out[p["position_id"]] = q["bid"]
        return out

    # ------------------------------------------------------------ entries

    def _refuse(self, order: Order, reason: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._db() as db:
            db.execute("INSERT INTO paper_refusals(at, strategy, market_id, reason) VALUES(?,?,?,?)",
                       (self.clock().isoformat(timespec="seconds"), str(order.strategy)[:80], str(order.market_id)[:80],
                        reason[:200]))
        return {"status": "REFUSED", "reason": reason, "decision": decision, "position": None}

    def submit(self, order: Order) -> dict[str, Any]:
        s = self.settings
        finite = lambda x: isinstance(x, (int, float)) and math.isfinite(x)   # noqa: E731
        if not (str(order.strategy).strip() and str(order.market_id).strip() and str(order.token_id).strip()):
            return self._refuse(order, "the order must name a strategy, a market and a token")
        if not (finite(order.stake) and order.stake > 0 and finite(order.stop) and 0 < order.stop < 1
                and finite(order.max_hold_hours) and order.max_hold_hours > 0
                and (order.target is None or (finite(order.target) and 0 < order.target < 1))):
            return self._refuse(order, "stake, stop, target and holding time must be positive finite numbers")
        with self._lock:
            if any(p["instrument"] == order.token_id and p["strategy"] == order.strategy
                   for p in self.ledger.positions("OPEN")):
                return self._refuse(order, "this strategy already holds this outcome")
            q = self._quote(order.token_id, s.max_quote_age_minutes)
            if q is None or q["ask"] is None:
                return self._refuse(order, f"no ask recorded in the last {s.max_quote_age_minutes:g} minutes")
            ask, bid = q["ask"], q["bid"]
            if not s.min_price <= ask <= s.max_price:
                return self._refuse(order, f"ask {ask:.3f} is outside {s.min_price:g} to {s.max_price:g}")
            if bid is None or ask - bid > s.max_spread + 1e-9:
                return self._refuse(order, "no bid recorded" if bid is None else
                                    f"spread {ask - bid:.3f} is wider than {s.max_spread:g}")
            from friday.risk import LedgerError, Proposal
            try:
                decision, position = self.ledger.open_checked(
                    self.governor, Proposal(order.strategy, order.market_id, float(order.stake), venue="polymarket"),
                    ask, self.prices(), instrument=order.token_id, label=order.label)
            except LedgerError as exc:
                return self._refuse(order, f"ledger: {exc}")
            if position is None:
                return self._refuse(order, f"risk governor: {decision['reason']}", decision)
            now = self.clock()
            with self._db() as db:
                db.execute(
                    "INSERT INTO paper_plans(position_id, token_id, market_id, entry_ask, entry_bid, entry_quote_at,"
                    " stop_distance, stop_price, target_price, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (position["position_id"], order.token_id, str(order.market_id), ask, bid, q["at"], float(order.stop),
                     round(ask - order.stop, 6), None if order.target is None else round(min(1.0, ask + order.target), 6),
                     (now + timedelta(hours=float(order.max_hold_hours))).isoformat(timespec="seconds")))
        try:
            self.store.watch([str(order.market_id)])           # keep recording this market while it is held
        except Exception:
            pass
        return {"status": "OPENED", "reason": decision["reason"], "decision": decision, "position": position}

    # ------------------------------------------------------------ resting orders (12B)

    def rest(self, order: Order, limit_price: float, ttl_minutes: float = 60.0) -> dict[str, Any]:
        """Place a paper buy that waits at `limit_price`. Nothing is opened now; `tick` fills it only
        if the market later trades through the price, and then only if the governor agrees."""
        s = self.settings
        finite = lambda x: isinstance(x, (int, float)) and math.isfinite(x)   # noqa: E731
        if not (str(order.strategy).strip() and str(order.market_id).strip() and str(order.token_id).strip()):
            return self._refuse(order, "the order must name a strategy, a market and a token")
        if not (finite(order.stake) and order.stake > 0 and finite(order.stop) and 0 < order.stop < 1
                and finite(order.max_hold_hours) and order.max_hold_hours > 0 and finite(ttl_minutes) and 0 < ttl_minutes <= 1440
                and finite(limit_price) and (order.target is None or (finite(order.target) and 0 < order.target < 1))):
            return self._refuse(order, "price, stake, stop, target and times must be positive finite numbers")
        with self._lock:
            with self._db() as db:
                pending = db.execute("SELECT 1 FROM paper_resting WHERE status='PENDING' AND strategy=? AND token_id=?",
                                     (order.strategy, order.token_id)).fetchone()
            if pending or any(p["instrument"] == order.token_id and p["strategy"] == order.strategy
                              for p in self.ledger.positions("OPEN")):
                return self._refuse(order, "this strategy already holds or is waiting on this outcome")
            q = self._quote(order.token_id, s.max_quote_age_minutes)
            if q is None or q["ask"] is None or q["bid"] is None:
                return self._refuse(order, f"no two-sided quote recorded in the last {s.max_quote_age_minutes:g} minutes")
            if not s.min_price <= limit_price <= s.max_price:
                return self._refuse(order, f"price {limit_price:.3f} is outside {s.min_price:g} to {s.max_price:g}")
            if limit_price >= q["ask"] - 1e-9:
                return self._refuse(order, "a resting order must be below the ask (at or above it, it is not resting)")
            if q["ask"] - q["bid"] > s.max_spread + 1e-9:
                return self._refuse(order, f"spread {q['ask'] - q['bid']:.3f} is wider than {s.max_spread:g}")
            from friday.risk import Proposal
            dry = self.governor.evaluate(Proposal(order.strategy, order.market_id, float(order.stake), venue="polymarket"),
                                         self.ledger.portfolio(self.prices()), record=False)
            if dry["verdict"] == "REJECTED":
                return self._refuse(order, f"risk governor: {dry['reason']}", dry)
            now = self.clock()
            with self._db() as db:
                cur = db.execute(
                    "INSERT INTO paper_resting(placed_at, expires_at, strategy, market_id, token_id, label, limit_price, stake,"
                    " stop, target, max_hold_hours, placed_bid, placed_ask, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING')",
                    (now.isoformat(timespec="seconds"), (now + timedelta(minutes=float(ttl_minutes))).isoformat(timespec="seconds"),
                     str(order.strategy)[:80], str(order.market_id)[:80], str(order.token_id)[:120], str(order.label)[:200],
                     round(float(limit_price), 6), float(order.stake), float(order.stop), order.target,
                     float(order.max_hold_hours), q["bid"], q["ask"]))
                order_id = cur.lastrowid
        try:
            self.store.watch([str(order.market_id)])
        except Exception:
            pass
        return {"status": "RESTING", "order_id": order_id, "limit_price": round(float(limit_price), 6), "reason": "waiting"}

    def resting(self, status: str | None = "PENDING", limit: int = 200) -> list[dict[str, Any]]:
        where, args = ("WHERE status=?", [status]) if status else ("", [])
        with self._db() as db:
            return [dict(r) for r in db.execute(
                f"SELECT * FROM paper_resting {where} ORDER BY order_id DESC LIMIT ?", (*args, max(1, min(int(limit), 2000))))]

    def _work_resting(self, now: datetime, summary: dict[str, Any]) -> None:
        """Fill, refuse or expire each waiting order. Called inside `tick`, under the lock."""
        stamp = now.isoformat(timespec="seconds")

        def done(order_id, status, note, position_id=None):
            with self._db() as db:
                db.execute("UPDATE paper_resting SET status=?, done_at=?, note=?, position_id=? WHERE order_id=?",
                           (status, stamp, str(note)[:200], position_id, order_id))
        from friday.risk import LedgerError, Proposal
        for o in reversed(self.resting("PENDING", 2000)):
            q = self._quote(o["token_id"], self.settings.exit_quote_age_minutes)
            fresh = q is not None and q["ask"] is not None and q["at"] > o["placed_at"]
            if fresh and q["ask"] < o["limit_price"] - 1e-9:                 # traded through: filled at our price
                if q["bid"] is None:
                    q["bid"] = q["ask"]
                try:
                    decision, position = self.ledger.open_checked(
                        self.governor, Proposal(o["strategy"], o["market_id"], o["stake"], venue="polymarket"),
                        o["limit_price"], self.prices(), instrument=o["token_id"], label=o["label"] or "")
                except LedgerError as exc:
                    done(o["order_id"], "REFUSED", f"ledger: {exc}")
                    continue
                if position is None:
                    done(o["order_id"], "REFUSED", f"risk governor: {decision['reason']}")
                    continue
                L = o["limit_price"]
                with self._db() as db:
                    db.execute(
                        "INSERT INTO paper_plans(position_id, token_id, market_id, entry_ask, entry_bid, entry_quote_at,"
                        " stop_distance, stop_price, target_price, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (position["position_id"], o["token_id"], o["market_id"], L, L, q["at"], o["stop"], round(L - o["stop"], 6),
                         None if o["target"] is None else round(min(1.0, L + o["target"]), 6),
                         (now + timedelta(hours=o["max_hold_hours"])).isoformat(timespec="seconds")))
                done(o["order_id"], "FILLED", f"ask {q['ask']:.3f} traded through {L:.3f}", position["position_id"])
                summary["filled"].append({"order_id": o["order_id"], "strategy": o["strategy"], "price": L,
                                          "position_id": position["position_id"]})
            elif stamp >= o["expires_at"]:
                done(o["order_id"], "EXPIRED", "not reached in time")
                summary["expired"] += 1

    # ------------------------------------------------------------ exits

    def _plans(self) -> dict[int, dict[str, Any]]:
        with self._db() as db:
            return {r["position_id"]: dict(r) for r in db.execute(
                "SELECT * FROM paper_plans WHERE exit_reason IS NULL").fetchall()}

    def _close(self, position: dict[str, Any], price: float, reason: str, quote: dict[str, Any] | None) -> dict[str, Any]:
        closed = self.ledger.close(position["position_id"], price, reason)
        with self._db() as db:
            db.execute("UPDATE paper_plans SET exit_bid=?, exit_ask=?, exit_quote_at=?, exit_reason=? WHERE position_id=?",
                       (quote["bid"] if quote else None, quote["ask"] if quote else None, quote["at"] if quote else None,
                        reason, position["position_id"]))
        return closed

    def tick(self, *_: Any) -> dict[str, Any]:
        """Look at every open paper position once and close those whose exit has come. Called after
        each stored market scan; safe to call at any time."""
        with self._lock:
            now = self.clock()
            plans = self._plans()
            open_positions = [p for p in self.ledger.positions("OPEN") if p["position_id"] in plans]
            summary = {"at": now.isoformat(timespec="seconds"), "open": len(open_positions), "closed": [],
                       "waiting_for_a_price": 0, "errors": [], "filled": [], "expired": 0}
            try:                                               # positions filled now are looked at on the next tick
                self._work_resting(now, summary)
            except Exception as exc:
                summary["errors"].append(f"resting: {type(exc).__name__}: {str(exc)[:100]}")
            if not open_positions:
                self.last_tick = summary
                return summary
            markets = sorted({str(p["market_id"]) for p in open_positions})
            try:
                self.store.watch(markets)
            except Exception as exc:
                summary["errors"].append(f"watch: {type(exc).__name__}")
            resolved = self.store.resolutions(markets)
            for p in open_positions:
                plan = plans[p["position_id"]]
                q = self._quote(plan["token_id"], self.settings.exit_quote_age_minutes)
                res = resolved.get(str(p["market_id"]))
                try:
                    if res and plan["token_id"] in res["payoffs"]:
                        done = self._close(p, float(res["payoffs"][plan["token_id"]]), "resolved", q)
                    elif q is None or q["bid"] is None:
                        summary["waiting_for_a_price"] += 1
                        continue
                    elif q["bid"] <= plan["stop_price"] + 1e-9:
                        done = self._close(p, q["bid"], "stop", q)
                    elif plan["target_price"] is not None and q["bid"] >= plan["target_price"] - 1e-9:
                        done = self._close(p, plan["target_price"], "target", q)
                    elif now.isoformat(timespec="seconds") >= plan["expires_at"]:
                        done = self._close(p, q["bid"], "time", q)
                    else:
                        continue
                except Exception as exc:                       # one bad position must not stop the others
                    summary["errors"].append(f"position {p['position_id']}: {type(exc).__name__}: {str(exc)[:100]}")
                    continue
                summary["closed"].append({"position_id": done["position_id"], "strategy": done["strategy"],
                                          "reason": done["close_reason"], "pnl": round(done["pnl"], 4)})
            if summary["waiting_for_a_price"] and self.resolver is not None:
                waiting = sorted({str(p["market_id"]) for p in open_positions
                                  if str(p["market_id"]) not in resolved})
                try:                                           # ask once; any answer is used on the next tick
                    for r in self.resolver(waiting[:20]):
                        if r.get("resolved") and r.get("payoffs"):
                            self.store.record_resolution(r["market_id"], r["payoffs"], "gamma", now)
                except Exception as exc:
                    summary["errors"].append(f"resolver: {type(exc).__name__}: {str(exc)[:100]}")
            self.last_tick = summary
            return summary

    # ------------------------------------------------------------ promotions (12C)

    def promotion(self, strategy: str) -> dict[str, Any] | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM paper_promotions WHERE strategy=?", (strategy,)).fetchone()
        return dict(r) if r else None

    def record_promotion(self, strategy: str, by: str, evidence: str) -> dict[str, Any]:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO paper_promotions(strategy, promoted_at, promoted_by, evidence, revoked_at,"
                       " revoke_reason) VALUES(?,?,?,?,NULL,NULL)",
                       (strategy, self.clock().isoformat(timespec="seconds"), str(by)[:60], evidence[:2000]))
        return self.promotion(strategy)

    def revoke_promotion(self, strategy: str, reason: str) -> dict[str, Any] | None:
        with self._db() as db:
            db.execute("UPDATE paper_promotions SET revoked_at=?, revoke_reason=? WHERE strategy=? AND revoked_at IS NULL",
                       (self.clock().isoformat(timespec="seconds"), str(reason)[:300], strategy))
        return self.promotion(strategy)

    # ------------------------------------------------------------ record

    def trades(self, limit: int = 500) -> list[dict[str, Any]]:
        """Closed paper trades, newest first, each with the spreads it really paid and its result in R."""
        with self._db() as db:
            plans = {r["position_id"]: dict(r) for r in db.execute("SELECT * FROM paper_plans").fetchall()}
        out = []
        for p in self.ledger.positions("CLOSED", limit):
            plan = plans.get(p["position_id"])
            if not plan:
                continue
            risk = p["units"] * plan["stop_distance"]
            out.append({**p, "entry_spread": None if plan["entry_bid"] is None else round(plan["entry_ask"] - plan["entry_bid"], 4),
                        "exit_spread": None if plan["exit_ask"] is None or plan["exit_bid"] is None
                        else round(plan["exit_ask"] - plan["exit_bid"], 4),
                        "rested": plan["entry_bid"] is not None and plan["entry_bid"] == plan["entry_ask"],
                        "r": round(p["pnl"] / risk, 3) if risk > 0 else None, "per_share": round(p["exit_price"] - p["entry_price"], 4)})
        return out

    def report(self) -> dict[str, Any]:
        """The paper record by strategy. This, not a backtest, is the evidence Phase 12 exists to gather."""
        by: dict[str, list[dict[str, Any]]] = {}
        for t in self.trades(2000):
            by.setdefault(t["strategy"], []).append(t)
        rows = []
        for name, ts in sorted(by.items()):
            n = len(ts)
            spreads = [t["entry_spread"] for t in ts if t["entry_spread"] is not None]
            rs = [t["r"] for t in ts if t["r"] is not None]
            reasons: dict[str, int] = {}
            for t in ts:
                reasons[t["close_reason"]] = reasons.get(t["close_reason"], 0) + 1
            rows.append({"strategy": name, "trades": n, "wins": sum(1 for t in ts if t["pnl"] > 0),
                         "pnl": round(sum(t["pnl"] for t in ts), 2), "staked": round(sum(t["stake"] for t in ts), 2),
                         "avg_per_share": round(sum(t["per_share"] for t in ts) / n, 4),
                         "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                         "avg_entry_spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
                         "exits": reasons, "days": len({str(t["closed_at"])[:10] for t in ts})})
        with self._db() as db:
            refused = [dict(r) for r in db.execute(
                "SELECT reason, COUNT(*) n FROM paper_refusals GROUP BY reason ORDER BY n DESC LIMIT 12").fetchall()]
            rest = {r["strategy"]: dict(r) for r in db.execute(
                "SELECT strategy, COUNT(*) placed, SUM(status='FILLED') filled, SUM(status='EXPIRED') expired,"
                " SUM(status='PENDING') pending, SUM(status='REFUSED') refused FROM paper_resting GROUP BY strategy").fetchall()}
        for row in rows:
            row["resting"] = rest.pop(row["strategy"], None)
        for name, r in sorted(rest.items()):                   # strategies with orders but no closed trade yet
            rows.append({"strategy": name, "trades": 0, "wins": 0, "pnl": 0.0, "staked": 0.0, "avg_per_share": None,
                         "avg_r": None, "avg_entry_spread": None, "exits": {}, "days": 0, "resting": r})
        return {"paper_version": PAPER_VERSION, "mode": "PAPER", "strategies": rows,
                "open": len(self.ledger.positions("OPEN")), "closed": sum(r["trades"] for r in rows),
                "refused": refused, "last_tick": self.last_tick, "settings": self.settings.__dict__,
                "live_trading": "LOCKED", "execution_locked": True}
