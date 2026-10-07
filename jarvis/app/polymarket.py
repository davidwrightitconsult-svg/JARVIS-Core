"""
Polymarket client used by the FRIDAY app (MARKET_FACTORY).

Phase 6A: the Phase 0 implementation (which read bids[0]/asks[0] — the
WORST prices, since Polymarket lists levels worst-first — summed share
counts as "liquidity", and silently dropped any market that errored) is
replaced by the Market Data Engine in friday.market. The class name is
kept so existing wiring and tests keep working.

Phase 6B: every snapshot is also recorded in the market store
(JARVIS_MARKET_STORE, JARVIS_MARKET_DB, retention settings). A store that
cannot be opened is reported via `store_status` and FRIDAY runs without it.

Phase 6C: decided / past-end markets are excluded from the scan universe
(JARVIS_MARKET_EXCLUDE_SETTLED, JARVIS_MARKET_NEAR_CERTAIN,
JARVIS_MARKET_PAST_END_HOURS).
"""

import os

from jarvis.market import PolymarketData
from jarvis.market.client import MAX_CANDIDATES
from jarvis.market.models import NEAR_CERTAIN, PAST_END_HOURS
from jarvis.market.store import (
    DEFAULT_BOOK_RETENTION_DAYS,
    DEFAULT_MARKET_DB,
    DEFAULT_RETENTION_DAYS,
    MarketStore,
)

from .config import GAMMA_HOST, POLYMARKET_HOST


def build_market_store() -> tuple[MarketStore | None, dict]:
    if os.getenv("JARVIS_MARKET_STORE", "true").strip().lower() in ("0", "false", "no", "off"):
        return None, {"enabled": False, "reason": "disabled by JARVIS_MARKET_STORE"}
    path = os.getenv("JARVIS_MARKET_DB", DEFAULT_MARKET_DB).strip() or DEFAULT_MARKET_DB
    try:
        store = MarketStore(
            path,
            retention_days=int(os.getenv("JARVIS_MARKET_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
            book_retention_days=int(os.getenv("JARVIS_MARKET_BOOK_RETENTION_DAYS", DEFAULT_BOOK_RETENTION_DAYS)),
        )
    except Exception as exc:
        return None, {"enabled": False, "path": path, "reason": f"{type(exc).__name__}: {exc}"[:300]}
    return store, {"enabled": True, "path": str(store.path)}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


def market_filter_settings() -> dict:
    """Validated 6C filter settings from the environment (ValueError if bad)."""
    near = float(os.getenv("JARVIS_MARKET_NEAR_CERTAIN", "").strip() or NEAR_CERTAIN)
    past = float(os.getenv("JARVIS_MARKET_PAST_END_HOURS", "").strip() or PAST_END_HOURS)
    if not 0.9 <= near < 1:
        raise ValueError("JARVIS_MARKET_NEAR_CERTAIN must be between 0.90 and 0.999")
    if past < 0:
        raise ValueError("JARVIS_MARKET_PAST_END_HOURS must be >= 0")
    cands = int(os.getenv("JARVIS_MARKET_MAX_CANDIDATES", "").strip() or MAX_CANDIDATES)
    if not 50 <= cands <= 5000:
        raise ValueError("JARVIS_MARKET_MAX_CANDIDATES must be between 50 and 5000")
    from jarvis.market.segments import excluded_segments
    return {"exclude_settled": _flag("JARVIS_MARKET_EXCLUDE_SETTLED", True),
            "near_certain": near, "past_end_hours": past, "max_candidates": cands,
            "exclude_segments": sorted(excluded_segments())}       # 8C.2: scope (default: sports out)


class PolyMarketClient(PolymarketData):
    def __init__(self):
        store, self.store_status = build_market_store()
        try:
            self.filter_status = market_filter_settings()
        except ValueError as exc:
            # A bad setting falls back to the safe defaults, and says so.
            self.filter_status = {"exclude_settled": True, "near_certain": NEAR_CERTAIN,
                                  "past_end_hours": PAST_END_HOURS, "max_candidates": MAX_CANDIDATES,
                                  "exclude_segments": ["micro", "sports"], "error": str(exc)}
        settings = {k: v for k, v in self.filter_status.items() if k != "error"}
        super().__init__(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST, store=store,
                         book_levels=int(os.getenv("JARVIS_MARKET_BOOK_LEVELS", "5")), **settings)
        try:
            self.watchlist_max = max(0, min(int(os.getenv("JARVIS_MARKET_WATCHLIST_MAX", "50") or 50), 500))
        except ValueError:
            self.watchlist_max = 50
