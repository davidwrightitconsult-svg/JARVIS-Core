"""
FRIDAY Polymarket market-data client (Phase 6A; settled-market filter 6C). Read-only.

- Gamma  GET /markets   active, open markets, highest 24h volume first
- CLOB   POST /books    order books in batches (falls back to GET /book
                        per token if the batch endpoint is unavailable)
- Nothing here can place, cancel or sign anything: there are no
  credentials, and only GET/POST-for-read endpoints are called.
- 6C: markets that are effectively decided (NEAR_CERTAIN) or long past
  their end date (PAST_END) are excluded from the scan universe — counted
  and named in the report, never silently dropped — and the freed slots
  are filled from further down Gamma's volume ranking (paged with
  `offset`, up to max_candidates markets; on a busy sports day most of the
  top-volume markets are finished games).
- Errors are COUNTED and reported with the snapshot (never silently
  swallowed, as the Phase 0 client did).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import (
    MARKET_DATA_VERSION,
    NEAR_CERTAIN,
    PAST_END_HOURS,
    Book,
    Market,
    parse_book,
    parse_market,
    settlement_reason,
    snapshot_rows,
)

DEFAULT_CLOB = "https://clob.polymarket.com"
DEFAULT_GAMMA = "https://gamma-api.polymarket.com"
BOOK_BATCH = 20
MAX_GAMMA_LIMIT = 200
MAX_CANDIDATES = 600          # 6C fix: how far down Gamma's volume ranking to look
USER_AGENT = f"FRIDAY-market-data/{MARKET_DATA_VERSION} (read-only)"


class MarketDataError(RuntimeError):
    pass


@dataclass
class SnapshotReport:
    markets: int = 0
    tokens: int = 0
    books_ok: int = 0
    books_missing: int = 0
    usable_rows: int = 0
    batch_endpoint: bool = True
    errors: list[str] = field(default_factory=list)
    observed_at: str = ""
    stored_run: int | None = None         # 6B: run id in the market store
    candidates: int = 0                   # 6C: markets Gamma returned
    excluded: int = 0                     # 6C: settled / past-end markets skipped
    excluded_reasons: dict[str, int] = field(default_factory=dict)
    excluded_examples: list[str] = field(default_factory=list)
    tracked: int = 0                      # 7B: markets observed via the watchlist
    quant: dict[str, Any] = field(default_factory=dict)   # 7B: signal pipeline summary

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class PolymarketData:
    def __init__(self, clob_host: str = DEFAULT_CLOB, gamma_host: str = DEFAULT_GAMMA,
                 timeout: float = 15.0, transport: httpx.BaseTransport | None = None,
                 store: Any = None, book_levels: int = 5, exclude_settled: bool = True,
                 near_certain: float = NEAR_CERTAIN, past_end_hours: float = PAST_END_HOURS,
                 max_candidates: int = MAX_CANDIDATES, watchlist_max: int = 50,
                 exclude_segments: Any = ()):
        self.clob = clob_host.rstrip("/")
        self.gamma = gamma_host.rstrip("/")
        self.client = httpx.Client(timeout=timeout, transport=transport,
                                   headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._batch_ok = True
        self.last_report: SnapshotReport | None = None
        self.store = store                     # MarketStore (6B) or None
        self.book_levels = max(0, min(int(book_levels), 20))
        if not 0.5 < float(near_certain) < 1:
            raise ValueError("near_certain must be between 0.5 and 1")
        if float(past_end_hours) < 0:
            raise ValueError("past_end_hours must be >= 0")
        self.exclude_settled = bool(exclude_settled)
        self.near_certain = float(near_certain)
        self.past_end_hours = float(past_end_hours)
        self.max_candidates = max(1, min(int(max_candidates), 5000))
        self.watchlist_max = max(0, min(int(watchlist_max), 500))
        # 8C.2: segments out of scope (the app passes FRIDAY_EXCLUDED_SEGMENTS, default sports).
        # They are dropped before any order book is fetched and never stored.
        self.exclude_segments = frozenset(exclude_segments or ())
        # 7B hooks, wired by the app: tracked markets to keep observing, and
        # observers called with the run id after each stored snapshot.
        self.tracked_source: Any = None
        self.observers: list[Any] = []

    # ------------------------------------------------------------ markets

    def markets(self, limit: int = 25, order: str = "volume24hr") -> list[Market]:
        return self._market_page(limit, 0, order)[0]

    def _market_page(self, limit: int, offset: int = 0,
                     order: str = "volume24hr") -> tuple[list[Market], int]:
        """(tradable markets, number of raw items Gamma returned)."""
        limit = max(1, min(int(limit), MAX_GAMMA_LIMIT))
        params = {"limit": limit, "active": "true", "closed": "false", "order": order, "ascending": "false"}
        if offset > 0:
            params["offset"] = int(offset)
        response = self.client.get(f"{self.gamma}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
        parsed = [parse_market(item) for item in items]
        return [m for m in parsed if m is not None and m.tradable_book][:limit], len(items)

    # ------------------------------------------------------------ books

    def books(self, token_ids: list[str], report: SnapshotReport | None = None) -> dict[str, Book | None]:
        result: dict[str, Book | None] = {t: None for t in token_ids}
        for start in range(0, len(token_ids), BOOK_BATCH):
            chunk = token_ids[start:start + BOOK_BATCH]
            if self._batch_ok:
                try:
                    response = self.client.post(f"{self.clob}/books",
                                                json=[{"token_id": t} for t in chunk])
                    if response.status_code in (404, 405):
                        self._batch_ok = False          # endpoint gone: use per-token GET
                    else:
                        response.raise_for_status()
                        for raw in response.json() or []:
                            if isinstance(raw, dict):
                                book = parse_book(raw)
                                if book.token_id in result:
                                    result[book.token_id] = book
                        continue
                except (httpx.HTTPError, ValueError) as exc:
                    if report is not None:
                        report.errors.append(f"books batch: {type(exc).__name__}: {str(exc)[:120]}")
            for token in chunk:
                if result[token] is not None:
                    continue
                try:
                    response = self.client.get(f"{self.clob}/book", params={"token_id": token})
                    response.raise_for_status()
                    result[token] = parse_book(response.json(), token)
                except (httpx.HTTPError, ValueError) as exc:
                    if report is not None:
                        report.errors.append(f"book {token[:10]}…: {type(exc).__name__}: {str(exc)[:120]}")
        if report is not None:
            report.batch_endpoint = self._batch_ok
        return result

    # ------------------------------------------------------------ snapshot

    def _observe(self, page: list[Market], now: datetime, report: SnapshotReport
                 ) -> tuple[list[tuple[Market, list]], list[tuple[Market, str]]]:
        """Fetch books for `page`; return (kept, excluded-with-reason)."""
        tokens = [o.token_id for m in page for o in m.outcomes]
        report.tokens += len(tokens)
        errors_before = len(report.errors)
        books = self.books(tokens, report)
        page_clean = len(report.errors) == errors_before
        report.books_ok += sum(1 for b in books.values() if b is not None)
        kept, excluded = [], []
        for m in page:
            objects = snapshot_rows(m, books, now=now, levels=self.book_levels)
            reason = settlement_reason(m, objects, now, self.near_certain, self.past_end_hours) \
                if self.exclude_settled else None
            # 7A: a market Polymarket lists but whose outcomes have NO order
            # book at all (none returned, and no fetch error on this page)
            # cannot be observed; it only wastes a scan slot. A fetch
            # failure is never mistaken for this: the market is kept and
            # flagged, and the error is reported.
            if reason is None and self.exclude_settled and page_clean \
                    and all(r.flags == ["NO_BOOK"] for r in objects):
                reason = "NO_BOOK"
            if reason:
                excluded.append((m, reason))
            else:
                kept.append((m, objects))
        return kept, excluded

    def _in_scope(self, markets: list[Market], report: SnapshotReport, excluded_ids: set[str]) -> list[Market]:
        """8C.2: drop markets of an excluded segment (counted as SEGMENT_<NAME>)."""
        if not self.exclude_segments:
            return markets
        out, dropped = [], []
        for m in markets:
            seg = getattr(m, "segment", None)
            if seg in self.exclude_segments:
                dropped.append((m, f"SEGMENT_{seg.upper()}"))
            else:
                out.append(m)
        excluded_ids.update(m.market_id for m, _ in dropped)
        self._count_excluded(report, dropped)
        return out

    @staticmethod
    def _count_excluded(report: SnapshotReport, excluded: list[tuple[Market, str]]) -> None:
        for m, reason in excluded:
            report.excluded += 1
            report.excluded_reasons[reason] = report.excluded_reasons.get(reason, 0) + 1
            if len(report.excluded_examples) < 5:
                report.excluded_examples.append(f"{reason}: {m.question[:90]}")

    def snapshot(self, limit: int = 25) -> tuple[list[dict[str, Any]], SnapshotReport]:
        """(rows, report). Rows are per outcome token, with quality flags.
        `limit` counts MARKETS kept after the settled-market filter; the
        7B watchlist adds tracked markets on top."""
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        report = SnapshotReport(observed_at=now.isoformat(timespec="seconds"))
        limit = max(1, min(int(limit), MAX_GAMMA_LIMIT))
        # Over-fetch so excluded (decided) markets do not shrink the scan,
        # and page further down the ranking while slots are still free.
        filtering = self.exclude_settled or bool(self.exclude_segments)
        page_size = min(MAX_GAMMA_LIMIT, limit * 2) if filtering else limit
        kept: list[tuple[Market, list]] = []
        candidates: list[Market] = []
        seen: set[str] = set()
        excluded_ids: set[str] = set()
        cursor, offset, more = 0, 0, True
        while len(kept) < limit:
            if cursor >= len(candidates):
                if not more or offset >= self.max_candidates:
                    break
                batch, raw = self._market_page(page_size, offset)
                offset += raw
                more = filtering and raw >= page_size
                fresh = [m for m in batch if m.market_id not in seen]
                seen.update(m.market_id for m in fresh)
                fresh = self._in_scope(fresh, report, excluded_ids)
                candidates.extend(fresh)
                if not fresh:
                    continue
            # Books are fetched page by page: only as many markets as are
            # still needed, so the over-fetch costs no extra book requests
            # unless markets were actually excluded.
            page = candidates[cursor: cursor + (limit - len(kept))]
            cursor += len(page)
            got, excluded = self._observe(page, now, report)
            kept.extend(got)
            excluded_ids.update(m.market_id for m, _ in excluded)
            self._count_excluded(report, excluded)
        report.candidates = cursor
        # 7B: sticky watchlist — markets that were recently eligible keep
        # being observed even after they drop out of the top-volume ranking,
        # so strategies (and 7C's outcome tracking) get continuous history.
        unwatch: list[str] = []
        if self.tracked_source is not None:
            try:
                tracked = list(self.tracked_source())
            except Exception as exc:
                tracked = []
                report.errors.append(f"watchlist: {type(exc).__name__}: {str(exc)[:120]}")
            have = {m.market_id for m, _ in kept}
            unwatch.extend(m for m in (t.market_id for t in tracked) if m in excluded_ids)
            extra = [t for t in tracked if t.market_id not in have and t.market_id not in excluded_ids]
            before = {t.market_id for t in extra}
            extra = self._in_scope(extra, report, excluded_ids)
            unwatch.extend(before - {t.market_id for t in extra})
            extra = extra[: self.watchlist_max]
            for start in range(0, len(extra), 25):
                got, excluded = self._observe(extra[start:start + 25], now, report)
                kept.extend(got)
                report.tracked += len(got)
                unwatch.extend(m.market_id for m, _ in excluded)
                self._count_excluded(report, excluded)
        report.markets = len(kept)
        report.books_missing = report.tokens - report.books_ok
        markets = [m for m, _ in kept]
        objects = [row for _, rows in kept for row in rows]
        rows = [row.as_dict() for row in objects]
        report.usable_rows = sum(1 for r in rows if r["usable"])
        report.errors = report.errors[:10]
        if self.store is not None:
            # History is a by-product of observing; a storage problem is
            # reported, but never breaks the scan itself.
            try:
                if unwatch and hasattr(self.store, "unwatch"):
                    self.store.unwatch(unwatch)
                report.stored_run = self.store.record(
                    markets, objects, report.as_dict(),
                    duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                report.errors.append(f"store: {type(exc).__name__}: {str(exc)[:120]}")
        # 7B: observers (the Quant pipeline) run after a stored run; their
        # failures are reported and never break the scan.
        if report.stored_run is not None:
            for observer in list(self.observers):
                try:
                    result = observer(report.stored_run)
                    if isinstance(result, dict):
                        report.quant = result
                except Exception as exc:
                    report.errors.append(f"observer: {type(exc).__name__}: {str(exc)[:120]}")
        self.last_report = report
        return rows, report

    # Hermes MarketDataSource protocol (MARKET_SCAN) and /api/scan.
    def snapshots(self, limit: int = 25) -> list[dict[str, Any]]:
        """`limit` counts ROWS (Hermes slices to it); binary markets give 2."""
        rows, _ = self.snapshot(max(1, (int(limit) + 1) // 2))
        return rows[: int(limit)]

    # ------------------------------------------------------------ 7C: resolutions

    def market_resolutions(self, market_ids: list[str], deadline_seconds: float = 30.0) -> list[dict[str, Any]]:
        """Read-only: ask Gamma whether markets have RESOLVED. A market counts
        as resolved only when it is closed AND every outcome price is exactly
        0 or 1 (summing to 1). Anything else — open, closed-but-unsettled,
        disputed, unreachable — is reported as not resolved. Bounded by
        `deadline_seconds` in total (it runs inside the market scan)."""
        out = []
        started = time.monotonic()
        for market_id in [str(m) for m in market_ids][:50]:
            if time.monotonic() - started > deadline_seconds:
                out.append({"market_id": market_id, "resolved": False, "error": "deadline: not checked this time"})
                continue
            try:
                response = self.client.get(f"{self.gamma}/markets/{market_id}", timeout=8.0)
                response.raise_for_status()
                raw = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                out.append({"market_id": market_id, "resolved": False,
                            "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
                continue
            raw = raw[0] if isinstance(raw, list) and raw else raw
            m = parse_market(raw) if isinstance(raw, dict) else None
            closed = isinstance(raw, dict) and str(raw.get("closed")).lower() == "true"
            prices = [o.gamma_price for o in m.outcomes] if m else []
            settled = (closed and len(prices) >= 2 and all(p is not None and (p < 1e-6 or p > 1 - 1e-6) for p in prices)
                       and abs(sum(prices) - 1) < 1e-6)
            out.append({"market_id": market_id, "closed": closed, "resolved": bool(settled),
                        "payoffs": {o.token_id: round(o.gamma_price) for o in m.outcomes} if settled else None})
        return out

    # ------------------------------------------------------------ 8A: calibration history

    CLOSED_ORDERS = ("closedTime", "endDate", None)

    def closed_markets(self, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Read-only: a page of CLOSED markets, most recently CLOSED first.

        8C: ordered by closedTime (newest first). 8A ordered by endDate,
        which put far-future end dates (2029 "next Attorney General"
        markets) first and never reached this week's finished games. If
        Gamma rejects an ordering, the next one is tried (endDate, then
        none). Returns (raw Gamma items, number returned)."""
        base: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_GAMMA_LIMIT)), "closed": "true"}
        if offset > 0:
            base["offset"] = int(offset)
        response = None
        for order in self.CLOSED_ORDERS:
            params = {**base, **({"order": order, "ascending": "false"} if order else {})}
            response = self.client.get(f"{self.gamma}/markets", params=params)
            if response.status_code not in (400, 422):
                break
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
        items = [i for i in items if isinstance(i, dict)]
        return items, len(items)

    def closed_markets_window(self, end_min: str, end_max: str, min_volume: float = 0, limit: int = 100,
                              offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Read-only (9B.2): CLOSED markets whose scheduled end date lies in [end_min, end_max] (ISO, UTC)
        with at least `min_volume` USD traded, newest end first. Gamma refuses deep offsets (422 beyond a
        few thousand) and has no cursor, so history is walked by end-date window instead."""
        params: dict[str, Any] = {"closed": "true", "end_date_min": end_min, "end_date_max": end_max,
                                  "order": "endDate", "ascending": "false",
                                  "limit": max(1, min(int(limit), MAX_GAMMA_LIMIT))}
        if min_volume > 0:
            params["volume_num_min"] = float(min_volume)
        if offset > 0:
            params["offset"] = int(offset)
        response = self.client.get(f"{self.gamma}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
        items = [i for i in items if isinstance(i, dict)]
        return items, len(items)

    def price_history(self, token_id: str, start_ts: int, end_ts: int, fidelity_minutes: int = 60
                      ) -> list[tuple[int, float]]:
        """Read-only: CLOB /prices-history for one token -> [(unix_ts, price)], oldest first."""
        response = self.client.get(f"{self.clob}/prices-history", params={
            "market": token_id, "startTs": int(start_ts), "endTs": int(end_ts), "fidelity": int(fidelity_minutes)},
            timeout=15.0)
        response.raise_for_status()
        payload = response.json()
        history = payload.get("history", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
        out = []
        for point in history:
            try:
                t, p = int(point["t"]), float(point["p"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= p <= 1:
                out.append((t, p))
        out.sort()
        return out

    def geoblock(self) -> dict[str, Any]:
        response = self.client.get("https://polymarket.com/api/geoblock")
        response.raise_for_status()
        return response.json()
