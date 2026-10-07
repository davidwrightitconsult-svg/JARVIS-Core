"""
FRIDAY Quant Core engine (Phase 7A): recorded history -> features -> universe.

Reads ONLY the Phase 6 market store (no Polymarket calls, no Claude). The
UI scan, the API, the CLI and Claude's market context all use this one
engine, so every consumer sees the same numbers.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from itertools import groupby
from typing import Any

from friday.market.store import MarketStore

from .features import HISTORY_HOURS, QUANT_VERSION, MarketFeatures, attention_score, market_features
from .universe import UniverseRules, evaluate


class QuantEngine:
    def __init__(self, store: MarketStore, rules: UniverseRules | None = None):
        self.store = store
        self.rules = rules or UniverseRules()

    def _build(self, rows: list[dict[str, Any]], now: datetime | None) -> list[MarketFeatures]:
        now = now or datetime.now(timezone.utc)
        if not rows:
            return []
        history = self.store.history_bulk([r["token_id"] for r in rows], hours=HISTORY_HOURS)
        levels = self.store.levels_bulk([r["snapshot_id"] for r in rows])
        out = []
        for _, group in groupby(rows, key=lambda r: r["market_id"]):
            m = market_features(list(group), history, levels, now)
            m.reasons = evaluate(m, self.rules)
            m.eligible = not m.reasons
            m.attention = attention_score(m) if m.eligible else 0
            out.append(m)
        return out

    def features(self, market_ids: list[str] | None = None, now: datetime | None = None) -> list[MarketFeatures]:
        """Markets of the latest run (default) or the given markets' latest
        observations, eligible first, then by attention."""
        rows = self.store.latest_rows(market_ids)
        markets = self._build(rows, now)
        markets.sort(key=lambda m: (not m.eligible, -m.attention, -(m.volume_24h or 0)))
        return markets

    def universe(self, now: datetime | None = None) -> dict[str, Any]:
        run = self.store.latest_run()
        markets = self.features(now=now)
        rejected: Counter = Counter()
        for m in markets:
            for reason in m.reasons:
                if reason.startswith("data quality: "):
                    for flag in reason[len("data quality: "):].split(", "):
                        rejected[f"DATA_{flag}"] += 1
                else:
                    rejected[reason] += 1
        return {
            "quant_version": QUANT_VERSION,
            "run": run,
            "markets": len(markets),
            "eligible": sum(m.eligible for m in markets),
            "rejected_by": dict(rejected.most_common()),
            "rules": self.rules.as_dict(),
            "eligible_markets": [{"market_id": m.market_id, "question": m.question, "attention": m.attention}
                                 for m in markets if m.eligible][:50],
            "note": "Eligibility is a data/liquidity gate. It is not a signal and not an edge.",
        }

    def status(self) -> dict[str, Any]:
        return {"quant_version": QUANT_VERSION, "rules": self.rules.as_dict()}
