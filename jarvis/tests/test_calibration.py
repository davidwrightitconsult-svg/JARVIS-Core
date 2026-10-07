"""
Phase 8A — calibration dataset and analysis.

Principles under test:
- Only cleanly resolved markets (closed AND 0/1 prices summing to 1) with
  enough volume enter the dataset; everything else is remembered as skipped
  with a reason and never fetched twice; a transient fetch error is retried
  on a later run, never recorded as "no data".
- The price at each horizon is the last recorded price at/before
  (reference - horizon), within a tolerance — never a later price.
- Calibration verdicts use Wilson intervals and minimum samples; Brier is
  reported against a no-skill baseline.
- The backfill is read-only, runs outside Hermes jobs, one at a time.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.market.backfill import CalibrationBackfill, _price_at, reference_time, resolution_of
from jarvis.market.client import PolymarketData
from jarvis.market.models import parse_market
from jarvis.market.store import MarketStore
from jarvis.quant.calibration import build_calibration, calibration_table, pooled, render_calibration_markdown, wilson
from jarvis.tests.test_market_data import gamma_market

REF = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def closed(i, prices='["1", "0"]', volume=50000, end=REF, closed_time=None, tokens=None):
    return {**gamma_market(i, tokens=tokens or (f"y{i}", f"n{i}")), "closed": True, "outcomePrices": prices,
            "volumeNum": volume, "endDate": end.isoformat().replace("+00:00", "Z"),
            **({"closedTime": closed_time} if closed_time else {})}


# ================================================================ pure helpers

def test_only_clean_resolutions_count():
    assert resolution_of(closed(1))[1] == {"y1": 1.0, "n1": 0.0}
    assert resolution_of(closed(2, prices='["0", "1"]'))[1] == {"y2": 0.0, "n2": 1.0}
    assert resolution_of(closed(3, prices='["0.5", "0.5"]')) is None            # 50/50 / ambiguous
    assert resolution_of({**closed(4), "closed": False}) is None                 # still open
    assert resolution_of(closed(5, prices='["1", "1"]')) is None                 # malformed


def test_reference_time_is_when_the_outcome_became_known():
    early = closed(1, closed_time="2026-09-18 08:00:00+00")                      # resolved before endDate
    assert reference_time(early, parse_market(early)) == datetime(2026, 9, 18, 8, tzinfo=timezone.utc)
    late = closed(2, closed_time="2026-09-21 08:00:00+00")                       # closed after the end
    assert reference_time(late, parse_market(late)) == REF


def test_price_at_never_uses_a_later_price():
    h = [(100, 0.1), (200, 0.2), (300, 0.3)]
    assert _price_at(h, 250, 100) == (200, 0.2)
    assert _price_at(h, 250, 10) is None                                          # nothing close enough
    assert _price_at(h, 99, 1000) is None                                         # only later prices exist


# ================================================================ backfill

class FakeHistory:
    """Gamma closed markets + CLOB prices-history, with call log."""

    def __init__(self, markets, prices, fail_tokens=(), reject_order=False):
        self.markets, self.prices, self.fail, self.reject_order = markets, prices, set(fail_tokens), reject_order
        self.calls = []

    def handler(self, request):
        p = dict(request.url.params)
        self.calls.append((request.url.path, p))
        if request.url.path == "/markets":
            if self.reject_order and "order" in p:
                return httpx.Response(422, json={"error": "bad order"})
            off, lim = int(p.get("offset", 0)), int(p.get("limit", 100))
            return httpx.Response(200, json=self.markets[off:off + lim])
        if request.url.path == "/prices-history":
            token = p["market"]
            if token in self.fail:
                return httpx.Response(500)
            start, end = int(p["startTs"]), int(p["endTs"])
            return httpx.Response(200, json={"history": [{"t": t, "p": v} for t, v in self.prices.get(token, [])
                                                         if start <= t <= end]})
        return httpx.Response(404)

    def client(self):
        return PolymarketData(transport=httpx.MockTransport(self.handler))


def hourly(token_price_yes, ref=REF, hours=200):
    """Hourly history: YES at a fixed price, NO at 1 - price."""
    ts = int(ref.timestamp())
    return [(ts - h * 3600, token_price_yes) for h in range(hours, -1, -1)]


def test_backfill_builds_points_and_remembers_skips(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    markets = [closed(1), closed(2, prices='["0.5", "0.5"]'), closed(3, volume=10),
               closed(4, tokens=("y4", "n4")), closed(5, tokens=("y5", "n5"))]
    prices = {"y1": hourly(0.8), "n1": hourly(0.2), "y5": hourly(0.3), "n5": hourly(0.7)}
    fake = FakeHistory(markets, prices)
    summary = CalibrationBackfill(fake.client(), store, pace_seconds=0).run(max_markets=50)
    assert summary["added"] == 2 and summary["skipped"] == {
        "not cleanly resolved": 1, "volume < 1000": 1, "no price history": 1}
    rows = store.calibration_points(24)
    assert {(r["token_id"], r["price"], r["payoff"]) for r in rows} == {
        ("y1", 0.8, 1.0), ("n1", 0.2, 0.0), ("y5", 0.3, 1.0), ("n5", 0.7, 0.0)}
    assert all(r["price_time"] <= (REF - timedelta(hours=24)).isoformat() for r in rows)
    stats = store.calibration_stats()
    assert stats["markets"] == 2 and stats["points_by_horizon"] == {"1h": 4, "6h": 4, "24h": 4, "72h": 4, "168h": 4}
    fake.calls.clear()
    again = CalibrationBackfill(fake.client(), store, pace_seconds=0).run(max_markets=50)
    assert again["added"] == 0 and again["seen"] == 0                        # nothing fetched twice
    assert not [c for c in fake.calls if c[0] == "/prices-history"]


def test_transient_history_error_is_retried_later(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    fake = FakeHistory([closed(1)], {"y1": hourly(0.8), "n1": hourly(0.2)}, fail_tokens={"y1"})
    first = CalibrationBackfill(fake.client(), store, pace_seconds=0).run()
    assert first["added"] == 0 and first["errors"] and store.calibration_known(["1"]) == set()
    fake.fail.clear()
    assert CalibrationBackfill(fake.client(), store, pace_seconds=0).run()["added"] == 1


def test_short_history_gives_only_the_horizons_it_covers(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    fake = FakeHistory([closed(1)], {"y1": hourly(0.6, hours=30), "n1": hourly(0.4, hours=30)})
    CalibrationBackfill(fake.client(), store, pace_seconds=0).run()
    assert store.calibration_stats()["points_by_horizon"] == {"1h": 2, "6h": 2, "24h": 2}


def test_closed_markets_query_and_order_fallback():
    fake = FakeHistory([closed(1)], {}, reject_order=True)
    items, n = fake.client().closed_markets(10)
    assert n == 1 and fake.calls[0][1]["closed"] == "true" and "order" not in fake.calls[-1][1]


def test_only_one_backfill_at_a_time(tmp_path):
    bf = CalibrationBackfill(FakeHistory([], {}).client(), MarketStore(tmp_path / "m.db"), pace_seconds=0)
    bf._running.acquire()
    assert bf.run() == {"status": "ALREADY_RUNNING"} and bf.start_background() is False
    bf._running.release()


# ================================================================ analysis

def pts(price, n, wins, market_prefix="m"):
    return [{"market_id": f"{market_prefix}{price}-{i}", "price": price, "payoff": 1.0 if i < wins else 0.0}
            for i in range(n)]


def test_wilson_interval():
    lo, hi = wilson(2, 100)
    assert lo == pytest.approx(0.0055, abs=1e-4) and hi == pytest.approx(0.0700, abs=1e-4)   # 2 of 100
    assert wilson(0, 0) is None


def test_calibration_verdicts():
    points = pts(0.08, 200, 4) + pts(0.45, 200, 90) + pts(0.93, 200, 196) + pts(0.25, 10, 3)
    cal = calibration_table(points)
    by = {b["bin"]: b for b in cal["bins"]}
    assert by["0.05-0.10"]["verdict"] == "OVERPRICED" and by["0.05-0.10"]["bias"] == round(0.02 - 0.08, 4)
    assert by["0.40-0.50"]["verdict"] == "CONSISTENT"
    assert by["0.90-0.95"]["verdict"] == "UNDERPRICED"                        # favourites win more than priced
    assert by["0.20-0.30"]["verdict"] == "FEW_SAMPLES" and by["0.00-0.02"]["samples"] == 0
    assert cal["samples"] == 610 and cal["brier"] < cal["baseline_brier"]
    assert pooled(points, 0.0, 0.10)["verdict"] == "OVERPRICED"
    assert calibration_table([pts(0.999, 1, 1)[0]])["bins"][-1]["samples"] == 1      # 1.0 edge lands in last bin


def test_build_and_render_from_store(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    for i in range(40):
        store.record_calibration_market(
            {"market_id": str(i), "question": f"Q{i}", "reference_time": REF.isoformat(), "volume": 5000},
            [{"token_id": f"y{i}", "outcome": "Yes", "payoff": 1.0 if i == 0 else 0.0, "horizon_hours": 24,
              "price": 0.07, "price_time": REF.isoformat()},
             {"token_id": f"n{i}", "outcome": "No", "payoff": 0.0 if i == 0 else 1.0, "horizon_hours": 24,
              "price": 0.93, "price_time": REF.isoformat()}])
    cal = build_calibration(store)
    assert cal["samples"] == 80 and cal["longshots"]["win_rate"] == 0.025
    assert build_calibration(store, min_volume=10000)["samples"] == 0
    md = render_calibration_markdown(cal)
    assert "| 0.05-0.10 | 40 | 40 | 7.0% | 2.5% |" in md and "Long shots" in md


# ================================================================ pipeline + app

def test_pipeline_starts_backfill_every_interval(tmp_path):
    from jarvis.quant import LIBRARY, QuantEngine, QuantPipeline
    store = MarketStore(tmp_path / "m.db")
    started = []
    pipe = QuantPipeline(QuantEngine(store), LIBRARY, calibration_updater=lambda: started.append(1) or True,
                         calibration_interval_hours=3)
    now = datetime.now(timezone.utc)
    assert pipe.evaluate(now=now)["calibration_backfill"] == "started"
    assert "calibration_backfill" not in pipe.evaluate(now=now + timedelta(hours=1))
    assert pipe.evaluate(now=now + timedelta(hours=3, minutes=1))["calibration_backfill"] == "started"
    assert len(started) == 2


def test_app_calibration_endpoints(tmp_path, monkeypatch):
    from jarvis.tests.test_market_data import FakePolymarket
    from jarvis.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    store.record_calibration_market({"market_id": "9", "reference_time": REF.isoformat(), "volume": 5000},
                                    [{"token_id": "y9", "payoff": 1.0, "horizon_hours": 24, "price": 0.6,
                                      "price_time": REF.isoformat()}])
    main = _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], {}), store)
    with TestClient(main.app) as c:
        cal = c.get("/api/quant/calibration").json()
        assert cal["samples"] == 1 and cal["backfill"] == {"enabled": False}
        assert c.post("/api/market/calibration/backfill").status_code == 503          # disabled in tests
        assert c.get("/api/quant/scorecard").json()["calibration"]["samples"] == 1
        assert c.get("/health").json()["execution_locked"] is True
