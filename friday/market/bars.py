"""
FRIDAY hourly bars (Phase 11B): one price per hour for each resolved market,
so the book strategies can be tested on what really happened.

Read-only against Polymarket (public price history; no account, no orders).
For each resolved two-outcome market already in the calibration set, the
builder reads the last `days` before the outcome became known, in one-week
requests (the venue refuses long ranges), and stores a regular hourly series.

- A bar is the last price the venue reported in that hour.
- An hour with no report repeats the previous price (nothing traded, so the
  price did not change). The count of hours really reported is kept, and a
  series with too few is marked THIN and never tested.
- The series stops when the outcome became known. Nothing after that is kept.

Safe to stop and rerun: a market is fetched once.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .segments import excluded_segments, segment_of

BARS_VERSION = "11C.1"
CHUNK_HOURS = 168            # one request = one week of hourly prices
DEFAULT_DAYS = 30
MIN_OBSERVED = 100           # hours with a reported price needed for a series to be tested
MIN_BARS = 150               # and at least this many hours from first to last


def regular(points: list[tuple[int, float]], end_hour: int) -> tuple[list[tuple[int, float]], int]:
    """(hourly series, hours observed). Buckets by hour (last price wins), fills gaps forward,
    stops at `end_hour` inclusive."""
    by_hour: dict[int, float] = {}
    for ts, price in sorted(points):
        h = int(ts) // 3600
        if h <= end_hour and 0.0 <= price <= 1.0:
            by_hour[h] = float(price)
    if not by_hour:
        return [], 0
    hours = sorted(by_hour)
    series, last = [], by_hour[hours[0]]
    for h in range(hours[0], hours[-1] + 1):
        last = by_hour.get(h, last)
        series.append((h, last))
    return series, len(hours)


class BarBuilder:
    def __init__(self, client, store, days: int = DEFAULT_DAYS, pace_seconds: float = 0.05,
                 min_volume: float = 0, exclude: frozenset[str] | None = None):
        self.client, self.store = client, store
        self.days = max(7, int(days))
        self.pace_seconds = max(0.0, float(pace_seconds))
        self.min_volume = float(min_volume)
        self.exclude = excluded_segments() if exclude is None else frozenset(exclude)

    def _history(self, token_id: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
        try:
            return self.client.price_history(token_id, start_ts, end_ts)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404, 422):    # a refused range is final: no data there
                return []
            raise
        finally:
            if self.pace_seconds:
                time.sleep(self.pace_seconds)

    def fetch(self, market: dict[str, Any]) -> tuple[list[tuple[int, float]], int, str]:
        ref = datetime.fromisoformat(str(market["reference_time"]).replace("Z", "+00:00"))
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        end_ts = int(ref.timestamp())
        start_ts = end_ts - self.days * 86400
        points: list[tuple[int, float]] = []
        hi = end_ts
        while hi > start_ts:
            lo = max(start_ts, hi - CHUNK_HOURS * 3600)
            chunk = self._history(market["token_id"], lo, hi)
            points.extend(chunk)
            if not chunk and points:           # nothing older: the market had not started trading
                break
            hi = lo
        series, observed = regular(points, end_ts // 3600)
        if not series:
            return [], 0, "EMPTY"
        status = "OK" if observed >= MIN_OBSERVED and len(series) >= MIN_BARS else "THIN"
        return series, observed, status

    def run(self, max_markets: int = 500, deadline_seconds: float | None = None, progress=None) -> dict[str, Any]:
        started = time.monotonic()
        summary = {"bars_version": BARS_VERSION, "days": self.days, "fetched": 0, "ok": 0, "thin": 0, "empty": 0,
                   "out_of_scope": 0, "errors": 0, "stopped": "no markets left"}
        while summary["fetched"] + summary["out_of_scope"] < max_markets:
            batch = self.store.bars_pending(min(200, max_markets - summary["fetched"] - summary["out_of_scope"]),
                                            self.min_volume)
            if not batch:
                break
            for m in batch:
                if deadline_seconds is not None and time.monotonic() - started > deadline_seconds:
                    summary["stopped"] = "deadline"
                    return summary
                if segment_of(m.get("question"), m.get("neg_risk")) in self.exclude:
                    self.store.record_bars(m, [], 0, "OUT_OF_SCOPE")
                    summary["out_of_scope"] += 1
                    continue
                try:
                    series, observed, status = self.fetch(m)
                except (httpx.HTTPError, ValueError) as exc:
                    summary["errors"] += 1
                    summary["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    if summary["errors"] >= 20:
                        summary["stopped"] = "too many errors"
                        return summary
                    time.sleep(2.0)
                    continue                     # left pending; a rerun tries it again
                if not self.store.record_bars(m, series, observed, status):
                    summary["already_stored"] = summary.get("already_stored", 0) + 1
                    continue                     # a second builder got there first; nothing lost
                summary["fetched"] += 1
                summary[status.lower()] += 1
                if progress and summary["fetched"] % 50 == 0:
                    progress(summary)
        else:
            summary["stopped"] = "market limit"
        return summary
