"""
Phase 6A — Market Data Engine: correct prices, honest quality flags.

Principles under test:
- Best bid/ask never depend on list order (Polymarket lists levels
  WORST-first; the Phase 0 client read bids[0]/asks[0] — the worst prices).
- Depth is USD near the mid, not a count of shares.
- Bad data is flagged and marked unusable; errors are reported, never
  silently dropped; the client is read-only.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.market import PolymarketData, parse_book, parse_market, quality_flags, snapshot_rows

NOW = datetime(2026, 9, 26, 21, 0, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)


def raw_book(token, bids, asks, ts=MS, last="0.49"):
    """Polymarket-style: prices/sizes as strings, WORST level first."""
    return {"market": "0xcond", "asset_id": token, "timestamp": str(ts), "hash": "h",
            "bids": [{"price": str(p), "size": str(s)} for p, s in sorted(bids)],            # ascending: worst bid first
            "asks": [{"price": str(p), "size": str(s)} for p, s in sorted(asks, reverse=True)],  # descending: worst ask first
            "tick_size": "0.01", "min_order_size": "5", "last_trade_price": last}


GOOD_YES = raw_book("yes1", bids=[(0.01, 5000), (0.40, 300), (0.47, 200), (0.48, 100)],
                    asks=[(0.99, 5000), (0.60, 300), (0.51, 250), (0.50, 100)])
GOOD_NO = raw_book("no1", bids=[(0.02, 4000), (0.49, 120)], asks=[(0.98, 4000), (0.52, 90)], last="0.51")


def gamma_market(i=1, tokens=("yes1", "no1"), closed=False, order_book=True, bid=0.48, ask=0.50):
    return {"id": str(i), "question": f"Will event {i} happen?", "conditionId": f"0xc{i}",
            "slug": f"event-{i}", "endDate": "2030-12-31T00:00:00Z", "active": True, "closed": closed,
            "acceptingOrders": True, "enableOrderBook": order_book, "negRisk": False,
            "volume24hr": 12345.6, "volumeNum": 999999, "liquidityNum": 54321.0,
            "bestBid": bid, "bestAsk": ask, "lastTradePrice": 0.49,
            "outcomes": '["Yes", "No"]', "outcomePrices": '["0.49", "0.51"]',
            "clobTokenIds": json.dumps(list(tokens))}


# ================================================================ parsing

def test_best_prices_do_not_depend_on_list_order():
    book = parse_book(GOOD_YES)
    assert book.raw_first_bid == 0.01 and book.raw_first_ask == 0.99   # what Phase 0 read
    assert book.best_bid == 0.48 and book.best_ask == 0.50               # the truth
    assert book.mid == 0.49 and book.spread == 0.02
    assert [p for p, _ in book.bids] == [0.48, 0.47, 0.40, 0.01]         # best first


def test_depth_is_usd_near_mid_not_share_count():
    book = parse_book(GOOD_YES)
    # within 0.47..0.51: bids 0.48x100 + 0.47x200, asks 0.50x100 + 0.51x250
    assert book.depth_usd() == round(48 + 94 + 50 + 127.5, 2)
    assert book.depth_usd() < 1000        # the 5,000-share 0.01/0.99 walls are excluded


def test_garbage_levels_are_dropped():
    raw = {"bids": [{"price": "abc", "size": "5"}, {"price": "0.4", "size": "-3"}, {"price": "1.7", "size": "2"},
                    {"price": "0.45", "size": "10"}, ["0.44", "7"], None],
           "asks": [{"price": "0.55", "size": "0"}, {"price": "0.56", "size": "9"}]}
    book = parse_book(raw, "t")
    assert book.bids == [(0.45, 10.0), (0.44, 7.0)] and book.asks == [(0.56, 9.0)]


def test_market_parsing_string_lists_and_filters():
    m = parse_market(gamma_market())
    assert m.question == "Will event 1 happen?" and [o.name for o in m.outcomes] == ["Yes", "No"]
    assert [o.token_id for o in m.outcomes] == ["yes1", "no1"] and m.outcomes[0].gamma_price == 0.49
    assert m.liquidity == 54321.0 and m.tradable_book
    assert not parse_market(gamma_market(closed=True)).tradable_book
    assert not parse_market(gamma_market(order_book=False)).tradable_book
    assert parse_market({"id": "9", "question": "no tokens"}) is None
    assert parse_market({"clobTokenIds": '["x"]'}) is None
    assert parse_market("junk") is None


# ================================================================ quality flags

@pytest.mark.parametrize("bids,asks,flag", [
    ([], [], "EMPTY_BOOK"),
    ([(0.4, 10)], [], "ONE_SIDED"),
    ([(0.6, 10)], [(0.5, 10)], "CROSSED"),
    ([(0.01, 10)], [(0.99, 10)], "PLACEHOLDER"),
    ([(0.20, 10)], [(0.45, 10)], "WIDE_SPREAD"),
])
def test_quality_flags(bids, asks, flag):
    book = parse_book(raw_book("t", bids, asks))
    assert flag in quality_flags(book, now=NOW)


def test_stale_and_gamma_mismatch_and_price_sum():
    stale = parse_book(raw_book("t", [(0.48, 10)], [(0.50, 10)], ts=MS - 3600_000))
    assert "STALE" in quality_flags(stale, now=NOW)
    market = parse_market(gamma_market(bid=0.70, ask=0.72))
    fresh = parse_book(GOOD_YES)
    assert "GAMMA_MISMATCH" in quality_flags(fresh, market, outcome_index=0, now=NOW)
    skew = parse_market(gamma_market())
    rows = snapshot_rows(skew, {"yes1": parse_book(GOOD_YES),
                                "no1": parse_book(raw_book("no1", [(0.79, 5)], [(0.81, 5)]))}, now=NOW)
    assert all("PRICE_SUM" in r.flags for r in rows)


def test_snapshot_rows_usable_and_legacy_keys():
    m = parse_market(gamma_market())
    rows = [r.as_dict() for r in snapshot_rows(m, {"yes1": parse_book(GOOD_YES), "no1": parse_book(GOOD_NO)}, now=NOW)]
    yes, no = rows
    assert yes["usable"] and yes["flags"] == [] and yes["title"] == "Will event 1 happen? — Yes"
    assert yes["price"] == 0.49 and yes["bid"] == 0.48 and yes["ask"] == 0.50 and yes["liquidity"] == yes["depth_usd"]
    assert no["best_bid"] == 0.49 and no["best_ask"] == 0.52
    missing = snapshot_rows(m, {"yes1": None, "no1": parse_book(GOOD_NO)}, now=NOW)[0]
    assert missing.flags == ["NO_BOOK"] and not missing.usable


# ================================================================ client

class FakePolymarket:
    def __init__(self, markets, books, batch=True, fail_tokens=(), gamma_status=200):
        self.markets, self.books, self.batch = markets, books, batch
        self.fail_tokens, self.gamma_status = set(fail_tokens), gamma_status
        self.calls = []

    def handler(self, request):
        path = request.url.path
        self.calls.append((request.method, request.url.host, path, dict(request.url.params)))
        if request.url.host == "gamma-api.polymarket.com" and path == "/markets":
            params = request.url.params           # honour limit/offset like the real Gamma
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100))
            return httpx.Response(self.gamma_status, json=self.markets[offset: offset + limit])
        if path == "/books" and request.method == "POST":
            if not self.batch:
                return httpx.Response(405)
            wanted = [x["token_id"] for x in json.loads(request.content)]
            return httpx.Response(200, json=[self.books[t] for t in wanted if t in self.books and t not in self.fail_tokens])
        if path == "/book":
            token = request.url.params["token_id"]
            if token in self.fail_tokens or token not in self.books:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=self.books[token])
        return httpx.Response(404)

    def client(self):
        return PolymarketData(transport=httpx.MockTransport(self.handler))


def _fresh(book):
    return {**book, "timestamp": str(int(time.time() * 1000))}


BOOKS = {"yes1": _fresh(GOOD_YES), "no1": _fresh(GOOD_NO),
         "yes2": _fresh(raw_book("yes2", [(0.01, 9)], [(0.99, 9)])), "no2": _fresh(raw_book("no2", [(0.01, 9)], [(0.99, 9)]))}


def test_client_snapshot_batch_and_report():
    fake = FakePolymarket([gamma_market(1), gamma_market(2, tokens=("yes2", "no2"), bid=0.01, ask=0.99),
                           gamma_market(3, tokens=("y3", "n3"), closed=True)], BOOKS)
    rows, report = fake.client().snapshot(limit=10)
    assert report.markets == 2 and report.tokens == 4 and report.books_ok == 4 and report.batch_endpoint
    assert report.usable_rows == 2 and report.errors == []
    gamma_call = next(c for c in fake.calls if c[2] == "/markets")
    # 6C: over-fetches 2x so excluded (decided) markets do not shrink the scan
    assert gamma_call[3] == {"limit": "20", "active": "true", "closed": "false",
                             "order": "volume24hr", "ascending": "false"}
    assert [c[2] for c in fake.calls].count("/books") == 1 and "/book" not in [c[2] for c in fake.calls]
    placeholder = [r for r in rows if r["market_id"] == "2"]
    assert all(not r["usable"] and "PLACEHOLDER" in r["flags"] for r in placeholder)


def test_client_falls_back_to_single_books_and_reports_errors():
    fake = FakePolymarket([gamma_market(1)], BOOKS, batch=False, fail_tokens={"no1"})
    rows, report = fake.client().snapshot(limit=5)
    assert report.batch_endpoint is False and report.books_ok == 1 and report.books_missing == 1
    assert any("no1" in e for e in report.errors)
    assert [r["usable"] for r in rows] == [True, False] and rows[1]["flags"] == ["NO_BOOK"]


def test_gamma_failure_raises_for_hermes_retry():
    with pytest.raises(httpx.HTTPStatusError):
        FakePolymarket([], {}, gamma_status=503).client().snapshot(limit=5)


def test_snapshots_limit_counts_rows_and_client_is_read_only():
    markets = [gamma_market(i, tokens=(f"y{i}", f"n{i}")) for i in range(1, 8)]
    books = {f"{s}{i}": _fresh(raw_book(f"{s}{i}", [(0.48, 50)], [(0.50, 50)])) for i in range(1, 8) for s in "yn"}
    fake = FakePolymarket(markets, books)
    rows = fake.client().snapshots(limit=6)
    assert len(rows) == 6
    assert next(c for c in fake.calls if c[2] == "/markets")[3]["limit"] == "6"     # 3 markets, 2x over-fetch
    assert [c[2] for c in fake.calls].count("/books") == 1                          # books only for kept markets
    assert {(m, p) for m, _, p, _ in fake.calls} <= {("GET", "/markets"), ("POST", "/books"), ("GET", "/book")}


# ================================================================ app

def test_scan_never_watches_unusable_rows(tmp_path, monkeypatch):
    import jarvis.app.db as db_module
    import jarvis.app.main as main
    from jarvis.core.health import HealthManager
    from jarvis.core.lifecycle import LifecycleManager
    from jarvis.core.state import StateManager
    from jarvis.tests.test_app_hermes import FakeIntelligence

    fake = FakePolymarket([gamma_market(1), gamma_market(2, tokens=("yes2", "no2"), bid=0.01, ask=0.99)], BOOKS)
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "jarvis.db"))
    state = StateManager("PAPER")
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "health_manager", HealthManager(state))
    monkeypatch.setattr(main, "lifecycle", LifecycleManager(state, main.events))
    monkeypatch.setattr(main, "INTELLIGENCE_FACTORY", FakeIntelligence)
    monkeypatch.setattr(main, "MARKET_FACTORY", fake.client)
    for name in ("HERMES_HEALTH_INTERVAL_SECONDS", "HERMES_MARKET_SCAN_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "0")
    with TestClient(main.app) as client:
        rows = client.get("/api/scan").json()
        report = client.get("/api/market/report").json()
    bad = [r for r in rows if r["market_id"] == "2"]
    assert bad and all(r["decision"] == "IGNORE" and "data quality: PLACEHOLDER" in r["reasons"] for r in bad)
    assert report["markets"] == 2 and report["usable_rows"] == 2
