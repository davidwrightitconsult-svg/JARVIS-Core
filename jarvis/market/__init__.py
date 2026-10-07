"""
FRIDAY Market Data Engine (Phase 6). Read-only Polymarket data with
correct best bid/ask, USD depth and data-quality flags.
"""

from .client import DEFAULT_CLOB, DEFAULT_GAMMA, MarketDataError, PolymarketData, SnapshotReport
from .models import (
    HARD_FLAGS,
    MARKET_DATA_VERSION,
    Book,
    Market,
    Outcome,
    parse_book,
    parse_market,
    parse_time,
    quality_flags,
    settlement_reason,
    snapshot_rows,
)

__all__ = [
    "DEFAULT_CLOB", "DEFAULT_GAMMA", "HARD_FLAGS", "MARKET_DATA_VERSION", "Book", "Market",
    "MarketDataError", "Outcome", "PolymarketData", "SnapshotReport", "parse_book", "parse_market",
    "parse_time", "quality_flags", "settlement_reason", "snapshot_rows",
]
