"""COURIER — the leaderboard analyzer.

Fetches top traders from Polymarket's public leaderboard API.
Optionally provides "copy-trade" signals by mirroring their positions.

NOTE: This is READ-ONLY analysis. Actual copy-trading requires
the user to have their own broker credentials and is deliberately
NOT automated here to avoid the platform placing unauthorized trades.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


POLYMARKET_LEADERBOARD_URL = "https://lb-api.polymarket.com/leaderboard"

_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache: dict[str, Any] = {"at": 0, "data": None}


@dataclass
class TopTrader:
    rank: int
    address: str
    username: str | None
    volume_usd: float
    pnl_usd: float
    roi: float


class Courier:
    """Fetches and caches Polymarket leaderboard data."""

    def fetch_top_traders(
        self,
        limit: int = 10,
        window: str = "all",  # 'all', '30d', '7d', '1d'
    ) -> list[TopTrader]:
        now = time.monotonic()
        if (
            _cache["data"] is not None
            and now - _cache["at"] < _CACHE_TTL_SECONDS
            and _cache["data"].get("window") == window
            and _cache["data"].get("limit") == limit
        ):
            return _cache["data"]["traders"]

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    POLYMARKET_LEADERBOARD_URL,
                    params={
                        "window": window,
                        "limit": limit,
                        "orderBy": "pnl",
                    },
                )
                if resp.status_code != 200:
                    return []

                raw = resp.json()
                traders: list[TopTrader] = []
                for i, row in enumerate(raw[:limit]):
                    traders.append(
                        TopTrader(
                            rank=i + 1,
                            address=row.get("proxyWallet", ""),
                            username=row.get("name"),
                            volume_usd=float(row.get("vol", 0) or 0),
                            pnl_usd=float(row.get("pnl", 0) or 0),
                            roi=float(row.get("roi", 0) or 0),
                        )
                    )

                _cache["at"] = now
                _cache["data"] = {
                    "window": window,
                    "limit": limit,
                    "traders": traders,
                }
                return traders

        except Exception:
            return []

    def describe_top_trader(self, window: str = "all") -> str:
        """Return a verbal summary of the top trader (for Alfred)."""
        traders = self.fetch_top_traders(limit=3, window=window)
        if not traders:
            return (
                "I am afraid the leaderboard is unavailable at present, sir. "
                "No copy-trade target can be identified."
            )

        top = traders[0]
        name = top.username or top.address[:8] + "..."
        return (
            f"The top trader on Polymarket (window: {window}) is {name}, "
            f"with ${top.pnl_usd:,.2f} in profit and "
            f"a return of {top.roi * 100:.1f} percent. "
            f"I would note, sir, that past performance is no guarantee "
            f"of future results. Which I am sure you already know."
        )


# ---- Singleton ----

_courier: Courier | None = None


def get_courier() -> Courier:
    global _courier
    if _courier is None:
        _courier = Courier()
    return _courier
