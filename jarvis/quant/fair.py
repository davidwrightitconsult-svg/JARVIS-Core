"""
FRIDAY Quant Core — fair probability and edge (Phase 8B).

Turns the 8A calibration dataset into a fair-probability estimate for a live
market, and prices it against what FRIDAY could actually pay: the live ask.

Fair probability (transparent, additive, shrunk, PER SEGMENT — 8B.2):
  0. the market's segment (sports / field / binary, see segments.py); a
     segment's bias is never applied to another segment (8B.1 pooled them
     and gave four MLB games the identical "+6.17c" edge)
  1. choose the calibration horizon closest to the market's time to end
     (24h, 72h or 168h; markets ending further out use 168h). Sports always
     use 24h: a game's end date is a resolution buffer, not the event
  2. find the price bin of the primary outcome's live mid, p
  3. that bin's measured bias IN THIS SEGMENT  b = win rate - mean price
  4. shrink it by the evidence:  w = n / (n + PRIOR)   (PRIOR = 200 samples)
     fair = clamp(p + w * b)
  A bin with little data barely moves the estimate (30 samples -> w = 0.13);
  with no data fair = p and the status says so. The standard error of the
  adjustment is carried so edges can be judged against their own noise.
  For a binary market the other outcome's fair value is 1 - fair (the
  calibration table is symmetric by construction).

Edge (per outcome, per $1-payoff share):
  edge = fair - ask - fee          (fee: JARVIS_EDGE_FEE, default 0)
  z    = edge / sqrt((w * se)^2 + MODEL_SE^2)
         8C: the sampling error alone made a thick bin look near-certain
         (BTC dip NO: z 17 on jarvis020). A price band's bias is not a law
         of nature — it drifts with time and differs between markets — so
         a model-uncertainty floor (JARVIS_EDGE_MODEL_SE, default 1c) is
         added in quadrature; the purely statistical z is still reported.
  status EDGE only if edge >= JARVIS_EDGE_MIN (default 2c) AND z >= 2;
  otherwise NO_EDGE, or INSUFFICIENT_CALIBRATION when the bin has < 30.

9B — what the tables are built from. Phase 9A showed the 8A points (prices a
fixed time before the ACTUAL resolution) leak the future for markets that
resolved early. The tables now use the END-ANCHORED points: the price h
hours before the SCHEDULED end date, for markets still open then — the
same thing FRIDAY sees live (hours to the published end). Horizons are
24h, 72h, 168h, 336h and 720h; a live market uses the nearest one (by
ratio). A market more than 45 days (1080h) from its end, or less than 6h,
is OUT_OF_CALIBRATED_RANGE: FRIDAY has no evidence there and says so
instead of borrowing a table.

Sizing (numbers only — no authority): Kelly for a $1 contract bought at a
with probability q is f* = (q - a) / (1 - a); FRIDAY reports a fractional
Kelly (default 1/4) capped at JARVIS_EDGE_MAX_STAKE (default 2% of
bankroll). Nothing here places, sizes or approves anything; the Risk
Governor (Phase 10) and Decision Engine (Phase 11) own those, and every
number is labelled estimated and unvalidated until Phases 8C-9 test it.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .calibration import BIN_EDGES, MIN_BIN_SAMPLE, _bin
from .features import MarketFeatures
from .segments import SEGMENTS, segment_of

FAIR_VERSION = "9B.1"
PRIOR_SAMPLES = 200.0
HORIZONS = (24.0, 72.0, 168.0, 336.0, 720.0)
MIN_HOURS = 6.0                  # closer to the end than this: no table applies
MAX_HOURS = 1080.0               # 45 days: beyond the longest horizon x 1.5
ANCHOR = "end"
MAX_ADJUSTMENT = 0.15
LABEL = ("ESTIMATED — UNVALIDATED (recorded as CALIBRATION_EDGE and scored at resolution, Phases 8C-9); "
         "no execution authority")


@dataclass(frozen=True)
class EdgeSettings:
    fee: float = 0.0                 # $ per share, added to the ask
    min_edge: float = 0.02           # $ per share
    min_z: float = 2.0
    kelly_fraction: float = 0.25
    max_stake: float = 0.02          # fraction of bankroll
    model_se: float = 0.01           # 8C: model-uncertainty floor ($ per share), added in quadrature

    def __post_init__(self):
        if not 0 <= self.fee < 0.2 or not 0 <= self.min_edge < 0.5 or self.min_z < 0:
            raise ValueError("JARVIS_EDGE_FEE must be 0..0.2, JARVIS_EDGE_MIN 0..0.5, JARVIS_EDGE_MIN_Z >= 0")
        if not 0 <= self.model_se <= 0.2:
            raise ValueError("JARVIS_EDGE_MODEL_SE must be 0..0.2")
        if not 0 < self.kelly_fraction <= 1 or not 0 < self.max_stake <= 0.25:
            raise ValueError("JARVIS_EDGE_KELLY_FRACTION must be 0..1 and JARVIS_EDGE_MAX_STAKE 0..0.25")

    @classmethod
    def from_environment(cls) -> "EdgeSettings":
        def f(name, default):
            raw = os.getenv(name, "").strip()
            return float(raw) if raw else default
        d = cls()
        return cls(fee=f("JARVIS_EDGE_FEE", d.fee), min_edge=f("JARVIS_EDGE_MIN", d.min_edge),
                   min_z=f("JARVIS_EDGE_MIN_Z", d.min_z),
                   kelly_fraction=f("JARVIS_EDGE_KELLY_FRACTION", d.kelly_fraction),
                   max_stake=f("JARVIS_EDGE_MAX_STAKE", d.max_stake),
                   model_se=f("JARVIS_EDGE_MODEL_SE", d.model_se))

    def as_dict(self) -> dict[str, float]:
        return {"fee": self.fee, "min_edge": self.min_edge, "min_z": self.min_z,
                "kelly_fraction": self.kelly_fraction, "max_stake": self.max_stake, "model_se": self.model_se}


def _r(x, d=4):
    return None if x is None else round(x, d)


def bin_table(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per bin: samples, mean price, win rate, bias and its standard error."""
    out = []
    for i in range(len(BIN_EDGES) - 1):
        rows = [p for p in points if _bin(p["price"]) == i]
        n = len(rows)
        if n:
            mean = sum(r["price"] for r in rows) / n
            rate = sum(1 for r in rows if r["payoff"] >= 0.5) / n
            se = math.sqrt(max(rate * (1 - rate), 1.0 / n) / n)       # never 0 for all-win/all-lose bins
            out.append({"n": n, "mean_price": mean, "win_rate": rate, "bias": rate - mean, "se": se})
        else:
            out.append({"n": 0, "mean_price": None, "win_rate": None, "bias": 0.0, "se": None})
    return out


def shrunk_fair(b: dict[str, Any], price: float) -> tuple[float, float, float, float | None]:
    """(fair, weight, adjustment, adjustment_se) from one bin's statistics. Pure:
    the live model and the Phase 9 backtest share this exact arithmetic."""
    n = b["n"]
    w = n / (n + PRIOR_SAMPLES) if n else 0.0
    adj = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, w * b["bias"]))
    return min(0.999, max(0.001, price + adj)), w, adj, (w * b["se"] if n else None)


def kelly(q: float, a: float) -> float:
    """Full Kelly fraction for buying a $1 contract at price a with probability q."""
    if a <= 0 or a >= 1 or q <= a:
        return 0.0
    return (q - a) / (1 - a)


class FairValueModel:
    """Calibration tables per (segment, horizon), cached (rebuilt at most every 10 min)."""

    def __init__(self, store, settings: EdgeSettings | None = None, cache_seconds: float = 600):
        self.store = store
        self.settings = settings or EdgeSettings()
        self.cache_seconds = float(cache_seconds)
        self._tables: dict[tuple[str, float], list[dict[str, Any]]] = {}
        self._built = 0.0
        self._lock = threading.Lock()

    def tables(self) -> dict[tuple[str, float], list[dict[str, Any]]]:
        with self._lock:
            if not self._tables or time.monotonic() - self._built > self.cache_seconds:
                tables = {}
                for h in HORIZONS:
                    points = self.store.calibration_points(h, anchor=ANCHOR)
                    for seg in SEGMENTS:
                        tables[(seg, h)] = bin_table(
                            [p for p in points if segment_of(p.get("question"), p.get("neg_risk")) == seg])
                self._tables = tables
                self._built = time.monotonic()
            return self._tables

    @staticmethod
    def horizon_for(hours_to_end: float | None, segment: str = "binary") -> float | None:
        """Nearest calibrated horizon (by ratio), or None outside the calibrated range."""
        if segment == "sports":
            return HORIZONS[0]
        if hours_to_end is None or not MIN_HOURS <= hours_to_end <= MAX_HOURS:
            return None
        return min(HORIZONS, key=lambda h: abs(math.log(h / hours_to_end)))

    def fair(self, price: float, hours_to_end: float | None, segment: str = "binary") -> dict[str, Any]:
        h = self.horizon_for(hours_to_end, segment)
        if h is None:
            i = _bin(price)
            return {"segment": segment, "horizon_hours": None, "bin": f"{BIN_EDGES[i]:.2f}-{BIN_EDGES[i + 1]:.2f}",
                    "bin_samples": 0, "bin_bias": None, "weight": 0.0, "adjustment": 0.0, "adjustment_se": None,
                    "fair": _r(price), "in_range": False}
        b = self.tables()[(segment, h)][_bin(price)]
        n = b["n"]
        fair, w, adj, _ = shrunk_fair(b, price)
        i = _bin(price)
        return {"segment": segment, "horizon_hours": h, "bin": f"{BIN_EDGES[i]:.2f}-{BIN_EDGES[i + 1]:.2f}",
                "bin_samples": n,
                "bin_bias": _r(b["bias"]) if n else None, "weight": _r(w, 3), "adjustment": _r(adj),
                "adjustment_se": _r(w * b["se"], 5) if n else None, "fair": _r(fair), "in_range": True}

    def evaluate(self, m: MarketFeatures) -> dict[str, Any] | None:
        """Fair value and edge for one eligible binary market, else None."""
        if not m.eligible or len(m.outcomes) != 2:
            return None
        s = self.settings
        o0, o1 = m.outcomes
        if o0.mid is None or o0.best_ask is None or o1.best_ask is None:
            return None
        base = self.fair(o0.mid, m.hours_to_end, m.segment)
        fairs = (base["fair"], _r(1 - base["fair"]))
        se = base["adjustment_se"]
        total_se = math.sqrt(se ** 2 + s.model_se ** 2) if se else None
        sides = []
        for o, q in zip((o0, o1), fairs):
            edge = q - o.best_ask - s.fee
            z = edge / total_se if total_se else None
            z_stat = edge / se if se else None
            full = kelly(q, o.best_ask + s.fee)
            sides.append({"outcome": o.outcome, "token_id": o.token_id, "fair": q, "ask": o.best_ask,
                          "bid": o.best_bid, "edge": _r(edge), "z": _r(z, 2), "z_statistical": _r(z_stat, 2), "kelly_full": _r(full),
                          "stake_fraction": _r(min(s.max_stake, s.kelly_fraction * full)),
                          "top_ask_usd": o.top_ask_usd})
        best = max(sides, key=lambda x: x["edge"])
        if not base["in_range"]:
            status = "OUT_OF_CALIBRATED_RANGE"
        elif base["bin_samples"] < MIN_BIN_SAMPLE:
            status = "INSUFFICIENT_CALIBRATION"
        elif best["edge"] >= s.min_edge and best["z"] is not None and best["z"] >= s.min_z:
            status = "EDGE"
        else:
            status = "NO_EDGE"
        return {"fair_version": FAIR_VERSION, "market_id": m.market_id, "question": m.question,
                "hours_to_end": m.hours_to_end, "price": o0.mid, **base, "sides": sides,
                "best_side": best["outcome"], "best_token_id": best["token_id"], "edge": best["edge"], "z": best["z"],
                "z_statistical": best["z_statistical"], "total_se": _r(total_se, 5),
                "stake_fraction": best["stake_fraction"] if status == "EDGE" else 0.0,
                "status": status, "label": LABEL}

    def edges(self, markets: list[MarketFeatures]) -> list[dict[str, Any]]:
        out = [e for e in (self.evaluate(m) for m in markets) if e is not None]
        out.sort(key=lambda e: (e["status"] != "EDGE", -(e["edge"] or 0)))
        return out

    def status(self) -> dict[str, Any]:
        tables = self.tables()
        by_h: dict[str, int] = {}
        for (seg, h), t in tables.items():
            by_h[f"{h:g}h"] = by_h.get(f"{h:g}h", 0) + sum(b["n"] for b in t)
        anchor = self.store.end_anchor_stats() if hasattr(self.store, "end_anchor_stats") else {}
        return {"fair_version": FAIR_VERSION, "settings": self.settings.as_dict(), "prior_samples": PRIOR_SAMPLES,
                "anchor": "scheduled end date", "calibrated_hours": [MIN_HOURS, MAX_HOURS],
                "reanchor_pending": anchor.get("pending"),
                "samples_by_horizon": by_h,
                "samples_by_segment": {f"{seg}/{h:g}h": sum(b["n"] for b in t) for (seg, h), t in tables.items()}}
