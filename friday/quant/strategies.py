"""
FRIDAY Quant Core — strategy library (Phase 7B).

Each strategy is a HYPOTHESIS written as a transparent, deterministic rule
over the 7A features of an ELIGIBLE market. A signal says only: "this rule
fired; it expects this token's price to move UP/DOWN within H hours". It
is not an order, carries no size, and grants nothing. Every input the rule
used is recorded with the signal, so 7C can score each strategy against
what the price actually did next — net of the spread — and so any result
can be reproduced.

Rules evaluate the PRIMARY outcome (index 0) of a market: for a binary
market, "YES down" and "NO up" are the same bet, and counting both would
double every statistic.

Starting library (all unvalidated):
  BOOK_IMBALANCE   resting depth heavily skewed to one side precedes a
                   short-term drift toward the heavy side            (1h)
  MEAN_REVERSION   a sharp 1h move, large versus the market's own
                   volatility, partly reverses                        (6h)
  MOMENTUM         a steady 6h trend confirmed by the last hour
                   continues                                          (6h)
  LONGSHOT_BIAS    the favourite-longshot bias: cheap longshots are
                   over-priced, heavy favourites under-priced         (24h)

8C adds one strategy that is not a rule over features but the 8B fair-value
model itself:
  CALIBRATION_EDGE the market is mispriced by its price band's measured,
                   shrunk bias in its own segment: buy the side whose
                   fair value beats the live ask by >= the minimum edge
                   and >= 2 (floored) standard errors. Recorded by the
                   pipeline (it needs the calibration tables), ONCE per
                   market, and judged HELD TO RESOLUTION — that is the
                   claim the model makes. Its horizon is the calibration
                   horizon used (24/72/168h) and only drives the interim
                   mark-to-market score.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .features import MarketFeatures, TokenFeatures

STRATEGIES_VERSION = "8C.1"


@dataclass(frozen=True)
class Strategy:
    name: str
    version: str
    horizon_hours: float
    hypothesis: str
    rule: str
    fn: Callable[[MarketFeatures, TokenFeatures], tuple[str, float, dict[str, Any]] | None] | None
    measure: str = "horizon"         # what the verdict is judged on: "horizon" or "resolution" (8C)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "horizon_hours": self.horizon_hours,
                "hypothesis": self.hypothesis, "rule": self.rule, "judged_on": self.measure,
                "status": "HYPOTHESIS (unvalidated)"}


def _clip01(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 3)


# ------------------------------------------------------------------ rules

IMBALANCE_MIN = 0.6


def _book_imbalance(m: MarketFeatures, o: TokenFeatures):
    if o.imbalance is None or abs(o.imbalance) < IMBALANCE_MIN:
        return None
    direction = "UP" if o.imbalance > 0 else "DOWN"
    return direction, _clip01((abs(o.imbalance) - IMBALANCE_MIN) / (1 - IMBALANCE_MIN)), {
        "imbalance": o.imbalance, "top_bid_usd": o.top_bid_usd, "top_ask_usd": o.top_ask_usd,
        "threshold": IMBALANCE_MIN}


REVERSION_MIN_MOVE = 0.03
REVERSION_VOL_MULT = 3.0


def _mean_reversion(m: MarketFeatures, o: TokenFeatures):
    if o.change_1h is None:
        return None
    threshold = max(REVERSION_MIN_MOVE, REVERSION_VOL_MULT * (o.volatility or 0.0))
    if abs(o.change_1h) < threshold:
        return None
    direction = "DOWN" if o.change_1h > 0 else "UP"
    return direction, _clip01(abs(o.change_1h) / (2 * threshold) ), {
        "change_1h": o.change_1h, "volatility": o.volatility, "threshold": round(threshold, 4)}


MOMENTUM_MIN_6H = 0.05
MOMENTUM_MIN_1H = 0.01


def _momentum(m: MarketFeatures, o: TokenFeatures):
    if o.change_6h is None or o.change_1h is None:
        return None
    if abs(o.change_6h) < MOMENTUM_MIN_6H or abs(o.change_1h) < MOMENTUM_MIN_1H \
            or (o.change_6h > 0) != (o.change_1h > 0):
        return None
    direction = "UP" if o.change_6h > 0 else "DOWN"
    return direction, _clip01(abs(o.change_6h) / 0.20), {
        "change_6h": o.change_6h, "change_1h": o.change_1h,
        "min_6h": MOMENTUM_MIN_6H, "min_1h": MOMENTUM_MIN_1H}


LONGSHOT_MAX = 0.10
FAVOURITE_MIN = 0.90


def _longshot_bias(m: MarketFeatures, o: TokenFeatures):
    if o.mid is None:
        return None
    if o.mid <= LONGSHOT_MAX:
        return "DOWN", _clip01((LONGSHOT_MAX - o.mid) / LONGSHOT_MAX + 0.1), {
            "mid": o.mid, "band": f"<= {LONGSHOT_MAX}", "hours_to_end": m.hours_to_end}
    if o.mid >= FAVOURITE_MIN:
        return "UP", _clip01((o.mid - FAVOURITE_MIN) / (1 - FAVOURITE_MIN) + 0.1), {
            "mid": o.mid, "band": f">= {FAVOURITE_MIN}", "hours_to_end": m.hours_to_end}
    return None


LIBRARY: tuple[Strategy, ...] = (
    Strategy("BOOK_IMBALANCE", "7B.1", 1.0,
             "Resting order-book depth heavily skewed to one side precedes a short-term drift toward "
             "the heavy side.",
             f"|imbalance| >= {IMBALANCE_MIN} on the primary outcome; direction = the heavy side",
             _book_imbalance),
    Strategy("MEAN_REVERSION", "7B.1", 6.0,
             "A sharp 1h move that is large relative to the market's own volatility is partly an "
             "over-reaction and reverses.",
             f"|1h change| >= max({REVERSION_MIN_MOVE}, {REVERSION_VOL_MULT} x volatility); direction = against the move",
             _mean_reversion),
    Strategy("MOMENTUM", "7B.1", 6.0,
             "A steady 6h trend, confirmed by the last hour, reflects information still being absorbed "
             "and continues.",
             f"|6h change| >= {MOMENTUM_MIN_6H} and 1h change >= {MOMENTUM_MIN_1H} in the same direction; "
             "direction = the trend",
             _momentum),
    Strategy("LONGSHOT_BIAS", "7B.1", 24.0,
             "Favourite-longshot bias: bettors over-pay for long shots and under-pay for heavy "
             "favourites.",
             f"primary mid <= {LONGSHOT_MAX} -> DOWN; primary mid >= {FAVOURITE_MIN} -> UP",
             _longshot_bias),
    Strategy("CALIBRATION_EDGE", "8C.1", 24.0,
             "The 8B fair-value model: a market is mispriced by its price band's measured, shrunk "
             "historical bias in its own segment (sports / field / binary).",
             "fair value - live ask - fee >= FRIDAY_EDGE_MIN and z >= FRIDAY_EDGE_MIN_Z (with the model-"
             "uncertainty floor); buy that side; once per market; judged held to resolution",
             None, measure="resolution"),
)
EDGE_STRATEGY = "CALIBRATION_EDGE"


def enabled_strategies() -> tuple[Strategy, ...]:
    """FRIDAY_QUANT_STRATEGIES: 'all' (default), 'none', or a comma list of names."""
    raw = os.getenv("FRIDAY_QUANT_STRATEGIES", "all").strip()
    if raw.lower() in ("", "all"):
        return LIBRARY
    if raw.lower() == "none":
        return ()
    wanted = {n.strip().upper() for n in raw.split(",") if n.strip()}
    unknown = wanted - {s.name for s in LIBRARY}
    if unknown:
        raise ValueError(f"unknown strategies in FRIDAY_QUANT_STRATEGIES: {sorted(unknown)}")
    return tuple(s for s in LIBRARY if s.name in wanted)


def evaluate_market(m: MarketFeatures, strategies: tuple[Strategy, ...], run_id: int | None = None,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Signals for ONE eligible market (none for ineligible markets)."""
    if not m.eligible or not m.outcomes:
        return []
    now = now or datetime.now(timezone.utc)
    o = m.outcomes[0]
    out = []
    for s in strategies:
        if s.fn is None:                 # pipeline-evaluated (CALIBRATION_EDGE)
            continue
        fired = s.fn(m, o)
        if fired is None:
            continue
        direction, strength, inputs = fired
        out.append({
            "run_id": run_id, "observed_at": o.observed_at, "strategy": s.name, "strategy_version": s.version,
            "market_id": m.market_id, "token_id": o.token_id, "outcome": o.outcome,
            "direction": direction, "strength": strength, "horizon_hours": s.horizon_hours,
            "expires_at": (now + timedelta(hours=s.horizon_hours)).isoformat(timespec="seconds"),
            "mid": o.mid, "best_bid": o.best_bid, "best_ask": o.best_ask, "half_spread": o.half_spread,
            "inputs": {**inputs, "question": m.question[:120], "attention": m.attention},
        })
    return out
