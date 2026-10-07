"""
Phase 9B.2 — the calibration dataset rebuilt from markets that reached their
scheduled end, walked by end-date window.

Principles under test:
- Gamma is asked only for CLOSED markets whose END DATE lies in a window and
  whose volume is above the minimum; no deep offsets (Gamma refuses them).
- Newest first: the last week is re-checked on every run (a market may close
  late), then the crawl continues backwards from a saved cursor, and the
  cursor only moves past a window once every market in it was handled.
- Each market is anchored on its end date in the same pass; nothing is left
  pending, and no price-history request spans more than a week.
- Out-of-scope markets (sports, five-minute "micro" markets) cost no
  price-history request and no budget.
- Read-only; nothing can trade.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jarvis.market.backfill import MAX_OFFSET, WINDOW_META, CalibrationBackfill
from jarvis.market.client import PolymarketData
from jarvis.market.store import MarketStore
from jarvis.tests.test_market_data import gamma_market

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def ended(i, days_ago, question=None, volume=50000, closed_after_hours=0.1, yes_wins=True, **extra):
    end = NOW - timedelta(days=days_ago)
    return {**gamma_market(i, tokens=(f"y{i}", f"n{i}")), "closed": True,
            "outcomePrices": '["1", "0"]' if yes_wins else '["0", "1"]', "volumeNum": volume, "endDate": iso(end),
            "closedTime": (end + timedelta(hours=closed_after_hours)).strftime("%Y-%m-%d %H:%M:%S+00"),
            **({"question": question} if question else {}), **extra}


class Polymarket:
    """Gamma end-date windows (+ volume filter, offset cap) and CLOB prices-history (range cap)."""

    def __init__(self, markets, lifetime_hours=800, fail=()):
        self.markets, self.lifetime, self.fail, self.calls = markets, lifetime_hours, set(fail), []

    def handler(self, request):
        p = dict(request.url.params)
        self.calls.append((request.url.path, p))
        if request.url.path == "/markets":
            off = int(p.get("offset", 0))
            if off >= 5000:
                return httpx.Response(422, json={"error": "offset too large"})
            lo, hi, vol = p["end_date_min"], p["end_date_max"], float(p.get("volume_num_min", 0))
            hit = [m for m in self.markets if m["closed"] and lo <= m["endDate"] <= hi and (m["volumeNum"] or 0) >= vol]
            hit.sort(key=lambda m: m["endDate"], reverse=True)
            return httpx.Response(200, json=hit[off: off + int(p["limit"])])
        if request.url.path == "/prices-history":
            a, b = int(p["startTs"]), int(p["endTs"])
            if b - a > 200 * 3600:
                return httpx.Response(400, json={"error": "interval too long"})
            token = p["market"]
            if token in self.fail:
                return httpx.Response(500)
            m = next(x for x in self.markets if token in x["clobTokenIds"])
            end = int(datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp())
            price = 0.7 if token.startswith("y") else 0.3
            return httpx.Response(200, json={"history": [
                {"t": t, "p": price} for t in range(end - self.lifetime * 3600, end + 1, 3600) if a <= t <= b]})
        return httpx.Response(404)

    def client(self):
        return PolymarketData(transport=httpx.MockTransport(self.handler))

    def history_tokens(self):
        return {p["market"] for path, p in self.calls if path == "/prices-history"}

    def windows(self):
        return [(p["end_date_min"], p["end_date_max"]) for path, p in self.calls if path == "/markets"]


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


def horizons(store, mid):
    with store._db() as db:
        return {r[0] for r in db.execute("SELECT DISTINCT horizon_hours FROM calib_points_end WHERE market_id=?", (mid,))}


def test_window_query_asks_for_closed_markets_by_end_date_and_volume():
    pm = Polymarket([ended(1, 2), ended(2, 2, volume=10), ended(3, 40)])
    lo, hi = iso(NOW - timedelta(days=3)), iso(NOW)
    items, n = pm.client().closed_markets_window(lo, hi, 1000)
    q = pm.calls[0][1]
    assert n == 1 and items[0]["id"] == "1"
    assert (q["closed"], q["end_date_min"], q["end_date_max"], q["order"]) == ("true", lo, hi, "endDate")
    assert float(q["volume_num_min"]) == 1000 and "offset" not in q


def test_markets_are_added_and_end_anchored_in_one_pass(store):
    pm = Polymarket([ended(1, 2), ended(2, 20), ended(3, 100)])
    summary = CalibrationBackfill(pm.client(), store, pace_seconds=0).run_windows(days=30)
    assert summary["added"] == 2 and summary["stopped"] == "history complete" and summary["days_covered"] >= 30
    assert store.calibration_known(["1", "2", "3"]) == {"1", "2"}             # 100 days ago is outside the 30 days
    assert horizons(store, "1") == {24, 72, 168, 336, 720} and store.end_anchor_stats()["pending"] == 0
    spans = [int(p["endTs"]) - int(p["startTs"]) for path, p in pm.calls if path == "/prices-history"]
    assert max(spans) <= 172 * 3600                                           # never a range Polymarket refuses
    assert all(int(p.get("offset", 0)) < 5000 for path, p in pm.calls if path == "/markets")


def test_a_market_resolved_early_has_no_point_after_its_resolution(store):
    pm = Polymarket([ended(1, 3, closed_after_hours=-48)])                    # resolved two days before its end
    CalibrationBackfill(pm.client(), store, pace_seconds=0).run_windows(days=10)
    assert horizons(store, "1") == {72, 168, 336, 720}
    with store._db() as db:
        late = db.execute("SELECT COUNT(*) FROM calib_points_end p JOIN calib_markets m ON m.market_id=p.market_id"
                          " WHERE p.price_time > m.reference_time").fetchone()[0]
    assert late == 0


def test_out_of_scope_markets_cost_no_history_request_and_no_budget(store):
    pm = Polymarket([ended(1, 1, question="Chicago Cubs vs. San Diego Padres"),
                     ended(2, 1, question="Bitcoin Up or Down - October 2, 12:35PM-12:40PM ET"),
                     ended(3, 1, sportsMarketType="moneyline"), ended(4, 1, outcomePrices='["0.5", "0.5"]'),
                     ended(5, 1)])
    bf = CalibrationBackfill(pm.client(), store, pace_seconds=0, exclude_segments={"sports", "micro"})
    summary = bf.run_windows(days=5, max_markets=1)
    assert summary["added"] == 1 and summary["skipped"] == {
        "out of scope: sports": 2, "out of scope: micro": 1, "not cleanly resolved": 1}
    assert pm.history_tokens() == {"y5", "n5"}


def test_newest_first_then_the_cursor_moves_back_and_is_resumed(store):
    pm = Polymarket([ended(i, d) for i, d in enumerate((1, 9, 12, 15, 18), start=1)], lifetime_hours=30)
    bf = CalibrationBackfill(pm.client(), store, pace_seconds=0)
    first = bf.run_windows(days=30, max_markets=3)
    assert first["added"] == 3 and first["stopped"] == "max_markets"
    assert store.calibration_known([str(i) for i in range(1, 6)]) == {"1", "2", "3"}      # newest first
    cursor = datetime.fromisoformat(store.meta_get(WINDOW_META))
    assert NOW - timedelta(days=15.5) < cursor < NOW - timedelta(days=12)     # stopped before the unfinished window
    pm.calls.clear()
    second = bf.run_windows(days=30, max_markets=10)
    assert second["added"] == 2 and second["stopped"] == "history complete"
    assert pm.history_tokens() == {"y4", "n4", "y5", "n5"}                    # nothing fetched twice
    deep = [w for w in pm.windows() if w[1] <= iso(NOW - timedelta(days=7))]
    assert min(w[1] for w in deep) > iso(NOW - timedelta(days=31)) and max(w[1] for w in deep) <= iso(cursor + timedelta(hours=6))


def test_the_last_week_is_rechecked_for_markets_that_closed_late(store):
    late = ended(1, 3)
    late["closed"] = False                                                    # past its end date, not yet resolved
    pm = Polymarket([late])
    bf = CalibrationBackfill(pm.client(), store, pace_seconds=0)
    assert bf.run_windows(days=10)["added"] == 0
    late["closed"] = True                                                     # resolved a day later
    assert bf.run_windows(days=10)["added"] == 1


def test_a_failed_request_keeps_the_window_for_the_next_run(store):
    pm = Polymarket([ended(1, 10)], fail={"y1"})
    bf = CalibrationBackfill(pm.client(), store, pace_seconds=0)
    first = bf.run_windows(days=30)
    assert first["added"] == 0 and first["errors"] and first["stopped"] is None
    assert datetime.fromisoformat(store.meta_get(WINDOW_META)) > NOW - timedelta(days=10)
    pm.fail.clear()
    assert bf.run_windows(days=30)["added"] == 1


def test_a_window_too_full_for_gamma_s_offset_cap_is_reported(store):
    crowd = [ended(i, 1, question="Tigers vs. Lions") for i in range(1, MAX_OFFSET + 150)]
    pm = Polymarket(crowd)
    bf = CalibrationBackfill(pm.client(), store, pace_seconds=0, exclude_segments={"sports"})
    summary = bf.run_windows(days=2)
    assert summary["truncated_windows"] == 1 and not summary["errors"]        # said, not hidden; no 422
    assert summary["skipped"]["out of scope: sports"] == MAX_OFFSET


def test_background_batch_and_status(store):
    pm = Polymarket([ended(1, 2)])
    bf = CalibrationBackfill(pm.client(), store, pace_seconds=0)
    summary = bf.run_all(days=5, max_markets=10)
    assert summary["added"] == 1 and summary["reanchor"]["stopped"] == "nothing pending"
    status = bf.status()
    assert status["backfill_version"] == "9B.2" and status["history_cursor"] and status["end_anchor"]["pending"] == 0
