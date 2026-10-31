"""
FRIDAY multi-leg arbitrage (Phase 15): trading the pricing inconsistency the 9C scan already finds.

A two-outcome market pays exactly $1 to one side. If the best ask of YES plus the best ask of NO is
below $1, buying one share of each locks in the difference whatever happens. No forecast is needed
and no view is taken. The 9C scanner has been reporting these since Phase 9 and nothing traded them.

### Why this needed 14A and 14B first
Both of them, or this module would be a machine for inventing money.

**Depth (14A).** The gap is almost always tiny and sits on a few dollars of resting size. Filling
"any size at the best ask" would have turned a $3 opportunity into a $500 one.

**Cost (14B).** The fee is the whole story here, and it is why the 9C scan has a `fee` parameter
that defaulted to 0. The taker fee is `rate * price * (1 - price)` per share, so for a pair at
prices p and (1 - p) the round trip into the position costs about `rate * 2p(1 - p)` per pair:

| Pair | Gross gap | Fee at 0.05 | Net |
|---|---|---|---|
| 0.49 / 0.49 | 2.0c | 2.5c | **−0.5c: a loss** |
| 0.95 / 0.03 | 2.0c | 0.4c | +1.6c |

A balanced pair near 0.50 pays the maximum fee on *both* legs, so a 2c arbitrage there is a losing
trade. The same 2c gap in a lopsided market, or in a zero-fee category, is real. Nothing about that
is visible without the cost model, which is why this phase comes third and not first.

### Atomicity
An arbitrage is riskless only as a whole. One leg filled and the other not is a naked directional
bet — the opposite of what was approved. So both legs are priced against the stake the governor
approved, checked against cash together, and inserted in a single transaction by
`PaperLedger.open_legs`. Any failure leaves no position at all.

### The exit is resolution, not a price
A complete pair pays exactly $1 whichever way the market resolves, so it is held to resolution and
closed at the recorded payoffs. Selling early would realise `bid_yes + bid_no`, which is below $1 —
it would hand back the entire edge plus the spread. These positions carry no plan row, so the
engine's stop, target, time limit and 13B settlement never see them. That is deliberate: every one
of those exits would destroy the thing being captured.

### What can still go wrong, and is not modelled
- **The gap may be stale.** The scan sees one snapshot every 15 minutes. A gap that was there when
  recorded may be gone before an order could reach the exchange. This is the largest unmodelled
  risk and it is the reason a paper record here is weaker evidence than it looks.
- **Both legs must actually fill.** Real atomicity does not exist on the CLOB: two orders are two
  orders. The live version of this needs a leg-failure policy (unwind immediately at the bid, and
  wear the loss) which paper trading cannot test.
- **Resolution risk.** The pair pays $1 if the market resolves at all. A market that is voided, or
  an oracle dispute, breaks the identity the whole trade rests on.
- **Multi-candidate events** (buy every candidate in a field for under $1) are not covered: FRIDAY
  does not record which markets belong to one event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .costs import CostSettings, cost_of
from .engine import PaperEngine
from .fills import buy_shares

ARBITRAGE_VERSION = "15.1"
STRATEGY = "PRICING_INCONSISTENCY"
BUDGET = 50.0                 # what one opportunity asks for; the Risk Governor decides the real size
MIN_LOCKED_PER_PAIR = 0.005   # half a cent a pair, net of fees. Below this the gap is noise.
MIN_PAIRS = 5.0               # fewer pairs than this is not worth an order
MAX_AGE_MINUTES = 15.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_arb_pairs(
    pair_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, market_id TEXT NOT NULL, observed_at TEXT,
    leg_a_token TEXT NOT NULL, leg_b_token TEXT NOT NULL,
    leg_a_position_id INTEGER, leg_b_position_id INTEGER,
    pairs REAL NOT NULL, cost_usd REAL NOT NULL, fees_usd REAL NOT NULL, locked_usd REAL NOT NULL,
    ask_sum REAL, decision_id INTEGER,
    status TEXT NOT NULL,                      -- OPEN | CLOSED
    closed_at TEXT, payout_usd REAL, realised_usd REAL, note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_arb_open ON paper_arb_pairs(market_id, status)
    WHERE status='OPEN';
"""


@dataclass(frozen=True)
class PairQuote:
    """What buying `pairs` of both outcomes would really cost, fees included."""
    pairs: float
    cost_usd: float            # everything paid: both legs plus both fees
    fees_usd: float
    leg_a_price: float         # volume-weighted, before fees
    leg_b_price: float
    leg_a_stake: float         # including that leg's share of the fees
    leg_b_stake: float
    complete: bool

    @property
    def payout_usd(self) -> float:
        """A complete pair pays exactly $1, whichever outcome happens."""
        return round(self.pairs, 6)

    @property
    def locked_usd(self) -> float:
        return round(self.payout_usd - self.cost_usd, 6)

    @property
    def locked_per_pair(self) -> float:
        return round(self.locked_usd / self.pairs, 6) if self.pairs > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"pairs": round(self.pairs, 6), "cost_usd": round(self.cost_usd, 6),
                "fees_usd": round(self.fees_usd, 6), "payout_usd": self.payout_usd,
                "locked_usd": self.locked_usd, "locked_per_pair": self.locked_per_pair,
                "leg_a_price": self.leg_a_price, "leg_b_price": self.leg_b_price,
                "complete": self.complete}


def _cost_of_pairs(a_asks, b_asks, pairs: float, costs: CostSettings, category: str | None,
                   max_slippage: float | None) -> tuple[float, float, Any, Any, Any, Any] | None:
    """Walk both books for exactly `pairs` shares each. None if either leg cannot supply them."""
    a = buy_shares(a_asks, pairs, max_slippage)
    b = buy_shares(b_asks, pairs, max_slippage)
    if a.price is None or b.price is None or not (a.complete and b.complete):
        return None
    fa = cost_of(costs, a.shares, a.price, category)
    fb = cost_of(costs, b.shares, b.price, category)
    total = a.stake + b.stake + fa.total_usd + fb.total_usd
    return total, fa.total_usd + fb.total_usd, a, b, fa, fb


def price_pair(a_asks, b_asks, budget: float, costs: CostSettings, category: str | None = None,
               max_slippage: float | None = 0.02, min_pairs: float = MIN_PAIRS) -> PairQuote | None:
    """The largest complete pair position buyable for `budget`, fees included.

    Found by bisection on the number of pairs rather than by splitting the budget between the legs:
    the legs can cost wildly different amounts (0.95 against 0.03) and an even split would leave
    most of the budget unused. Cost per pair is monotone in the number of pairs, so bisection is
    exact. None when `budget` cannot buy `min_pairs`."""
    if not (isinstance(budget, (int, float)) and budget > 0):
        return None
    a_top = (a_asks or [[0, 0]])[0][0] if a_asks else 0.0
    b_top = (b_asks or [[0, 0]])[0][0] if b_asks else 0.0
    if not (a_top > 0 and b_top > 0):
        return None
    # Walking the book only ever costs more than the top, so this is a hard upper bound on pairs.
    hi = budget / (a_top + b_top)
    if hi < min_pairs:
        return None
    lo, best = 0.0, None
    for _ in range(50):
        mid = (lo + hi) / 2
        if mid <= 0:
            break
        priced = _cost_of_pairs(a_asks, b_asks, mid, costs, category, max_slippage)
        if priced is not None and priced[0] <= budget + 1e-9:
            best, lo = (mid, priced), mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    if best is None:
        return None
    pairs, (total, fees, a, b, fa, fb) = best
    if pairs < min_pairs:
        return None
    return PairQuote(pairs=round(pairs, 6), cost_usd=round(total, 6), fees_usd=round(fees, 6),
                     leg_a_price=a.price, leg_b_price=b.price,
                     leg_a_stake=round(a.stake + fa.total_usd, 6),
                     leg_b_stake=round(b.stake + fb.total_usd, 6),
                     complete=bool(a.complete and b.complete))


class PairTrader:
    """Opens an arbitrage pair when the gate allows it and the net gap survives fees; closes it at
    resolution. Originates nothing: the 9C scan decides whether this strategy may trade at all."""

    def __init__(self, engine: PaperEngine, store, gate_provider: Callable[[], dict[str, Any]] | None,
                 enabled: bool = False, budget: float = BUDGET,
                 min_locked_per_pair: float = MIN_LOCKED_PER_PAIR, min_pairs: float = MIN_PAIRS,
                 max_age_minutes: float = MAX_AGE_MINUTES, executor: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.engine, self.store = engine, store
        self.gate_provider = gate_provider
        self.enabled = bool(enabled)
        self.budget = float(budget)
        self.min_locked_per_pair = float(min_locked_per_pair)
        self.min_pairs = float(min_pairs)
        self.max_age_minutes = float(max_age_minutes)
        self.executor = executor                   # 16B: may this trade open now, at this size?
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last: dict[str, Any] = {}
        with self.engine._db() as db:
            db.executescript(SCHEMA)

    # ------------------------------------------------------------ the gate
    def allowed(self) -> bool:
        """Does the gate currently allow PRICING_INCONSISTENCY for paper? No gate, no trading."""
        if self.gate_provider is None:
            return False
        try:
            gate = self.gate_provider()
        except Exception:                                   # noqa: BLE001 - fail closed, never guess
            return False
        if not isinstance(gate, dict):
            return False
        for key in ("approved_for_paper", "forward_test"):
            v = gate.get(key, ())
            if isinstance(v, (list, tuple, set, frozenset)) and STRATEGY in v:
                return True
        return False

    # ------------------------------------------------------------ bookkeeping
    def _open_markets(self) -> set[str]:
        with self.engine._db() as db:
            return {r["market_id"] for r in
                    db.execute("SELECT market_id FROM paper_arb_pairs WHERE status='OPEN'").fetchall()}

    def _refuse(self, market_id: str, reason: str) -> None:
        with self.engine._db() as db:
            db.execute("INSERT INTO paper_refusals(at, strategy, market_id, reason) VALUES(?,?,?,?)",
                       (self.clock().isoformat(timespec="seconds"), STRATEGY, str(market_id)[:80], reason[:200]))

    def _books(self, candidate: dict[str, Any]) -> tuple[Any, Any] | None:
        reader = getattr(self.store, "levels_for_snapshot", None)
        if reader is None:
            return None
        try:
            a = reader(candidate["legs"][0]["snapshot_id"])
            b = reader(candidate["legs"][1]["snapshot_id"])
        except Exception:                                      # noqa: BLE001
            return None
        if not (a.get("asks") and b.get("asks")):
            return None
        return a["asks"], b["asks"]

    # ------------------------------------------------------------ the run
    def on_run(self, *_: Any) -> dict[str, Any]:
        now = self.clock()
        out: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "version": ARBITRAGE_VERSION,
                               "considered": 0, "opened": 0, "refused": 0, "closed": 0, "pairs": []}
        if not self.enabled:
            out["inactive"] = "disabled"
            self.last = out
            return out
        try:
            out["closed"] = self._settle_resolved()
        except Exception as exc:                               # noqa: BLE001
            out["errors"] = [f"settle: {type(exc).__name__}: {str(exc)[:80]}"]
        if not self.allowed():
            out["inactive"] = f"the gate does not allow {STRATEGY} for paper trading"
            self.last = out
            return out

        held = self._open_markets()
        reader = getattr(self.store, "arbitrage_candidates", None)
        if reader is None:
            out["inactive"] = "this store cannot report arbitrage candidates"
            self.last = out
            return out
        candidates = reader(self.max_age_minutes, 200, now.isoformat(timespec="seconds"))
        # A resolved market still shows a gap in its last snapshot, and the gap is meaningless: the
        # outcome is already known and nothing will trade there again.
        try:
            settled = set(self.store.resolutions(sorted({c["market_id"] for c in candidates})))
        except Exception:                                      # noqa: BLE001
            settled = set()
        for cand in candidates:
            if cand["market_id"] in held or cand["market_id"] in settled:
                continue
            out["considered"] += 1
            result = self._try(cand)
            out["pairs"].append(result)
            if result["status"] == "OPENED":
                out["opened"] += 1
            else:
                out["refused"] += 1
        self.last = out
        return out

    def _try(self, cand: dict[str, Any]) -> dict[str, Any]:
        market_id = cand["market_id"]
        books = self._books(cand)
        if books is None:
            self._refuse(market_id, "no recorded depth behind the two asks")
            return {"market_id": market_id, "status": "REFUSED", "reason": "no recorded depth"}
        a_asks, b_asks = books
        category = self.engine._fee_category(market_id)
        costs = self.engine.settings.costs
        slip = self.engine.settings.max_slippage

        budget = self.budget
        if self.executor is not None:
            v = self.executor.may_open(STRATEGY, self.budget)
            if not v.allowed:
                self._refuse(market_id, f"executor: {v.reason}")
                return {"market_id": market_id, "status": "REFUSED", "reason": f"executor: {v.reason}"}
            budget = min(self.budget, v.max_stake)

        quotes: dict[float, PairQuote] = {}

        def legs_for(approved: float) -> list[dict[str, Any]] | None:
            q = price_pair(a_asks, b_asks, approved, costs, category, slip, self.min_pairs)
            if q is None or q.locked_per_pair < self.min_locked_per_pair:
                return None
            quotes[approved] = q
            # Each leg is booked at its own fee-inclusive price, the same convention as every other
            # position in the ledger, so PnL needs no special case for an arbitrage.
            return [{"stake": q.leg_a_stake, "price": round(q.leg_a_stake / q.pairs, 6),
                     "units": q.pairs, "instrument": cand["legs"][0]["token_id"], "label": "arb-leg-a"},
                    {"stake": q.leg_b_stake, "price": round(q.leg_b_stake / q.pairs, 6),
                     "units": q.pairs, "instrument": cand["legs"][1]["token_id"], "label": "arb-leg-b"}]

        # Refuse before troubling the governor when the gap does not survive fees at any size.
        probe = price_pair(a_asks, b_asks, budget, costs, category, slip, self.min_pairs)
        if probe is None:
            self._refuse(market_id, f"not enough depth for {self.min_pairs:g} complete pairs")
            return {"market_id": market_id, "status": "REFUSED", "reason": "not enough depth"}
        if probe.locked_per_pair < self.min_locked_per_pair:
            reason = (f"gross gap {1 - cand['ask_sum']:.4f} does not survive fees: "
                      f"{probe.locked_per_pair:+.4f} a pair net ({category})")
            self._refuse(market_id, reason)
            return {"market_id": market_id, "status": "REFUSED", "reason": reason,
                    "quote": probe.as_dict()}

        from jarvis.risk import LedgerError, Proposal
        try:
            decision, legs = self.engine.ledger.open_legs_checked(
                self.engine.governor,
                Proposal(STRATEGY, market_id, probe.cost_usd, venue="polymarket"),
                legs_for, self.engine.prices())
        except LedgerError as exc:
            self._refuse(market_id, f"ledger: {exc}")
            return {"market_id": market_id, "status": "REFUSED", "reason": f"ledger: {exc}"}
        if legs is None:
            self._refuse(market_id, f"risk governor: {decision['reason']}")
            return {"market_id": market_id, "status": "REFUSED", "reason": decision["reason"]}

        q = quotes[float(decision["approved_stake"])]
        with self.engine._db() as db:
            db.execute(
                "INSERT INTO paper_arb_pairs(at, market_id, observed_at, leg_a_token, leg_b_token,"
                " leg_a_position_id, leg_b_position_id, pairs, cost_usd, fees_usd, locked_usd, ask_sum,"
                " decision_id, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
                (self.clock().isoformat(timespec="seconds"), market_id, cand["observed_at"],
                 cand["legs"][0]["token_id"], cand["legs"][1]["token_id"],
                 legs[0]["position_id"], legs[1]["position_id"], q.pairs, q.cost_usd, q.fees_usd,
                 q.locked_usd, cand["ask_sum"], decision["decision_id"]))
        try:
            self.store.watch([market_id])
        except Exception:                                      # noqa: BLE001
            pass
        return {"market_id": market_id, "status": "OPENED", "quote": q.as_dict(),
                "positions": [legs[0]["position_id"], legs[1]["position_id"]],
                "category": category}

    # ------------------------------------------------------------ the exit
    def _settle_resolved(self) -> int:
        """Close both legs of every open pair whose market has resolved. There is no other exit:
        selling early would hand back the edge and the spread with it."""
        with self.engine._db() as db:
            rows = [dict(r) for r in
                    db.execute("SELECT * FROM paper_arb_pairs WHERE status='OPEN'").fetchall()]
        if not rows:
            return 0
        resolved = self.store.resolutions(sorted({r["market_id"] for r in rows}))
        closed = 0
        for r in rows:
            res = resolved.get(r["market_id"])
            payoffs = (res or {}).get("payoffs") or {}
            if r["leg_a_token"] not in payoffs or r["leg_b_token"] not in payoffs:
                continue
            payout = 0.0
            for token, pid in ((r["leg_a_token"], r["leg_a_position_id"]),
                               (r["leg_b_token"], r["leg_b_position_id"])):
                done = self.engine.ledger.close(int(pid), float(payoffs[token]), "arb-resolved")
                payout += float(done["proceeds"])
            with self.engine._db() as db:
                db.execute("UPDATE paper_arb_pairs SET status='CLOSED', closed_at=?, payout_usd=?,"
                           " realised_usd=?, note=? WHERE pair_id=?",
                           (self.clock().isoformat(timespec="seconds"), round(payout, 6),
                            round(payout - r["cost_usd"], 6), "resolved", r["pair_id"]))
            closed += 1
        return closed

    # ------------------------------------------------------------ reporting
    def report(self, limit: int = 100) -> dict[str, Any]:
        with self.engine._db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM paper_arb_pairs ORDER BY pair_id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),)).fetchall()]
        done = [r for r in rows if r["status"] == "CLOSED" and r["realised_usd"] is not None]
        return {"version": ARBITRAGE_VERSION, "enabled": self.enabled, "strategy": STRATEGY,
                "allowed_now": self.allowed(),
                "open": sum(1 for r in rows if r["status"] == "OPEN"), "closed": len(done),
                "expected_locked_usd": round(sum(r["locked_usd"] for r in rows), 4),
                "realised_usd": round(sum(r["realised_usd"] for r in done), 4) if done else None,
                "fees_usd": round(sum(r["fees_usd"] for r in rows), 4),
                # Expected minus realised is the cost of the things the model does not include:
                # a gap that had moved, a leg that filled worse, a market that did not pay $1.
                "slippage_vs_expected": (round(sum(r["realised_usd"] - r["locked_usd"] for r in done), 4)
                                         if done else None),
                "min_locked_per_pair": self.min_locked_per_pair, "budget": self.budget,
                "pairs": rows[:20], "last_run": self.last}
