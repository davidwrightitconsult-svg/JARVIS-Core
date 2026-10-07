"""
FRIDAY Quant Core — market features (Phase 7A).

Pure, deterministic functions over FRIDAY's own recorded order-book history
(Phase 6 market store). No network, no Claude, no opinions: every number
here can be recomputed from the store and checked.

Per outcome token:
  price     best bid/ask, mid, spread, half-spread (the cost of crossing)
  depth     USD within 2c of mid; USD at the best bid / best ask; top-of-book
            imbalance (bid USD - ask USD) / (bid USD + ask USD) over the
            recorded levels, in [-1, 1]
  movement  mid change over 1h / 6h / 24h (None until history covers it);
            volatility = standard deviation of successive mid changes;
            24h range
  quality   observations in the window, usable ratio, data age, flags
Per market:
  hours to end; for complete outcome sets, the sum of best asks (buying every
  outcome costs this and pays exactly 1) and of best bids (selling every
  outcome raises this) — `ask_sum < 1` or `bid_sum > 1` would be a pricing
  inconsistency, reported as a fact, never acted on here.

Features describe the market. They are NOT signals and NOT edges: Phase 7B
turns them into candidate signals (hypotheses), 7C measures those signals
against what prices did next, and Phases 8-9 decide whether any edge is real.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis.market.models import parse_time

from .segments import segment_of

QUANT_VERSION = "7A.1"
HORIZONS_HOURS = (1, 6, 24)
HISTORY_HOURS = 25              # enough for the 24h change plus one interval


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


@dataclass
class TokenFeatures:
    market_id: str
    token_id: str
    outcome: str
    observed_at: str
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    half_spread: float | None
    depth_usd: float
    top_bid_usd: float | None
    top_ask_usd: float | None
    imbalance: float | None
    change_1h: float | None
    change_6h: float | None
    change_24h: float | None
    volatility: float | None
    range_24h: list[float] | None
    observations: int
    usable_ratio: float | None
    age_seconds: float
    flags: list[str]
    usable: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketFeatures:
    market_id: str
    question: str
    end_date: str | None
    hours_to_end: float | None
    volume_24h: float | None
    gamma_liquidity: float | None
    outcomes: list[TokenFeatures] = field(default_factory=list)
    neg_risk: bool | None = None
    segment: str = "binary"
    ask_sum: float | None = None
    bid_sum: float | None = None
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)
    attention: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["outcomes"] = [o.as_dict() for o in self.outcomes]
        return d


def change_over(history: list[dict[str, Any]], hours: float, now_mid: float | None,
                now: datetime) -> float | None:
    """Mid now minus the last usable mid at or before (now - hours).
    None when the recorded history does not reach back that far."""
    if now_mid is None:
        return None
    cutoff = now - timedelta(hours=hours)
    past = None
    for h in history:
        if h["usable"] and h["mid"] is not None and _ts(h["observed_at"]) <= cutoff:
            past = h["mid"]
        elif _ts(h["observed_at"]) > cutoff:
            break
    return None if past is None else _r(now_mid - past)


def volatility(history: list[dict[str, Any]]) -> float | None:
    """Standard deviation of successive usable mid changes (per observation)."""
    mids = [h["mid"] for h in history if h["usable"] and h["mid"] is not None]
    diffs = [b - a for a, b in zip(mids, mids[1:])]
    return _r(statistics.pstdev(diffs), 5) if len(diffs) >= 2 else None


def imbalance(levels: dict[str, list[list[float]]]) -> tuple[float | None, float | None, float | None]:
    """(top bid USD, top ask USD, imbalance over all recorded levels)."""
    bids, asks = levels.get("bids") or [], levels.get("asks") or []
    top_bid = _r(bids[0][0] * bids[0][1], 2) if bids else None
    top_ask = _r(asks[0][0] * asks[0][1], 2) if asks else None
    bid_usd = sum(p * s for p, s in bids)
    ask_usd = sum(p * s for p, s in asks)
    total = bid_usd + ask_usd
    return top_bid, top_ask, (_r((bid_usd - ask_usd) / total) if total > 0 else None)


def token_features(latest: dict[str, Any], history: list[dict[str, Any]],
                   levels: dict[str, list[list[float]]], now: datetime) -> TokenFeatures:
    window = [h for h in history if _ts(h["observed_at"]) >= now - timedelta(hours=24)] or history[-1:]
    usable_mids = [h["mid"] for h in window if h["usable"] and h["mid"] is not None]
    top_bid, top_ask, imb = imbalance(levels)
    mid = latest["mid"] if latest["usable"] else None
    return TokenFeatures(
        market_id=latest["market_id"], token_id=latest["token_id"], outcome=latest.get("outcome") or "",
        observed_at=latest["observed_at"],
        best_bid=latest["best_bid"], best_ask=latest["best_ask"], mid=latest["mid"], spread=latest["spread"],
        half_spread=_r(latest["spread"] / 2) if latest["spread"] is not None else None,
        depth_usd=latest["depth_usd"] or 0.0, top_bid_usd=top_bid, top_ask_usd=top_ask, imbalance=imb,
        change_1h=change_over(history, 1, mid, now), change_6h=change_over(history, 6, mid, now),
        change_24h=change_over(history, 24, mid, now), volatility=volatility(window),
        range_24h=[min(usable_mids), max(usable_mids)] if usable_mids else None,
        observations=len(window),
        usable_ratio=_r(sum(h["usable"] for h in window) / len(window), 2) if window else None,
        age_seconds=round(max(0.0, (now - _ts(latest["observed_at"])).total_seconds()), 1),
        flags=list(latest["flags"]), usable=bool(latest["usable"]),
    )


def market_features(rows: list[dict[str, Any]], history: dict[str, list[dict[str, Any]]],
                    levels: dict[int, dict[str, list[list[float]]]], now: datetime | None = None) -> MarketFeatures:
    """rows: the latest snapshot rows of ONE market (one per outcome, in order)."""
    now = now or datetime.now(timezone.utc)
    first = rows[0]
    end = parse_time(first.get("end_date"))
    outcomes = [token_features(r, history.get(r["token_id"], []), levels.get(r["snapshot_id"], {}), now)
                for r in rows]
    asks = [o.best_ask for o in outcomes]
    bids = [o.best_bid for o in outcomes]
    return MarketFeatures(
        market_id=first["market_id"], question=first.get("question") or "", end_date=first.get("end_date"),
        hours_to_end=_r((end - now).total_seconds() / 3600, 2) if end else None,
        volume_24h=first.get("volume_24h"), gamma_liquidity=first.get("gamma_liquidity"),
        outcomes=outcomes,
        neg_risk=None if first.get("neg_risk") is None else bool(first.get("neg_risk")),
        segment=segment_of(first.get("question"), first.get("neg_risk")),
        ask_sum=_r(sum(asks)) if len(outcomes) >= 2 and all(a is not None for a in asks) else None,
        bid_sum=_r(sum(bids)) if len(outcomes) >= 2 and all(b is not None for b in bids) else None,
    )


def attention_score(m: MarketFeatures) -> int:
    """0-100 ranking of how WATCHABLE a market is (depth, tightness,
    activity). It says nothing about value or edge."""
    if not m.outcomes:
        return 0
    depth = min(o.depth_usd for o in m.outcomes)
    spreads = [o.spread for o in m.outcomes if o.spread is not None]
    spread = max(spreads) if spreads else 1.0
    volume = m.volume_24h or 0.0
    s_depth = 40 * min(1.0, math.log10(1 + depth) / 5)          # $100k+ within 2c = full marks
    s_tight = 30 * max(0.0, 1 - spread / 0.05)                  # 5c+ spread = 0
    s_volume = 30 * min(1.0, math.log10(1 + volume) / 6)        # $1M+ 24h volume = full marks
    return int(round(s_depth + s_tight + s_volume))
