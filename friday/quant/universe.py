"""
FRIDAY Quant Core — tradable-universe rules (Phase 7A).

A market is ELIGIBLE for strategy work only if every outcome has usable,
fresh data AND the market is deep, tight and active enough that a position
could later be entered and exited without the spread eating any edge.
Every rejection carries a plain reason code, so the universe is auditable.

Eligibility is a data/liquidity gate, not a recommendation.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .features import MarketFeatures
from .segments import excluded_segments


@dataclass(frozen=True)
class UniverseRules:
    min_depth_usd: float = 500.0         # per outcome, USD within 2c of mid
    max_spread: float = 0.05             # per outcome, dollars (5c)
    min_volume_24h: float = 1000.0       # USD
    min_price: float = 0.03              # primary outcome mid
    max_price: float = 0.97
    max_age_seconds: float = 1800.0      # latest observation must be this fresh
    excluded_segments: tuple[str, ...] = ()   # 8C.2: out of scope (app default: sports)

    def __post_init__(self):
        if not (0 <= self.min_price < self.max_price <= 1):
            raise ValueError("price bounds must satisfy 0 <= min_price < max_price <= 1")
        if self.min_depth_usd < 0 or self.min_volume_24h < 0:
            raise ValueError("depth/volume minimums must be >= 0")
        if not 0 < self.max_spread <= 1:
            raise ValueError("max_spread must be in (0, 1]")
        if self.max_age_seconds < 60:
            raise ValueError("max_age_seconds must be >= 60")

    @classmethod
    def from_environment(cls) -> "UniverseRules":
        def f(name: str, default: float) -> float:
            raw = os.getenv(name, "").strip()
            return float(raw) if raw else default
        d = cls()
        return cls(
            min_depth_usd=f("FRIDAY_QUANT_MIN_DEPTH_USD", d.min_depth_usd),
            max_spread=f("FRIDAY_QUANT_MAX_SPREAD", d.max_spread),
            min_volume_24h=f("FRIDAY_QUANT_MIN_VOLUME_24H", d.min_volume_24h),
            min_price=f("FRIDAY_QUANT_MIN_PRICE", d.min_price),
            max_price=f("FRIDAY_QUANT_MAX_PRICE", d.max_price),
            max_age_seconds=f("FRIDAY_QUANT_MAX_AGE_SECONDS", d.max_age_seconds),
            excluded_segments=tuple(sorted(excluded_segments())),
        )

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate(m: MarketFeatures, rules: UniverseRules) -> list[str]:
    """Reason codes why `m` is NOT eligible; empty list = eligible."""
    reasons: list[str] = []
    if m.segment in rules.excluded_segments:
        return [f"OUT_OF_SCOPE: {m.segment}"]         # 8C.2: never eligible, whatever its liquidity
    if len(m.outcomes) < 2:
        return ["INCOMPLETE_MARKET"]
    bad = sorted({f for o in m.outcomes if not o.usable for f in o.flags} or
                 ({"UNUSABLE"} if any(not o.usable for o in m.outcomes) else set()))
    if bad:
        reasons.append("data quality: " + ", ".join(bad))
    if max(o.age_seconds for o in m.outcomes) > rules.max_age_seconds:
        reasons.append("STALE_OBSERVATION")
    if not bad:
        spread = max(o.spread for o in m.outcomes if o.spread is not None)
        if spread > rules.max_spread + 1e-9:
            reasons.append(f"SPREAD>{rules.max_spread:g}")
        if min(o.depth_usd for o in m.outcomes) < rules.min_depth_usd:
            reasons.append(f"DEPTH<{rules.min_depth_usd:g}")
        primary = m.outcomes[0].mid
        if primary is None or not rules.min_price <= primary <= rules.max_price:
            reasons.append("PRICE_EXTREME")
    if (m.volume_24h or 0) < rules.min_volume_24h:
        reasons.append(f"VOLUME<{rules.min_volume_24h:g}")
    return reasons
