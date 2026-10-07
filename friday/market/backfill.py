"""
FRIDAY calibration backfill (Phase 8A). Read-only against Polymarket.

Builds the dataset that answers the question every edge depends on: when a
Polymarket price said p, how often did that outcome actually happen?

For each CLOSED market (newest first) that resolved cleanly — closed AND
every outcome price exactly 0 or 1, summing to 1 — and traded at least
`min_volume` USD:
  reference time = when the outcome became known: the earlier of Gamma's
                   endDate and closedTime (a market resolved early uses its
                   close time)
  for each outcome token, the recorded price (CLOB /prices-history, hourly)
  at 1h, 6h, 24h, 72h and 168h before the reference time — the last price
  at or before that moment, within a tolerance — together with its 1/0
  payoff.
Markets that are unresolved, ambiguous, thin or without history are
remembered as skipped (with a reason) so they are never fetched twice.

8C (the jarvis020 sports gap): closed markets are crawled most recently
CLOSED first (Gamma closedTime), so the finished games of the last days are
reached first — 8A's endDate ordering started at 2029 end dates and never
got to them. Each run first takes the newest closures; once it meets a page
it has fully seen, it jumps to where the previous run's crawl stopped
(meta calibration_crawl_offset), so the dataset also grows backwards in
time. Volume is read from every Gamma volume field (volumeNum, volume,
volumeClob + volumeAmm); a market with no volume field at all is skipped
as "volume unknown", never silently treated as zero.

8C.2 — scope: markets of an excluded segment (FRIDAY_EXCLUDED_SEGMENTS,
default sports) are skipped as "out of scope: <segment>" BEFORE any price
history is fetched. If a segment is brought back into scope its skips are
forgotten, so those markets are evaluated on the next crawl.

9B — END ANCHOR (the 9A finding). The points above are taken a fixed time
before the ACTUAL resolution. For a market that resolved early that is
knowledge of the future: "NVIDIA all-time high by December 31?" resolved
on 2 October, and its "24h before" price is a price FRIDAY, looking at a
market with three months left, could never have known was one day from the
end. `reanchor()` therefore builds a second set of points (calib_points_end):
the price at 24h / 72h / 168h / 336h / 720h before the SCHEDULED end date,
and only when the market was still open at that moment. That is exactly
what FRIDAY knows live (hours to the published end), so the fair-value
tables and the backtest use this set. A market that resolved before
end - h simply has no point at h. Recent prices are read at 60-minute
fidelity, the 14- and 30-day ones at 6-hour fidelity (one extra request per
token). Every calibration market, old and new, goes through `reanchor()`;
each background batch re-anchors what is pending after its crawl.

9B.2 — what jarvis020 showed after re-anchoring, and the rebuild.
- 1,470 of the 1,962 calibration markets had resolved more than 30 days
  before their scheduled end. 8A crawled closed markets with the furthest
  end dates first, and a closed market whose end date is years away has, by
  definition, resolved early. The dataset was a sample of early resolutions.
- Gamma refuses offsets beyond a few thousand (422), so "page deeper" is not
  available. It does filter by end-date window and minimum volume.
- CLOB refuses any price-history request spanning 30 days (400) at every
  fidelity; a week is served, even for markets that ended six months ago.
`run_windows()` therefore rebuilds the dataset from markets that reached
their scheduled end: it walks 6-hour end-date windows backwards (newest
first, the last 7 days re-checked on every run, then on from a saved
cursor), asks Gamma only for closed markets above the volume minimum, and
anchors each market on its end date in the same pass. The 14- and 30-day
prices come from a small 12-hour request around each moment, and only for
markets that were already trading a week before they ended.

Honest limits: prices-history is a traded/mid price, not the executable ask
(8B prices edges against the live ask); a market resolved early may already
reflect the news at short horizons, which is why 24h+ horizons are the
default for analysis.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .models import parse_market, parse_time

BACKFILL_VERSION = "9B.2"
END_HORIZONS = (24, 72, 168, 336, 720)
FINE_HOURS = 172          # one request: the last week (60-minute prices) before the resolution
WINDOW_HOURS = 6          # end-date window of the history crawl
HEAD_DAYS = 7             # re-checked on every run (markets that closed late)
WINDOW_META = "calibration_window_cursor"
MAX_OFFSET = 4900
CRAWL_META = "calibration_crawl_offset"
HORIZONS_HOURS = (1, 6, 24, 72, 168)
MIN_VOLUME = 1000.0


def _price_at(history: list[tuple[int, float]], target: int, tolerance: int) -> tuple[int, float] | None:
    """Last (t, p) with target - tolerance <= t <= target."""
    best = None
    for t, p in history:
        if t > target:
            break
        if t >= target - tolerance:
            best = (t, p)
    return best


def resolution_of(raw: dict[str, Any]) -> tuple[Any, dict[str, float]] | None:
    """(Market, {token_id: payoff}) when the market resolved cleanly, else None."""
    m = parse_market(raw)
    if m is None or len(m.outcomes) < 2 or str(raw.get("closed")).lower() != "true":
        return None
    prices = [o.gamma_price for o in m.outcomes]
    if any(p is None or not (p < 1e-6 or p > 1 - 1e-6) for p in prices) or abs(sum(prices) - 1) > 1e-6:
        return None
    return m, {o.token_id: float(round(o.gamma_price)) for o in m.outcomes}


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v >= 0 else None


def volume_of(raw: dict[str, Any]) -> float | None:
    """Lifetime traded volume (USD) from any Gamma volume field; None if none is present."""
    candidates = [_num(raw.get("volumeNum")), _num(raw.get("volume"))]
    clob, amm = _num(raw.get("volumeClob")), _num(raw.get("volumeAmm"))
    if clob is not None or amm is not None:
        candidates.append((clob or 0.0) + (amm or 0.0))
    known = [c for c in candidates if c is not None]
    return max(known) if known else None


def reference_time(raw: dict[str, Any], m) -> datetime | None:
    times = [t for t in (parse_time(m.end_date), parse_time(raw.get("closedTime"))) if t is not None]
    return min(times) if times else None


class CalibrationBackfill:
    def __init__(self, client, store, min_volume: float = MIN_VOLUME, pace_seconds: float = 0.15,
                 horizons: tuple[float, ...] = HORIZONS_HOURS, exclude_segments: Any = ()):
        self.client = client
        self.store = store
        self.min_volume = float(min_volume)
        self.pace_seconds = max(0.0, float(pace_seconds))
        self.horizons = tuple(horizons)
        self.exclude_segments = frozenset(exclude_segments or ())
        self._running = threading.Lock()
        self.last: dict[str, Any] | None = None

    def run(self, max_markets: int = 200, max_pages: int = 30, deadline_seconds: float | None = None,
            page_size: int = 100) -> dict[str, Any]:
        """Process up to `max_markets` NEW closed markets. One run at a time."""
        if not self._running.acquire(blocking=False):
            return {"status": "ALREADY_RUNNING"}
        try:
            return self._run(max_markets, max_pages, deadline_seconds, page_size)
        finally:
            self._running.release()

    def _run(self, max_markets, max_pages, deadline_seconds, page_size) -> dict[str, Any]:
        started = time.monotonic()
        summary: dict[str, Any] = {"backfill_version": BACKFILL_VERSION, "pages": 0, "seen": 0, "added": 0,
                                   "points": 0, "skipped": {}, "errors": [], "stopped": None}

        def skip(market_id: str, reason: str) -> None:
            self.store.record_calibration_skip(market_id, reason)
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

        meta_get = getattr(self.store, "meta_get", None)
        meta_set = getattr(self.store, "meta_set", None)
        if meta_get and meta_set and meta_get("calibration_volume_rules") != BACKFILL_VERSION:
            # once: 8A treated a blank volume field as 0 and skipped those markets as thin
            summary["volume_skips_reopened"] = self.store.forget_calibration_skips("volume <")
            meta_set("calibration_volume_rules", BACKFILL_VERSION)
        from .segments import SEGMENTS
        back = sum(self.store.forget_calibration_skips(f"out of scope: {seg}")
                   for seg in SEGMENTS if seg not in self.exclude_segments)
        if back:
            summary["scope_skips_reopened"] = back
            if meta_set:
                meta_set(CRAWL_META, "0")
        try:
            resume = int(meta_get(CRAWL_META) or 0) if meta_get else 0
        except ValueError:
            resume = 0
        offset = 0
        jumped = False
        processed = 0
        for _ in range(max(1, int(max_pages))):
            if deadline_seconds is not None and time.monotonic() - started > deadline_seconds:
                summary["stopped"] = "deadline"
                break
            try:
                items, n = self.client.closed_markets(page_size, offset)
            except (httpx.HTTPError, ValueError) as exc:
                summary["errors"].append(f"closed markets: {type(exc).__name__}: {str(exc)[:120]}")
                break
            summary["pages"] += 1
            if not n:
                summary["stopped"] = "no more closed markets"
                if meta_set:
                    meta_set(CRAWL_META, "0")            # crawled everything: start over from the newest
                break
            known = self.store.calibration_known([str(i.get("id")) for i in items])
            if not jumped and resume > offset + n and all(str(i.get("id") or "") in known for i in items):
                jumped = True                             # newest closures done: resume the deep crawl
                summary["resumed_at_offset"] = resume
                offset = resume
                continue
            offset += n
            complete = True
            for raw in items:
                market_id = str(raw.get("id") or "")
                if not market_id or market_id in known:
                    continue
                summary["seen"] += 1
                if processed >= max_markets:
                    complete = False
                    break
                if deadline_seconds is not None and time.monotonic() - started > deadline_seconds:
                    summary["stopped"] = "deadline"
                    complete = False
                    break
                resolved = resolution_of(raw)
                if resolved is None:
                    skip(market_id, "not cleanly resolved")
                    continue
                m, payoffs = resolved
                if m.segment in self.exclude_segments:
                    skip(market_id, f"out of scope: {m.segment}")
                    continue
                volume = volume_of(raw)
                if volume is None:
                    skip(market_id, "volume unknown")
                    continue
                if volume < self.min_volume:
                    skip(market_id, f"volume < {self.min_volume:g}")
                    continue
                ref = reference_time(raw, m)
                if ref is None:
                    skip(market_id, "no end/close time")
                    continue
                processed += 1                    # only markets that cost price-history requests count
                ref_ts = int(ref.timestamp())
                start_ts = ref_ts - int((max(self.horizons) + 4) * 3600)
                points = []
                failed = False
                for o in m.outcomes:
                    try:
                        history = self.client.price_history(o.token_id, start_ts, ref_ts)
                    except (httpx.HTTPError, ValueError) as exc:
                        summary["errors"].append(f"history {market_id}: {type(exc).__name__}: {str(exc)[:100]}")
                        failed = True
                        break
                    finally:
                        if self.pace_seconds:
                            time.sleep(self.pace_seconds)
                    for h in self.horizons:
                        target = ref_ts - int(h * 3600)
                        tolerance = int(max(2.0, 0.1 * h) * 3600)
                        hit = _price_at(history, target, tolerance)
                        if hit is not None:
                            points.append({"token_id": o.token_id, "outcome": o.name, "payoff": payoffs[o.token_id],
                                           "horizon_hours": h, "price": hit[1],
                                           "price_time": datetime.fromtimestamp(hit[0], timezone.utc)
                                           .isoformat(timespec="seconds")})
                if failed:
                    complete = False             # transient: try again on a later run
                    continue
                if not points:
                    skip(market_id, "no price history")
                    continue
                self.store.record_calibration_market({
                    "market_id": market_id, "question": m.question, "slug": m.slug, "end_date": m.end_date,
                    "closed_time": raw.get("closedTime"), "reference_time": ref.isoformat(timespec="seconds"),
                    "volume": volume, "neg_risk": m.neg_risk}, points)
                summary["added"] += 1
                summary["points"] += len(points)
            if complete and meta_set and offset > resume:
                resume = offset                   # every market up to here handled: remember the depth
                meta_set(CRAWL_META, str(offset))
            if summary["stopped"] == "deadline":
                break
            if processed >= max_markets:
                summary["stopped"] = "max_markets"
                break
        summary["crawl_offset"] = resume
        summary["errors"] = summary["errors"][:10]
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last = summary
        return summary

    # ------------------------------------------------------------ 9B: end anchor

    def reanchor(self, max_markets: int = 300, deadline_seconds: float | None = None) -> dict[str, Any]:
        """Build end-anchored points for calibration markets that do not have them yet."""
        if not self._running.acquire(blocking=False):
            return {"status": "ALREADY_RUNNING"}
        try:
            return self._reanchor(max_markets, deadline_seconds)
        finally:
            self._running.release()

    def _reanchor(self, max_markets, deadline_seconds) -> dict[str, Any]:
        started = time.monotonic()
        summary: dict[str, Any] = {"backfill_version": BACKFILL_VERSION, "markets": 0, "points": 0, "by_status": {},
                                   "errors": [], "stopped": None}

        def done(market_id, points, status):
            self.store.record_end_anchor(market_id, points, status)
            summary["markets"] += 1
            summary["points"] += len(points)
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1

        from .segments import segment_of
        if self.store.meta_get("end_anchor_rules") != BACKFILL_VERSION:
            # once: 9B.1 asked for a 30-day range Polymarket refuses, so long horizons were lost
            summary["redone"] = self.store.reset_end_anchor(("done", "no price at any horizon"))
            self.store.meta_set("end_anchor_rules", BACKFILL_VERSION)
        fetched = 0
        while fetched < max_markets and summary["stopped"] is None:
            batch = self.store.end_anchor_pending(50)
            if not batch:
                summary["stopped"] = "nothing pending"
                break
            progressed = False
            for m in batch:
                if deadline_seconds is not None and time.monotonic() - started > deadline_seconds:
                    summary["stopped"] = "deadline"
                    break
                if fetched >= max_markets:
                    summary["stopped"] = "max_markets"
                    break
                end, ref = parse_time(m["end_date"]), parse_time(m["reference_time"])
                tokens = {}
                for p in m["points"]:
                    tokens.setdefault(p["token_id"], (p["outcome"], p["payoff"]))
                if segment_of(m["question"], m["neg_risk"]) in self.exclude_segments:
                    done(m["market_id"], [], "out of scope")
                    progressed = True
                    continue
                if end is None or ref is None or not tokens:
                    done(m["market_id"], [], "no end date")
                    progressed = True
                    continue
                end_ts, ref_ts = int(end.timestamp()), int(ref.timestamp())
                if end_ts - max(END_HORIZONS) * 3600 > ref_ts:
                    done(m["market_id"], [], "resolved before any horizon")     # e.g. months early: no request
                    progressed = True
                    continue
                fetched += 1
                points, failed = [], False
                for token_id, (outcome, payoff) in tokens.items():
                    try:
                        fine = self._history(token_id, ref_ts - FINE_HOURS * 3600, ref_ts)
                        points += self._end_points(token_id, outcome, payoff, end_ts, ref_ts, fine)
                    except (httpx.HTTPError, ValueError) as exc:
                        summary["errors"].append(f"history {m['market_id']}: {type(exc).__name__}: {str(exc)[:100]}")
                        failed = True
                        break
                if failed:
                    continue                               # transient: stays pending for a later run
                done(m["market_id"], points, "done" if points else "no price at any horizon")
                progressed = True
            if not progressed and summary["stopped"] is None:
                summary["stopped"] = "errors only"         # never spin on a failing batch
        summary["errors"] = summary["errors"][:10]
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["pending"] = self.store.end_anchor_stats()["pending"]
        self.last_reanchor = summary
        return summary

    def _history(self, token_id: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
        """Paced price history; a refused range (400/404/422) is final and yields nothing."""
        try:
            return self.client.price_history(token_id, start_ts, end_ts)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404, 422):
                return []
            raise
        finally:
            if self.pace_seconds:
                time.sleep(self.pace_seconds)

    def _end_points(self, token_id: str, outcome, payoff, end_ts: int, ref_ts: int,
                    fine: list[tuple[int, float]]) -> list[dict[str, Any]]:
        """Prices at END_HORIZONS before the scheduled end, while the market was open. `fine` is the
        last week before the resolution; a moment before it is read with one small request, and only
        if the market was already trading at the start of that week."""
        points = []
        fine_start = ref_ts - FINE_HOURS * 3600
        old = bool(fine) and fine[0][0] <= fine_start + 12 * 3600
        for h in END_HORIZONS:
            target = end_ts - int(h * 3600)
            if target > ref_ts:
                continue                                   # already resolved by then: FRIDAY could not have bet
            tolerance = int(min(12.0, max(2.0, 0.1 * h)) * 3600)
            hit = _price_at(fine, target, tolerance)
            if hit is None and old and target < fine_start + tolerance:
                hit = _price_at(sorted(self._history(token_id, target - tolerance, target)), target, tolerance)
            if hit is not None:
                points.append({"token_id": token_id, "outcome": outcome, "payoff": payoff, "horizon_hours": h,
                               "price": hit[1], "price_time": datetime.fromtimestamp(hit[0], timezone.utc)
                               .isoformat(timespec="seconds")})
        return points

    # ------------------------------------------------------------ 9B.2: history by end-date window

    def run_windows(self, days: float = 180, max_markets: int = 300, deadline_seconds: float | None = None
                    ) -> dict[str, Any]:
        """Markets that reached their scheduled end in the last `days`, end-anchored in the same pass."""
        if not self._running.acquire(blocking=False):
            return {"status": "ALREADY_RUNNING"}
        try:
            return self._run_windows(days, max_markets, deadline_seconds)
        finally:
            self._running.release()

    def _run_windows(self, days, max_markets, deadline_seconds) -> dict[str, Any]:
        started = time.monotonic()
        summary: dict[str, Any] = {"backfill_version": BACKFILL_VERSION, "windows": 0, "pages": 0, "seen": 0,
                                   "added": 0, "points": 0, "end_points": 0, "skipped": {}, "errors": [],
                                   "stopped": None, "truncated_windows": 0}
        state = {"fetched": 0}

        def skip(market_id: str, reason: str) -> None:
            self.store.record_calibration_skip(market_id, reason)
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

        def out_of_budget() -> bool:
            if deadline_seconds is not None and time.monotonic() - started > deadline_seconds:
                summary["stopped"] = "deadline"
                return True
            if state["fetched"] >= max_markets:
                summary["stopped"] = "max_markets"
                return True
            return False

        def scan(lo: datetime, hi: datetime) -> bool:
            """True if every market of the window was handled."""
            summary["windows"] += 1
            offset = 0
            while True:
                try:
                    items, n = self.client.closed_markets_window(
                        lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ"), self.min_volume, 100, offset)
                except (httpx.HTTPError, ValueError) as exc:
                    summary["errors"].append(f"window {lo:%Y-%m-%d %H}h: {type(exc).__name__}: {str(exc)[:100]}")
                    return False
                summary["pages"] += 1
                known = self.store.calibration_known([str(i.get("id")) for i in items])
                for raw in items:
                    market_id = str(raw.get("id") or "")
                    if not market_id or market_id in known:
                        continue
                    summary["seen"] += 1
                    resolved = resolution_of(raw)
                    if resolved is None:
                        skip(market_id, "not cleanly resolved")
                        continue
                    m, payoffs = resolved
                    if m.segment in self.exclude_segments:
                        skip(market_id, f"out of scope: {m.segment}")
                        continue
                    volume = volume_of(raw)
                    if volume is None or volume < self.min_volume:
                        skip(market_id, "volume unknown" if volume is None else f"volume < {self.min_volume:g}")
                        continue
                    ref, end = reference_time(raw, m), parse_time(m.end_date)
                    if ref is None or end is None:
                        skip(market_id, "no end/close time")
                        continue
                    if out_of_budget():
                        return False
                    state["fetched"] += 1
                    ref_ts, end_ts = int(ref.timestamp()), int(end.timestamp())
                    points, end_points, failed = [], [], False
                    for o in m.outcomes:
                        try:
                            fine = self._history(o.token_id, ref_ts - FINE_HOURS * 3600, ref_ts)
                            end_points += self._end_points(o.token_id, o.name, payoffs[o.token_id], end_ts, ref_ts, fine)
                        except (httpx.HTTPError, ValueError) as exc:
                            summary["errors"].append(f"history {market_id}: {type(exc).__name__}: {str(exc)[:100]}")
                            failed = True
                            break
                        for h in self.horizons:
                            hit = _price_at(fine, ref_ts - int(h * 3600), int(max(2.0, 0.1 * h) * 3600))
                            if hit is not None:
                                points.append({"token_id": o.token_id, "outcome": o.name, "payoff": payoffs[o.token_id],
                                               "horizon_hours": h, "price": hit[1],
                                               "price_time": datetime.fromtimestamp(hit[0], timezone.utc)
                                               .isoformat(timespec="seconds")})
                    if failed:
                        return False                      # transient: the window is scanned again next run
                    if not points and not end_points:
                        skip(market_id, "no price history")
                        continue
                    self.store.record_calibration_market({
                        "market_id": market_id, "question": m.question, "slug": m.slug, "end_date": m.end_date,
                        "closed_time": raw.get("closedTime"), "reference_time": ref.isoformat(timespec="seconds"),
                        "volume": volume, "neg_risk": m.neg_risk}, points)
                    self.store.record_end_anchor(market_id, end_points,
                                                 "done" if end_points else "no price at any horizon")
                    summary["added"] += 1
                    summary["points"] += len(points)
                    summary["end_points"] += len(end_points)
                if n < 100:
                    return True
                offset += n
                if offset >= MAX_OFFSET:
                    summary["truncated_windows"] += 1     # Gamma refuses deeper offsets; reported, not hidden
                    return True

        step = timedelta(hours=WINDOW_HOURS)
        now = datetime.now(timezone.utc)
        top = datetime.fromtimestamp((int(now.timestamp()) // (WINDOW_HOURS * 3600) + 1) * WINDOW_HOURS * 3600,
                                     timezone.utc)
        head_end = top - timedelta(days=HEAD_DAYS)
        oldest = top - timedelta(days=float(days))
        hi = top
        while hi > max(head_end, oldest) and summary["stopped"] is None and not summary["errors"]:
            scan(hi - step, hi)                           # the last week, on every run: markets that closed late
            hi -= step
        cursor = min(parse_time(self.store.meta_get(WINDOW_META)) or head_end, head_end)
        while cursor > oldest and summary["stopped"] is None and not summary["errors"]:
            if not scan(cursor - step, cursor):
                break
            cursor -= step
            self.store.meta_set(WINDOW_META, cursor.isoformat(timespec="seconds"))
        if cursor <= oldest and summary["stopped"] is None and not summary["errors"]:
            summary["stopped"] = "history complete"
            self.store.meta_set(WINDOW_META, cursor.isoformat(timespec="seconds"))
        summary["cursor"] = cursor.isoformat(timespec="seconds")
        summary["target"] = oldest.isoformat(timespec="seconds")
        summary["days_covered"] = round((top - cursor).total_seconds() / 86400, 1)
        summary["errors"] = summary["errors"][:10]
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["at"] = now.isoformat(timespec="seconds")
        self.last = summary
        return summary

    def run_all(self, reanchor_markets: int = 300, days: float = 180, **kwargs) -> dict[str, Any]:
        """A background batch: markets that reached their end (by end-date window), then re-anchor
        anything still pending from the older crawls."""
        deadline = kwargs.get("deadline_seconds")
        summary = self.run_windows(days, kwargs.get("max_markets", 300), deadline)
        if summary.get("status") == "ALREADY_RUNNING":
            return summary
        summary["reanchor"] = self.reanchor(reanchor_markets, deadline)
        self.last = summary
        return summary

    def start_reanchor_background(self, max_markets: int = 300, deadline_seconds: float | None = 3600) -> bool:
        if self._running.locked():
            return False
        threading.Thread(target=self.reanchor, args=(max_markets, deadline_seconds),
                         name="friday-calibration-reanchor", daemon=True).start()
        return True

    def start_background(self, **kwargs) -> bool:
        """Run in a daemon thread (never inside a Hermes job). False if one is running."""
        if self._running.locked():
            return False
        threading.Thread(target=self.run_all, kwargs=kwargs, name="friday-calibration-backfill", daemon=True).start()
        return True

    def status(self) -> dict[str, Any]:
        return {"backfill_version": BACKFILL_VERSION, "running": self._running.locked(), "last": self.last,
                "last_reanchor": getattr(self, "last_reanchor", None), "end_anchor": self.store.end_anchor_stats(),
                "history_cursor": self.store.meta_get(WINDOW_META),
                "exclude_segments": sorted(self.exclude_segments),
                "min_volume": self.min_volume, "horizons": list(self.horizons)}
