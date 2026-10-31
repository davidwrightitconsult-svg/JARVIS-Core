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

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .costs import CostSettings, cost_of, round_trip
from .fills import buy_for_stake, capacity, sell_shares

PAPER_VERSION = "14B.1"

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
CREATE TABLE IF NOT EXISTS paper_settlements(     -- 13B: every pre-resolution exit, and what holding would have paid
    position_id INTEGER PRIMARY KEY, token_id TEXT NOT NULL, market_id TEXT NOT NULL,
    settled_at TEXT NOT NULL, market_end TEXT NOT NULL, exit_bid REAL NOT NULL, units REAL NOT NULL,
    proceeds REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_signal_orders(  -- 13C: one paper order per quant signal, ever
    signal_id INTEGER PRIMARY KEY, at TEXT NOT NULL, strategy TEXT NOT NULL, market_id TEXT NOT NULL,
    token_id TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL, position_id INTEGER, note TEXT
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
    # 14B COSTS: the taker fee folded into every booked price, so that drawdown, the scorecard and
    # the validation gate all see net-of-fee numbers without each having to know about fees.
    costs: CostSettings = dc_field(default_factory=CostSettings)
    # 18B: refuse a market where getting in and out again costs more than this share of the capital
    # committed. At mid prices a 2c spread plus a 0.05 fee is ~9%; a 6c spread is ~17%. An edge has
    # to beat this before it is an edge, so a market above the limit is not worth the attempt.
    # None turns the check off. See `jarvis.paper.costs.round_trip`.
    max_round_trip_fraction: float | None = 0.15
    # 13B SETTLEMENT: close this many minutes before the market's end date, rather than holding into
    # resolution, where the book thins out and the spread widens (the liquidity trap). 0 = hold.
    settle_before_minutes: float = 30.0
    # A position whose bid is already this high is left to resolve: selling would pay more in spread
    # than holding could still gain. None = always exit. This is a cost rule, not a view on the outcome.
    settle_hold_above: float | None = 0.97
    # 14A DEPTH: fill by walking the recorded book instead of taking any size at the best price.
    depth_aware: bool = True
    # How far past the best price we will walk on an ENTRY. Levels beyond it are left alone, exactly
    # as a limit order would, so a fill may be partial.
    max_slippage: float = 0.02
    # A book that cannot absorb at least this much is not worth entering at all.
    min_depth_usd: float = 5.0

    def __post_init__(self):
        if not (0 < self.max_quote_age_minutes <= 240 and 0 < self.exit_quote_age_minutes <= 1440):
            raise ValueError("quote ages must be positive (entry up to 240 minutes, exit up to 1440)")
        if not (0 < self.max_spread <= 0.25 and 0 <= self.min_price < self.max_price <= 1):
            raise ValueError("max_spread must be in (0, 0.25] and 0 <= min_price < max_price <= 1")
        if not (0 <= self.settle_before_minutes <= 1440):
            raise ValueError("settle_before_minutes must be between 0 (hold) and 1440")
        if self.settle_hold_above is not None and not (0.5 < self.settle_hold_above <= 1):
            raise ValueError("settle_hold_above must be in (0.5, 1] or None")
        if not 0 < self.max_slippage <= 0.25:
            raise ValueError("max_slippage must be in (0, 0.25]")
        if self.min_depth_usd <= 0:
            raise ValueError("min_depth_usd must be > 0")


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
                 clock: Callable[[], datetime] | None = None,
                 live_quotes: Callable[[str], dict[str, Any] | None] | None = None):
        self.governor, self.ledger, self.store = governor, ledger, store
        # 13A: an optional real-time quote source (token_id -> {"bid","ask","at"}). It only ever
        # replaces a recorded quote with a FRESHER one; it never loosens a freshness or spread rule.
        self.live_quotes = live_quotes
        self.settings = settings or PaperSettings()
        self.resolver = resolver              # asks the venue whether markets have resolved (read-only)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = ledger.path
        self._lock = threading.Lock()
        self.last_tick: dict[str, Any] | None = None
        with self._db() as db:
            db.executescript(_SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """14A: additive, idempotent columns recording what the fill really cost."""
        cols = {r["name"] for r in db.execute("PRAGMA table_info(paper_plans)").fetchall()}
        for name in ("entry_requested_stake", "entry_slippage", "entry_levels", "entry_complete",
                     "exit_slippage", "exit_levels", "exit_complete",
                     # 14B: the fee each side of the round trip was charged, and the raw fill price
                     # before it, so the cost of trading can be separated from the cost of size.
                     "entry_fill_price", "entry_fee_usd", "exit_fill_price", "exit_fee_usd"):
            if name not in cols:
                db.execute(f"ALTER TABLE paper_plans ADD COLUMN {name} REAL")
        # The books themselves, so "at what size does this edge disappear?" can be answered by
        # re-walking the real depth rather than extrapolating from a single filled size.
        if "maker_entry" not in cols:
            db.execute("ALTER TABLE paper_plans ADD COLUMN maker_entry REAL")
        for name in ("fee_category", "entry_book", "exit_book"):
            if name not in cols:
                db.execute(f"ALTER TABLE paper_plans ADD COLUMN {name} TEXT")

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
        now_dt = self.clock()
        now = now_dt.isoformat(timespec="seconds")
        ok = lambda x: isinstance(x, (int, float)) and math.isfinite(x) and 0.0 <= x <= 1.0   # noqa: E731
        stored = None
        q = self.store.quote_near(token_id, now, max_age_minutes / 60.0)
        if q:
            bid, ask = q.get("best_bid"), q.get("best_ask")
            stored = {"bid": bid if ok(bid) else None, "ask": ask if ok(ask) else None, "at": q["observed_at"],
                      "levels": self._levels(token_id, q["observed_at"])}
        live = self._live_quote(token_id, now_dt, max_age_minutes, ok)
        if live and (stored is None or live["at"] >= stored["at"]):   # a tie goes to the stream
            return live
        return stored

    def _live_quote(self, token_id: str, now: datetime, max_age_minutes: float, ok) -> dict[str, Any] | None:
        if self.live_quotes is None:
            return None
        try:
            q = self.live_quotes(token_id)
        except Exception:                                  # a broken feed must never break trading logic
            return None
        if not q or not isinstance(q.get("at"), str) or not ok(q.get("bid")) or not ok(q.get("ask")):
            return None
        if q["bid"] > q["ask"]:                            # crossed: the same rule the store applies
            return None
        if q["at"] < (now - timedelta(minutes=max_age_minutes)).isoformat(timespec="seconds"):
            return None
        return {"bid": q["bid"], "ask": q["ask"], "at": q["at"], "levels": q.get("levels")}

    def _fee_category(self, market_id: str) -> str:
        """Polymarket's fee rate depends on the market's category, so the fee cannot be known
        without classifying the question. An unreadable market falls through to the highest rate."""
        try:
            from jarvis.market.segments import category_of, segment_of
            with self.store._db() as db:
                r = db.execute("SELECT question, neg_risk FROM markets WHERE market_id=?",
                               (str(market_id),)).fetchone()
            if not r:
                return "unknown"
            question = r["question"]
            if segment_of(question, r["neg_risk"]) == "sports":
                return "sports"
            return category_of(question)
        except Exception:                                      # noqa: BLE001
            return "unknown"

    def _levels(self, token_id: str, observed_at: str) -> dict[str, Any] | None:
        """The book FRIDAY recorded in the snapshot this quote came from. None when the store cannot
        report depth at all (a minimal store in a test); an empty book is a real answer, not None."""
        reader = getattr(self.store, "levels", None)
        if reader is None:
            return None
        try:
            book = reader(token_id, observed_at)
        except Exception:                                      # noqa: BLE001
            return None
        return book if isinstance(book, dict) else None

    def live_tokens(self) -> set[str]:
        """Tokens whose price can change what this engine does right now: held positions and
        resting orders. The real-time feed watches these first."""
        tokens = {p["token_id"] for p in self._plans().values()}
        tokens |= {o["token_id"] for o in self.resting("PENDING", 2000)}
        return tokens

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
            category = self._fee_category(order.market_id)
            if s.max_round_trip_fraction is not None:
                rt = round_trip(s.costs, bid, ask, category)
                if rt is not None and rt.fraction_of_cost > s.max_round_trip_fraction:
                    return self._refuse(order, (
                        f"the round trip costs {rt.total:.4f} a share, {rt.fraction_of_cost * 100:.1f}% of "
                        f"the {rt.cost_basis:.2f} it commits (spread {rt.spread:.3f} + fees "
                        f"{rt.entry_fee + rt.exit_fee:.4f}, {category}); the limit is "
                        f"{s.max_round_trip_fraction * 100:.0f}%"))
            # 14A: what the book can actually absorb, and what walking it would really cost.
            asks = (q.get("levels") or {}).get("asks") if isinstance(q.get("levels"), dict) else None
            depth_on = self.settings.depth_aware and asks is not None
            room = capacity(asks, self.settings.max_slippage) if depth_on else float(order.stake)
            if depth_on and room < s.min_depth_usd:
                return self._refuse(order, f"not enough depth: ${room:.2f} rests within "
                                           f"{self.settings.max_slippage:g} of the best ask")
            wanted = min(float(order.stake), room) if depth_on else float(order.stake)

            fills: dict[float, Any] = {}
            costs: dict[float, Any] = {}

            def price_for(approved: float) -> float | None:
                """Price the fill against the stake the governor approved, not the one requested,
                and fold in the taker fee so everything downstream sees a net-of-cost position."""
                if depth_on:
                    fill = buy_for_stake(asks, approved, self.settings.max_slippage)
                    fills[approved] = fill
                    raw, shares = fill.price, fill.shares
                else:
                    raw, shares = ask, (approved / ask if ask else 0.0)
                if raw is None:
                    return None
                cost = cost_of(s.costs, shares, raw, category)
                costs[approved] = cost
                return min(0.9999, round(raw + cost.per_share, 6))

            from jarvis.risk import LedgerError, Proposal
            try:
                decision, position = self.ledger.open_checked(
                    self.governor, Proposal(order.strategy, order.market_id, wanted, venue="polymarket"),
                    ask, self.prices(), instrument=order.token_id, label=order.label, price_for=price_for)
            except LedgerError as exc:
                return self._refuse(order, f"ledger: {exc}")
            if position is None:
                return self._refuse(order, f"risk governor: {decision['reason']}", decision)
            now = self.clock()
            approved = float(decision["approved_stake"])
            fill = fills.get(approved)
            cost = costs.get(approved)
            # The stop and the target hang off the price we REALLY paid, not the best ask we hoped
            # for. A fill that walked the book starts further from its stop than the plan intended.
            entry = float(position["entry_price"])
            with self._db() as db:
                db.execute(
                    "INSERT INTO paper_plans(position_id, token_id, market_id, entry_ask, entry_bid, entry_quote_at,"
                    " stop_distance, stop_price, target_price, expires_at, entry_requested_stake, entry_slippage,"
                    " entry_levels, entry_complete, entry_fill_price, entry_fee_usd, fee_category, entry_book)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (position["position_id"], order.token_id, str(order.market_id), entry, bid, q["at"],
                     # A stop PRICE is a price: clamp it into [0, 1). SignalTrader deliberately
                     # passes a stop DISTANCE of 0.999 to make the stop unreachable, which used to
                     # record a stop_price of -0.48 -- behaviourally fine, since a bid can never
                     # reach it, but meaningless as data and a trap for anything that reads the
                     # column. Zero is equally unreachable while still being a price.
                     float(order.stop), max(0.0, round(entry - order.stop, 6)),
                     None if order.target is None else round(min(1.0, entry + order.target), 6),
                     (now + timedelta(hours=float(order.max_hold_hours))).isoformat(timespec="seconds"),
                     float(order.stake), (fill.slippage if fill else 0.0),
                     float(fill.levels_used if fill else 1), 1.0 if (fill is None or fill.complete) else 0.0,
                     (fill.price if fill else ask), (cost.fee_usd if cost else 0.0), category,
                     json.dumps(q.get("levels")) if isinstance(q.get("levels"), dict) else None))
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
            from jarvis.risk import Proposal
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
        from jarvis.risk import LedgerError, Proposal
        for o in reversed(self.resting("PENDING", 2000)):
            q = self._quote(o["token_id"], self.settings.exit_quote_age_minutes)
            fresh = q is not None and q["ask"] is not None and q["at"] > o["placed_at"]
            if fresh and q["ask"] < o["limit_price"] - 1e-9:                 # traded through: filled at our price
                if q["bid"] is None:
                    q["bid"] = q["ask"]
                L = o["limit_price"]
                category = self._fee_category(o["market_id"])
                # 18A: this is a MAKER fill. The order rested at L and someone else crossed into
                # it, so the price is L (not the market ask, which is now better) and no taker fee
                # is charged. Previously the maker treatment happened only because no fee was
                # applied on this path at all, which was right by accident.
                asks = (q.get("levels") or {}).get("asks") if isinstance(q.get("levels"), dict) else None
                room = None
                if self.settings.depth_aware and asks:
                    # How much sell interest is observable at or better than our limit. A resting
                    # order can only be filled by someone willing to sell to it, and this is the
                    # one bound on that the snapshot can support. It is still optimistic: it
                    # assumes every such share would have come to us.
                    room = capacity(asks, L - asks[0][0])
                    if room < self.settings.min_depth_usd:
                        done(o["order_id"], "REFUSED",
                             f"traded through {L:.3f} but only ${room:.2f} rests at or under it")
                        continue
                wanted = min(float(o["stake"]), room) if room is not None else float(o["stake"])
                maker_costs: dict[float, Any] = {}

                def maker_price(approved: float, _L=L, _cat=category) -> float | None:
                    cost = cost_of(self.settings.costs, approved / _L, _L, _cat, maker=True)
                    maker_costs[approved] = cost
                    return max(0.0001, min(0.9999, round(_L + cost.per_share, 6)))

                try:
                    decision, position = self.ledger.open_checked(
                        self.governor, Proposal(o["strategy"], o["market_id"], wanted, venue="polymarket"),
                        L, self.prices(), instrument=o["token_id"], label=o["label"] or "",
                        price_for=maker_price)
                except LedgerError as exc:
                    done(o["order_id"], "REFUSED", f"ledger: {exc}")
                    continue
                if position is None:
                    done(o["order_id"], "REFUSED", f"risk governor: {decision['reason']}")
                    continue
                maker_cost = maker_costs.get(float(decision["approved_stake"]))
                entry = float(position["entry_price"])
                with self._db() as db:
                    db.execute(
                        "INSERT INTO paper_plans(position_id, token_id, market_id, entry_ask, entry_bid, entry_quote_at,"
                        " stop_distance, stop_price, target_price, expires_at, entry_requested_stake,"
                        " entry_slippage, entry_levels, entry_complete, entry_fill_price, entry_fee_usd,"
                        " fee_category, entry_book, maker_entry) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                        (position["position_id"], o["token_id"], o["market_id"], entry, L, q["at"], o["stop"],
                         max(0.0, round(entry - o["stop"], 6)),
                         None if o["target"] is None else round(min(1.0, entry + o["target"]), 6),
                         (now + timedelta(hours=o["max_hold_hours"])).isoformat(timespec="seconds"),
                         float(o["stake"]), 0.0, 1.0, 1.0, L,
                         (maker_cost.fee_usd if maker_cost else 0.0), category,
                         json.dumps(q.get("levels")) if isinstance(q.get("levels"), dict) else None))
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

    def _exit_fill(self, position: dict[str, Any], quote: dict[str, Any] | None):
        """14A: what selling this whole position into the recorded bids would really fetch.

        No slippage cap here: an exit is not optional, so the model walks what the book shows. It
        is still optimistic — it assumes the whole visible book is ours — but it no longer pretends
        an entire position clears at the best bid. Returns None when depth is unavailable."""
        if not self.settings.depth_aware or not quote:
            return None
        levels = quote.get("levels")
        bids = levels.get("bids") if isinstance(levels, dict) else None
        if not bids:
            return None
        fill = sell_shares(bids, float(position["units"]))
        return fill if fill.price is not None else None

    def _close(self, position: dict[str, Any], price: float, reason: str, quote: dict[str, Any] | None,
               fill=None) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT fee_category FROM paper_plans WHERE position_id=?",
                             (position["position_id"],)).fetchone()
        # 14B: the taker is charged on the way out as well. Netting it off the exit price keeps the
        # ledger's own arithmetic the single source of PnL instead of adding a second, parallel one.
        cost = cost_of(self.settings.costs, float(position["units"]), price,
                       row["fee_category"] if row else None)
        net = max(0.0, round(price - cost.per_share, 6))
        closed = self.ledger.close(position["position_id"], net, reason)
        with self._db() as db:
            db.execute("UPDATE paper_plans SET exit_bid=?, exit_ask=?, exit_quote_at=?, exit_reason=?,"
                       " exit_slippage=?, exit_levels=?, exit_complete=?, exit_fill_price=?, exit_fee_usd=?,"
                       " exit_book=? WHERE position_id=?",
                       (quote["bid"] if quote else None, quote["ask"] if quote else None, quote["at"] if quote else None,
                        reason, fill.slippage if fill else None,
                        float(fill.levels_used) if fill else None,
                        (1.0 if fill.complete else 0.0) if fill else None,
                        price, cost.fee_usd,
                        json.dumps(quote.get("levels")) if quote and isinstance(quote.get("levels"), dict) else None,
                        position["position_id"]))
        return closed

    @staticmethod
    def _end_dt(raw: str | None) -> datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _settlement_due(self, market_id: str, bid: float, ends: dict[str, str],
                        now: datetime) -> bool:
        """13B: is this position inside the pre-resolution window, and not already so far in front
        that selling would cost more spread than holding could still gain?"""
        if self.settings.settle_before_minutes <= 0:
            return False
        end = self._end_dt(ends.get(str(market_id)))
        if end is None:
            return False                                    # no end date recorded: the time limit still applies
        hold = self.settings.settle_hold_above
        if hold is not None and bid >= hold:
            return False
        return now >= end - timedelta(minutes=self.settings.settle_before_minutes)

    def _record_settlement(self, closed: dict[str, Any], plan: dict[str, Any],
                           ends: dict[str, str], now: datetime) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO paper_settlements(position_id, token_id, market_id, settled_at,"
                " market_end, exit_bid, units, proceeds) VALUES(?,?,?,?,?,?,?,?)",
                (closed["position_id"], plan["token_id"], str(closed["market_id"]),
                 now.isoformat(timespec="seconds"), str(ends.get(str(closed["market_id"])) or ""),
                 float(closed["exit_price"]), float(closed["units"]), float(closed["proceeds"])))

    def capacity_report(self, ladder=None) -> dict[str, Any]:
        """14B: at what size does each strategy's edge stop existing? Re-walks the real books every
        closed trade entered against and exited into, at a ladder of larger stakes."""
        from .capacity import LADDER, report
        sql = ("SELECT p.strategy, p.units, pl.entry_fill_price, pl.entry_fee_usd, pl.exit_fill_price,"
               " pl.exit_fee_usd, pl.fee_category, pl.entry_book, pl.exit_book"
               " FROM paper_positions p JOIN paper_plans pl ON pl.position_id = p.position_id"
               " WHERE p.status='CLOSED'")
        try:
            with self._db() as db:
                rows = [dict(r) for r in db.execute(sql).fetchall()]
        except sqlite3.Error:
            # The plans and the positions live in separate databases in some deployments; without
            # the join there is nothing to replay, and saying so beats a 500.
            return {"version": "14B.1", "strategies": {}, "note": "positions and plans are not joinable here"}
        out = report(rows, self.settings.costs, ladder or LADDER,
                     self.settings.max_slippage, self.settings.min_depth_usd)
        out["costs"] = {"enabled": self.settings.costs.enabled,
                        "fee_multiplier": self.settings.costs.fee_multiplier,
                        "gas_usd_per_fill": self.settings.costs.gas_usd_per_fill}
        return out

    def settlement_report(self) -> dict[str, Any]:
        """13B: did exiting before resolution help or cost? For every settled position whose market
        has since resolved, compare what it was sold for with what holding would have paid. This is
        measured, not assumed: if early exit is costing money, this report is where it shows."""
        with self._db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM paper_settlements").fetchall()]
        if not rows:
            return {"settled": 0, "resolved": 0, "gain_from_exiting": None, "unresolved": 0, "detail": []}
        resolutions = self.store.resolutions(sorted({r["market_id"] for r in rows}))
        detail, total = [], 0.0
        for r in rows:
            res = resolutions.get(r["market_id"])
            payoff = (res or {}).get("payoffs", {}).get(r["token_id"])
            if payoff is None:
                continue
            held = float(payoff) * r["units"]
            delta = round(r["proceeds"] - held, 4)          # positive: exiting early was worth it
            total += delta
            detail.append({"position_id": r["position_id"], "market_id": r["market_id"],
                           "exit_bid": r["exit_bid"], "resolved_at_payoff": float(payoff),
                           "sold_for": round(r["proceeds"], 4), "would_have_paid": round(held, 4),
                           "gain_from_exiting": delta})
        return {"settled": len(rows), "resolved": len(detail), "unresolved": len(rows) - len(detail),
                "gain_from_exiting": round(total, 4) if detail else None,
                "avg_per_settlement": round(total / len(detail), 4) if detail else None,
                "detail": detail[-50:]}

    def tick(self, *_: Any) -> dict[str, Any]:
        """Look at every open paper position once and close those whose exit has come. Called after
        each stored market scan; safe to call at any time."""
        with self._lock:
            now = self.clock()
            plans = self._plans()
            open_positions = [p for p in self.ledger.positions("OPEN") if p["position_id"] in plans]
            summary = {"at": now.isoformat(timespec="seconds"), "open": len(open_positions), "closed": [],
                       "waiting_for_a_price": 0, "errors": [], "filled": [], "expired": 0, "settled": 0}
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
            ends = {}
            if self.settings.settle_before_minutes > 0:
                reader = getattr(self.store, "market_end_dates", None)
                try:                                       # a store without end dates: no settlement, no error
                    ends = reader(markets) if reader else {}
                except Exception as exc:
                    summary["errors"].append(f"end dates: {type(exc).__name__}")
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
                        ex = self._exit_fill(p, q)
                        done = self._close(p, ex.price if ex else q["bid"], "stop", q, ex)
                    elif plan["target_price"] is not None and q["bid"] >= plan["target_price"] - 1e-9:
                        done = self._close(p, plan["target_price"], "target", q)
                    elif self._settlement_due(p["market_id"], q["bid"], ends, now):
                        ex = self._exit_fill(p, q)
                        done = self._close(p, ex.price if ex else q["bid"], "settlement", q, ex)
                        self._record_settlement(done, plan, ends, now)
                        summary["settled"] += 1
                    elif now.isoformat(timespec="seconds") >= plan["expires_at"]:
                        ex = self._exit_fill(p, q)
                        done = self._close(p, ex.price if ex else q["bid"], "time", q, ex)
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
