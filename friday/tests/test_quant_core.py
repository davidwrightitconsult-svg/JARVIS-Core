"""
Phase 7A — Quant Core: deterministic features and the tradable universe.

Principles under test:
- Features are pure functions of FRIDAY's recorded history: price changes
  only when history reaches back far enough, volatility from successive
  mids, book imbalance from recorded levels, pricing sums across outcomes.
- The universe gate is auditable: every rejection has a reason code, and
  unusable/stale/thin/wide/extreme/inactive markets are never eligible.
- The UI scan is served from recorded data (no Polymarket call per refresh);
  `edge` stays None; nothing can trade.
- Markets with no order book at all leave the scan; a fetch FAILURE is never
  mistaken for that.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from friday.market import parse_book, parse_market, snapshot_rows
from friday.market.context import MarketContext
from friday.market.store import MarketStore
from friday.quant import QuantEngine, UniverseRules, evaluate
from friday.quant.features import change_over, imbalance, volatility
from friday.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _book(token, mid, size=2000, ts=None):
    ms = int((ts or NOW).timestamp() * 1000)
    return raw_book(token, [(round(mid - 0.01, 2), size), (round(mid - 0.02, 2), size)],
                    [(round(mid + 0.01, 2), size), (round(mid + 0.02, 2), size)], ts=ms)


def _record(store, when, mids, market=None, volume=12345.6, sizes=(2000, 2000)):
    """Record one run at `when`: mids = (yes_mid, no_mid)."""
    m = parse_market({**(market or gamma_market()), "volume24hr": volume})
    books = {"yes1": parse_book(_book("yes1", mids[0], sizes[0], when)),
             "no1": parse_book(_book("no1", mids[1], sizes[1], when))}
    rows = snapshot_rows(m, books, now=when)
    return store.record([m], rows, {"observed_at": when.isoformat(timespec="seconds"), "markets": 1})


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "d" / "m.db")


def _history(store):
    for hours_ago, yes in ((24.5, 0.40), (6.5, 0.45), (1.5, 0.47), (0.5, 0.48), (0, 0.50)):
        _record(store, NOW - timedelta(hours=hours_ago), (yes, round(1 - yes, 2)))


# ================================================================ pure features

def test_change_needs_history_that_reaches_back():
    h = [{"observed_at": (NOW - timedelta(hours=2)).isoformat(), "mid": 0.40, "usable": True},
         {"observed_at": NOW.isoformat(), "mid": 0.50, "usable": True}]
    assert change_over(h, 1, 0.50, NOW) == 0.10
    assert change_over(h, 6, 0.50, NOW) is None                 # no data 6h back: None, never 0
    assert change_over(h, 1, None, NOW) is None                 # unusable now: None
    h[0]["usable"] = False
    assert change_over(h, 1, 0.50, NOW) is None                 # unusable past point is skipped


def test_volatility_and_imbalance():
    h = [{"mid": m, "usable": True} for m in (0.40, 0.42, 0.40, 0.42, 0.40)]
    assert volatility(h) == 0.02                                # changes +2c/-2c/+2c/-2c
    assert volatility(h[:2]) is None                            # needs 2+ changes
    top_bid, top_ask, imb = imbalance({"bids": [[0.5, 300], [0.49, 100]], "asks": [[0.52, 100]]})
    assert top_bid == 150.0 and top_ask == 52.0 and imb == round((199 - 52) / 251, 4)
    assert imbalance({"bids": [], "asks": []}) == (None, None, None)


def test_market_features_from_recorded_history(store):
    _history(store)
    [m] = QuantEngine(store).features(now=NOW)
    yes, no = m.outcomes
    assert (yes.mid, yes.change_1h, yes.change_6h, yes.change_24h) == (0.50, 0.03, 0.05, 0.10)
    assert no.change_24h == -0.10 and yes.observations == 4          # 24h window (24.5h point outside)
    # equal share sizes, but bids sit at lower prices than asks: (1940 - 2060) / 4000
    assert yes.half_spread == 0.01 and yes.imbalance == -0.03 and yes.top_bid_usd == round(0.49 * 2000, 2)
    assert yes.range_24h == [0.45, 0.50] and yes.volatility is not None and yes.age_seconds == 0
    assert m.ask_sum == 1.02 and m.bid_sum == 0.98                   # 1c spread each side
    assert m.hours_to_end > 24 * 365 and m.eligible and m.attention > 50


# ================================================================ universe

def test_universe_reasons(store):
    rules = UniverseRules()
    _record(store, NOW, (0.50, 0.50))
    [ok] = QuantEngine(store).features(now=NOW)
    assert evaluate(ok, rules) == []
    thin = QuantEngine(store, UniverseRules(min_depth_usd=1e7)).features(now=NOW)[0]
    assert thin.reasons == ["DEPTH<1e+07"] and not thin.eligible and thin.attention == 0
    assert QuantEngine(store, UniverseRules(max_spread=0.01)).features(now=NOW)[0].reasons == ["SPREAD>0.01"]
    assert QuantEngine(store, UniverseRules(min_volume_24h=1e9)).features(now=NOW)[0].reasons == ["VOLUME<1e+09"]
    assert QuantEngine(store, UniverseRules(max_price=0.4)).features(now=NOW)[0].reasons == ["PRICE_EXTREME"]
    later = NOW + timedelta(hours=1)
    assert "STALE_OBSERVATION" in QuantEngine(store).features(now=later)[0].reasons


def test_unusable_data_is_never_eligible(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    m = parse_market(gamma_market())
    books = {"yes1": parse_book(raw_book("yes1", [(0.40, 5000)], [], ts=int(NOW.timestamp() * 1000))),
             "no1": parse_book(_book("no1", 0.60))}
    store.record([m], snapshot_rows(m, books, now=NOW), {"observed_at": NOW.isoformat(timespec="seconds")})
    engine = QuantEngine(store)
    [f] = engine.features(now=NOW)
    assert not f.eligible and f.reasons[0] == "data quality: ONE_SIDED"
    u = engine.universe(now=NOW)
    assert u["eligible"] == 0 and u["rejected_by"] == {"DATA_ONE_SIDED": 1}
    assert "not a signal" in u["note"]


def test_rules_from_env_and_validation(monkeypatch):
    monkeypatch.setenv("FRIDAY_QUANT_MIN_DEPTH_USD", "2500")
    monkeypatch.setenv("FRIDAY_QUANT_MAX_SPREAD", "0.03")
    r = UniverseRules.from_environment()
    assert r.min_depth_usd == 2500 and r.max_spread == 0.03 and r.min_price == 0.03
    for bad in ({"min_price": 0.9, "max_price": 0.1}, {"max_spread": 0}, {"max_age_seconds": 5},
                {"min_depth_usd": -1}):
        with pytest.raises(ValueError):
            UniverseRules(**bad)


# ================================================================ NO_BOOK exclusion (7A)

def _nobook_universe():
    books = {"yes1": _fresh(_book("yes1", 0.5)), "no1": _fresh(_book("no1", 0.5)),
             "y3": _fresh(_book("y3", 0.3)), "n3": _fresh(_book("n3", 0.7))}
    markets = [{**gamma_market(8, tokens=("yb", "nb")), "question": "Listed but no book"},
               gamma_market(1), gamma_market(3, tokens=("y3", "n3"))]
    return markets, books


def test_markets_without_any_book_leave_the_scan():
    markets, books = _nobook_universe()
    fake = FakePolymarket(markets, books)
    rows, report = fake.client().snapshot(limit=2)
    assert {r["market_id"] for r in rows} == {"1", "3"}
    assert report.excluded_reasons == {"NO_BOOK": 1} and report.errors == []


def test_fetch_failure_is_not_mistaken_for_no_book():
    markets, books = _nobook_universe()
    fake = FakePolymarket(markets, books, batch=False, fail_tokens={"yb", "nb"})
    rows, report = fake.client().snapshot(limit=2)
    kept = {r["market_id"] for r in rows}
    assert "8" in kept and report.excluded == 0 and len(report.errors) == 2   # kept, flagged, reported
    assert all(r["flags"] == ["NO_BOOK"] and not r["usable"] for r in rows if r["market_id"] == "8")


# ================================================================ context + CLI

def test_claude_context_carries_quant_features(store):
    _history(store)
    block = MarketContext(store, engine=QuantEngine(store)).context_for("x", ["1"])
    q = block["markets"][0]["quant"]
    assert q["eligible"] is True and q["outcomes"]["Yes"]["change_1h"] == 0.03
    assert "not a trading signal" in block["notice"] and "data/liquidity gate" in block["notice"]


def test_cli(store, capsys):
    from friday.quant.__main__ import main as cli
    _history(store)
    assert cli(["universe", "--db", str(store.path)]) == 0
    assert json.loads(capsys.readouterr().out)["eligible"] == 1
    assert cli(["features", "--db", str(store.path)]) == 0
    out = capsys.readouterr().out
    assert "ELIGIBLE attention" in out and "24h +0.100" in out


# ================================================================ app

def _app(tmp_path, monkeypatch, fake, store):
    import friday.app.db as db_module
    import friday.app.main as main
    from friday.core.health import HealthManager
    from friday.core.lifecycle import LifecycleManager
    from friday.core.state import StateManager
    from friday.tests.test_app_hermes import FakeIntelligence

    def factory():
        client = fake.client()
        client.store = store
        client.filter_status = {"exclude_settled": True}
        return client

    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "friday.db"))
    state = StateManager("PAPER")
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "health_manager", HealthManager(state))
    monkeypatch.setattr(main, "lifecycle", LifecycleManager(state, main.events))
    monkeypatch.setattr(main, "INTELLIGENCE_FACTORY", FakeIntelligence)
    monkeypatch.setattr(main, "MARKET_FACTORY", factory)
    for name in ("HERMES_HEALTH_INTERVAL_SECONDS", "HERMES_MARKET_SCAN_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "0")
    return main


def test_scan_is_served_from_recorded_data(tmp_path, monkeypatch, store):
    markets, books = _nobook_universe()
    books["yes1"] = _fresh(_book("yes1", 0.5, 4000))
    books["no1"] = _fresh(_book("no1", 0.5, 4000))
    fake = FakePolymarket(markets, books)
    main = _app(tmp_path, monkeypatch, fake, store)
    with TestClient(main.app) as c:
        first = c.get("/api/scan").json()                     # store empty: one live snapshot
        gamma_calls = sum(1 for call in fake.calls if call[2] == "/markets")
        second = c.get("/api/scan").json()                    # fresh: served from the store
        assert sum(1 for call in fake.calls if call[2] == "/markets") == gamma_calls
        assert first == second and store.stats()["counts"]["runs"] == 1
        watch = [r for r in first if r["decision"] == "WATCH"]
        assert watch and all(r["edge"] is None and r["eligible"] for r in watch)
        assert all(r["decision"] == "IGNORE" and r["reasons"] for r in first if not r["eligible"])
        assert {"title", "price", "score", "decision"} <= set(first[0])            # UI contract
        u = c.get("/api/quant/universe").json()
        assert u["markets"] == 2 and u["eligible"] == len(watch) and u["status"]["enabled"] is True
        f = c.get("/api/quant/features", params={"eligible_only": True}).json()
        assert len(f["markets"]) == len(watch) and "Not signals" in f["note"]
        assert c.get("/health").json()["execution_locked"] is True
