"""
FRIDAY Validation Lab — instrument calibration (Phase 17).

Everything up to Phase 16 built an apparatus for measuring edge. Nothing ever checked that the
apparatus can measure. Its arithmetic is unit-tested to the penny, which is a different claim: a
thermometer with perfect arithmetic and a bad sensor reads 20 degrees in a furnace.

So this plants strategies whose informational content is **known by construction** into a synthetic
market, runs them through the real pipeline end to end, and checks that the instrument reaches the
verdict it must reach. Nothing is mocked. It uses the real `MarketStore`, the real `Scorer`, the
real edge report, the real `CostSettings` and the real `Executor`. If any of those are wrong, this
fails — which is the whole point.

### The planted strategies
Each knows exactly as much as its name says, and the correct verdict follows from the construction
rather than from whatever the code happens to output:

    ORACLE      sees the exit price AND only trades when the coming move clears the round trip.
                Should be MAKES_MONEY.
    WEAK        sees the exit price but only trades the small moves, which the round trip eats.
                Should be KNOWS_SOMETHING_BUT_COSTS_EAT_IT: real information, too small to keep.
    COINFLIP    seeded random direction, no information at all. Should be NO_EDGE.
    ANTI        sees the exit price and deliberately bets against it.
                Should be WORSE_THAN_A_COIN_FLIP.

If the report calls COINFLIP profitable, the instrument is broken and every number this platform
has ever produced is suspect. If it calls ORACLE worthless, it would veto a real edge. Those are
the two failure modes that matter, and they are opposite, so one harness has to catch both.

### What this is NOT
Not evidence that any real strategy works. The synthetic market has no relationship to Polymarket
beyond having plausible spreads and depth, and ORACLE cheats by construction. **A planted edge
being detected says the measuring device works. It says nothing about whether a real edge exists.**
Only forward paper trading on live data can say that, which is still the one thing this project has
never done.

The market is a bounded random walk in the true probability with a spread around it, quoted to a
tick, with configurable depth on each side. A fixed seed makes every run reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis.market.models import Market, Outcome, SnapshotRow
from jarvis.market.store import MarketStore
from jarvis.paper.costs import CostSettings

REHEARSAL_VERSION = "17.1"

ORACLE, WEAK, COINFLIP, ANTI = "ORACLE", "WEAK", "COINFLIP", "ANTI"
PLANTED = (ORACLE, WEAK, COINFLIP, ANTI)


@dataclass
class SimSettings:
    markets: int = 40
    scans: int = 30                  # how many 15-minute scans to simulate
    minutes_between_scans: float = 15.0
    horizon_hours: float = 1.0       # the signal horizon, in hours
    spread: float = 0.02             # quoted spread, in dollars of price
    depth_shares: float = 4000.0     # resting size per level; deep, so 14A is not the variable here
    levels: int = 5
    volatility: float = 0.02         # per-scan sd of the true probability
    tick: float = 0.01
    seed: int = 17
    # ORACLE only acts when the coming move clears the round trip several times over. The first
    # calibration run showed why this is necessary: an oracle that trades EVERY move it foresees
    # still loses money, because crossing a 2c spread and paying a ~2c fee costs more than the
    # typical hourly move is worth. Information is not the binding constraint on this venue;
    # selectivity about size is.
    oracle_min_move: float = 0.06
    # WEAK trades only moves in this band: real information, but smaller than the round trip.
    weak_max_move: float = 0.03
    category_question: str = "Will the index close above 100 on Friday?"   # -> "economy", 0.05


@dataclass
class Rehearsal:
    settings: SimSettings = field(default_factory=SimSettings)
    start: datetime = field(default_factory=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))

    # ------------------------------------------------------------ the market
    def _quote(self, p: float) -> tuple[float, float]:
        """A two-sided quote around a true probability, on the tick grid and inside (0, 1)."""
        s = self.settings
        half = s.spread / 2.0
        bid = max(s.tick, min(1.0 - 2 * s.tick, p - half))
        ask = max(bid + s.tick, min(1.0 - s.tick, p + half))
        q = lambda x: round(round(x / s.tick) * s.tick, 4)          # noqa: E731
        bid, ask = q(bid), q(ask)
        return (bid, ask) if ask > bid else (bid, round(bid + s.tick, 4))

    def _book(self, bid: float, ask: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        s = self.settings
        bids = [(round(bid - i * s.tick, 4), s.depth_shares) for i in range(s.levels)
                if bid - i * s.tick > 0]
        asks = [(round(ask + i * s.tick, 4), s.depth_shares) for i in range(s.levels)
                if ask + i * s.tick < 1]
        return bids, asks

    def build(self, store: MarketStore) -> dict[str, list[dict[str, Any]]]:
        """Record `scans` scans of a synthetic universe into a real store. Returns, per scan, the
        quote of every YES token, which is what the planted strategies get to see."""
        s = self.settings
        rng = random.Random(s.seed)
        truth = {f"m{i}": rng.uniform(0.15, 0.85) for i in range(s.markets)}
        end = (self.start + timedelta(days=30)).isoformat(timespec="seconds")

        markets = [Market(market_id=mid, question=f"{s.category_question} [{mid}]", end_date=end,
                          neg_risk=False,
                          outcomes=[Outcome(name="Yes", token_id=f"{mid}-yes"),
                                    Outcome(name="No", token_id=f"{mid}-no")])
                   for mid in truth]

        timeline: dict[str, list[dict[str, Any]]] = {}
        for scan in range(s.scans):
            at = (self.start + timedelta(minutes=scan * s.minutes_between_scans)).isoformat(timespec="seconds")
            rows, seen = [], []
            for mid in truth:
                truth[mid] = min(0.9, max(0.1, truth[mid] + rng.gauss(0, s.volatility)))
                for name, p in (("Yes", truth[mid]), ("No", 1.0 - truth[mid])):
                    bid, ask = self._quote(p)
                    bids, asks = self._book(bid, ask)
                    rows.append(SnapshotRow(
                        market_id=mid, question=f"{s.category_question} [{mid}]", outcome=name,
                        token_id=f"{mid}-{name.lower()}", best_bid=bid, best_ask=ask,
                        mid=round((bid + ask) / 2, 4), spread=round(ask - bid, 4),
                        last_trade_price=None, depth_usd=s.depth_shares * bid,
                        gamma_liquidity=None, volume_24h=None, end_date=end, flags=[], usable=True,
                        observed_at=at, top_bids=bids, top_asks=asks))
                    if name == "Yes":
                        seen.append({"market_id": mid, "token_id": f"{mid}-yes", "observed_at": at,
                                     "mid": round((bid + ask) / 2, 4), "best_bid": bid, "best_ask": ask})
            store.record(markets, rows, {"observed_at": at, "markets": len(truth),
                                         "tokens": len(rows), "usable_rows": len(rows)}, source="rehearsal")
            timeline[at] = seen
        return timeline

    # ------------------------------------------------------------ the strategies
    def plant(self, store: MarketStore, timeline: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        """Write signals whose informational content is known by construction.

        Every planted strategy is allowed to look up the quote one horizon ahead, which is why none
        of this is evidence about any real strategy. What differs is what each one DOES with it."""
        s = self.settings
        rng = random.Random(s.seed + 1)
        stamps = sorted(timeline)
        ahead = int(round(s.horizon_hours * 60 / s.minutes_between_scans))
        signals: list[dict[str, Any]] = []
        counts = dict.fromkeys(PLANTED, 0)

        for i, at in enumerate(stamps):
            if i + ahead >= len(stamps):
                break                                      # no future quote to cheat with
            future = {row["token_id"]: row for row in timeline[stamps[i + ahead]]}
            expires = (datetime.fromisoformat(at) + timedelta(hours=s.horizon_hours)).isoformat(timespec="seconds")
            for row in timeline[at]:
                nxt = future.get(row["token_id"])
                if nxt is None:
                    continue
                move = nxt["mid"] - row["mid"]
                base = {"observed_at": at, "market_id": row["market_id"], "token_id": row["token_id"],
                        "outcome": "Yes", "strength": 0.6, "horizon_hours": s.horizon_hours,
                        "expires_at": expires, "mid": row["mid"], "best_bid": row["best_bid"],
                        "best_ask": row["best_ask"],
                        "half_spread": round((row["best_ask"] - row["best_bid"]) / 2, 4),
                        "inputs": "{}", "strategy_version": "rehearsal-1"}
                plan = [
                    # Sees the move and backs it, but only when it is big enough to pay for itself.
                    (ORACLE, "UP" if move > 0 else "DOWN", abs(move) >= s.oracle_min_move),
                    # Sees the move and backs it, but only the small ones: real information, too
                    # small to survive the round trip.
                    (WEAK, "UP" if move > 0 else "DOWN", s.tick < abs(move) <= s.weak_max_move),
                    # No information whatsoever.
                    (COINFLIP, "UP" if rng.random() < 0.5 else "DOWN", True),
                    # Sees the move and bets against it.
                    (ANTI, "DOWN" if move > 0 else "UP", abs(move) > s.tick),
                ]
                for name, direction, take in plan:
                    if not take:
                        continue
                    signals.append({**base, "strategy": name, "direction": direction})
                    counts[name] += 1
        if signals:
            store.record_signals(signals)
        return counts

    # ------------------------------------------------------------ the run
    def run(self, store: MarketStore, costs: CostSettings | None = None,
            min_sample: int = 30) -> dict[str, Any]:
        """Build the market, plant the strategies, score them with the REAL scorer, and report with
        the REAL edge report. Returns the report plus what was planted, so the two can be compared."""
        from jarvis.lab.edge import report as edge_report
        from jarvis.quant.scoring import Scorer

        timeline = self.build(store)
        planted = self.plant(store, timeline)
        # The clock is advanced past every horizon so that every signal is due to be scored.
        after = datetime.fromisoformat(sorted(timeline)[-1]) + timedelta(hours=self.settings.horizon_hours + 1)
        # `due_signals` returns at most 500 rows per call, so one pass silently scores a fraction of
        # a large plant and truncates every strategy unevenly. Keep going until nothing is due.
        scorer = Scorer(store, record=True)
        scored = {"scoring_version": None, "due": 0, "scored": 0, "by_status": {}, "passes": 0}
        for _ in range(200):
            once = scorer.score_due(now=after)
            if not once.get("scored"):
                break
            scored["scoring_version"] = once.get("scoring_version")
            scored["due"] += once.get("due") or 0
            scored["scored"] += once.get("scored") or 0
            scored["passes"] += 1
            for k, v in (once.get("by_status") or {}).items():
                scored["by_status"][k] = scored["by_status"].get(k, 0) + v
        report = edge_report(store.outcomes(days=3650, limit=100000), costs or CostSettings(),
                             min_sample=min_sample)
        # Close the loop: the real 16B executor, reading this real report, deciding whether each
        # planted strategy may open a trade. A gate that approves all four is deliberate -- the
        # question being asked here is what the EXECUTOR does once the gate has said yes.
        from jarvis.paper.executor import ExecutionSettings, Executor
        gate = {"approved_for_paper": list(PLANTED), "forward_test": [], "live_approved": []}
        executor = Executor(gate_provider=lambda: gate, edge_provider=lambda: report,
                            settings=ExecutionSettings(enabled=True), clock=lambda: after)
        decisions = {name: executor.may_open(name, 20.0).as_dict() for name in PLANTED}
        return {"rehearsal_version": REHEARSAL_VERSION, "planted": planted,
                "scoring": scored, "edge": report, "execution": decisions,
                "settings": {"markets": self.settings.markets, "scans": self.settings.scans,
                             "spread": self.settings.spread, "horizon_hours": self.settings.horizon_hours,
                             "seed": self.settings.seed}}


# What the instrument MUST find, stated as properties rather than verdict strings and written down
# before the run. Several verdicts can be defensible for the same record -- a coin flip both fails
# to show information AND loses money, and both statements are true -- so what is asserted here is
# the thing that would actually matter if it were wrong.
#
# The two failure modes that would make every number this platform produces worthless:
#   a FALSE POSITIVE on noise      - COINFLIP must not be credited with information
#   a FALSE NEGATIVE on real edge  - ORACLE must not be dismissed
# They are opposite, so one harness has to catch both.
def _money(row):
    return (row.get("net_after_fees") or {})


def _info(row):
    return (row.get("edge_vs_coinflip") or {})


REQUIRED: dict[str, tuple[str, Any]] = {
    ORACLE: ("makes money: the whole after-fee interval above zero",
             lambda r: _money(r).get("beats_zero") is True),
    WEAK: ("knows something, but does not make money",
           lambda r: _info(r).get("beats_zero") is True and _money(r).get("beats_zero") is not True),
    COINFLIP: ("is NOT credited with information it does not have",
               lambda r: _info(r).get("beats_zero") is not True),
    ANTI: ("is shown to be conclusively worse than random",
           lambda r: (_info(r).get("ci95") or [0, 1])[1] < 0),
}


def check(result: dict[str, Any]) -> dict[str, Any]:
    """Did the instrument find what the construction requires it to find?"""
    rows, ok = {}, True
    for name, (what, holds) in REQUIRED.items():
        row = result["edge"]["strategies"].get(name) or {}
        try:
            passed = bool(holds(row))
        except Exception:                                      # noqa: BLE001
            passed = False
        ok = ok and passed
        rows[name] = {"required": what, "pass": passed, "verdict": row.get("verdict"),
                      "scored": row.get("scored", 0),
                      "net_after_fees": _money(row).get("mean"),
                      "edge_vs_coinflip": _info(row).get("mean")}
    return {"calibrated": ok, "checks": rows}


def render(result: dict[str, Any], verdicts: dict[str, Any]) -> str:
    from jarvis.lab.edge import render as render_edge
    lines = [f"INSTRUMENT CALIBRATION {result['rehearsal_version']} — can the edge report measure?",
             f"{result['settings']['markets']} synthetic markets · {result['settings']['scans']} scans · "
             f"spread {result['settings']['spread']} · seed {result['settings']['seed']}",
             f"planted: " + " · ".join(f"{k}={v}" for k, v in result["planted"].items()), ""]
    for name, row in verdicts["checks"].items():
        mark = "PASS" if row["pass"] else "FAIL"
        lines.append(f"  [{mark}] {name:<9} must {row['required']}")
        lines.append(f"         {row['scored']:>5} scored · net {row['net_after_fees']} · "
                     f"vs coinflip {row['edge_vs_coinflip']} · {row['verdict']}")
    lines += ["", "  the 16B executor, reading that report, on a $20 order:"]
    for name, d in (result.get("execution") or {}).items():
        lines.append(f"    {name:<9} {'ALLOW $' + format(d['max_stake'], '.0f') if d['allowed'] else 'REFUSE'}"
                     f"  — {d['reason'][:88]}")
    lines += ["", "CALIBRATED" if verdicts["calibrated"] else "NOT CALIBRATED — the instrument is wrong",
              "", "A planted edge being found says the measuring device works. It says NOTHING about",
              "whether a real edge exists: only forward paper trading on live data can say that.",
              "", render_edge(result["edge"])]
    return "\n".join(lines)
