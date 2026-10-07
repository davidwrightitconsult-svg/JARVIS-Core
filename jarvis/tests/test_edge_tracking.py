"""
Phase 8C — edge estimates recorded and scored at resolution; the sports
calibration gap closed.

Principles under test:
- Closed markets are crawled most recently CLOSED first (closedTime), with
  endDate and then no ordering as fallbacks; 8A's endDate ordering started
  at 2029 end dates and never reached this week's games.
- The crawl resumes where the previous run stopped once it meets a page it
  has fully seen, and only remembers a depth after every market on the
  pages before it was handled.
- Volume is read from every Gamma volume field; a market with none is
  skipped as "volume unknown", never treated as 0.
- Fight and prop wording is sports; a matchmaking question or a politics
  question that says "by decision" is not.
- z carries a model-uncertainty floor: a thick bin cannot make an edge look
  near-certain; the purely statistical z is still reported.
- Every EDGE estimate becomes ONE CALIBRATION_EDGE signal per market (the
  side to buy, all model inputs), scored at its horizon AND held to
  resolution, and that strategy's verdict comes from the resolution result.
- Resolution checks rotate so long-dated markets cannot starve others.
- Nothing can trade.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.market.backfill import CRAWL_META, CalibrationBackfill, volume_of
from jarvis.market.client import PolymarketData
from jarvis.market.store import MarketStore
from jarvis.quant import QuantEngine
from jarvis.quant.fair import EdgeSettings, FairValueModel
from jarvis.quant.pipeline import QuantPipeline
from jarvis.quant.scorecard import build_scorecard, render_markdown
from jarvis.quant.scoring import Scorer
from jarvis.quant.segments import segment_of
from jarvis.quant.strategies import EDGE_STRATEGY, LIBRARY
from jarvis.tests.test_calibration import REF, closed, hourly
from jarvis.tests.test_fair_value import calib, longshot_market
from jarvis.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
from jarvis.tests.test_quant_core import NOW


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


class Gamma:
    """Closed-market pages (+ prices-history) with a configurable set of rejected orderings."""

    def __init__(self, markets, prices=None, reject=()):
        self.markets, self.prices, self.reject = markets, prices or {}, set(reject)
        self.calls = []

    def handler(self, request):
        p = dict(request.url.params)
        self.calls.append((request.url.path, p))
        if request.url.path == "/markets":
            if p.get("order") in self.reject:
                return httpx.Response(422, json={"error": "bad order"})
            off, lim = int(p.get("offset", 0)), int(p.get("limit", 100))
            return httpx.Response(200, json=self.markets[off:off + lim])
        if request.url.path == "/prices-history":
            start, end = int(p["startTs"]), int(p["endTs"])
            return httpx.Response(200, json={"history": [{"t": t, "p": v} for t, v in self.prices.get(p["market"], [])
                                                         if start <= t <= end]})
        return httpx.Response(404)

    def client(self):
        return PolymarketData(transport=httpx.MockTransport(self.handler))

    def pages(self):
        return [int(p.get("offset", 0)) for path, p in self.calls if path == "/markets"]


def priced(ids):
    return {t: hourly(0.7 if t.startswith("y") else 0.3) for i in ids for t in (f"y{i}", f"n{i}")}


# ================================================================ backfill: order, volume, crawl

def test_closed_markets_are_ordered_by_close_time_with_fallbacks():
    g = Gamma([closed(1)])
    g.client().closed_markets(10)
    assert g.calls[-1][1]["order"] == "closedTime" and g.calls[-1][1]["ascending"] == "false"
    g = Gamma([closed(1)], reject={"closedTime"})
    items, n = g.client().closed_markets(10)
    assert n == 1 and g.calls[-1][1]["order"] == "endDate"
    g = Gamma([closed(1)], reject={"closedTime", "endDate"})
    assert g.client().closed_markets(10)[1] == 1 and "order" not in g.calls[-1][1]


def test_volume_from_every_gamma_field():
    assert volume_of({"volumeNum": 5000}) == 5000
    assert volume_of({"volumeNum": None, "volume": "2500.5"}) == 2500.5
    assert volume_of({"volumeClob": 800, "volumeAmm": 700}) == 1500
    assert volume_of({"volumeNum": 25, "volumeClob": 3000}) == 3000         # the largest known figure
    assert volume_of({"volumeNum": None, "volume": "n/a"}) is None
    assert volume_of({}) is None


def test_8a_volume_skips_are_reopened_once(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    store.record_calibration_skip("2", "volume < 1000")                     # 8A: blank volume read as 0
    store.record_calibration_skip("3", "not cleanly resolved")
    clob = {**closed(2), "volumeNum": None, "volumeClob": 4000}
    g = Gamma([clob, closed(3, prices='["0.5", "0.5"]')], priced([2]))
    first = CalibrationBackfill(g.client(), store, pace_seconds=0).run()
    assert first["volume_skips_reopened"] == 1 and first["added"] == 1 and first["seen"] == 1
    assert "volume_skips_reopened" not in CalibrationBackfill(g.client(), store, pace_seconds=0).run()


def test_unknown_volume_is_its_own_skip_reason(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    blank = {**closed(1), "volumeNum": None}
    blank.pop("volume", None)
    clob = {**closed(2), "volumeNum": None, "volumeClob": 4000}
    g = Gamma([blank, clob], priced([2]))
    summary = CalibrationBackfill(g.client(), store, pace_seconds=0).run()
    assert summary["skipped"] == {"volume unknown": 1} and summary["added"] == 1


def test_crawl_resumes_where_the_last_run_stopped(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    markets = [closed(i) for i in range(1, 7)]
    g = Gamma(markets, priced(range(1, 7)))
    first = CalibrationBackfill(g.client(), store, pace_seconds=0).run(max_pages=2, page_size=2)
    assert first["added"] == 4 and store.meta_get(CRAWL_META) == "4"
    g.calls.clear()
    second = CalibrationBackfill(g.client(), store, pace_seconds=0).run(max_pages=3, page_size=2)
    assert second["resumed_at_offset"] == 4 and second["added"] == 2
    assert g.pages() == [0, 4, 6]                                           # page 2 (all known) never re-read
    assert store.meta_get(CRAWL_META) == "0"                                # reached the end: start over


def test_newest_closures_first_then_depth_only_after_complete_pages(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    g = Gamma([closed(i) for i in range(1, 5)], priced(range(1, 20)))
    CalibrationBackfill(g.client(), store, pace_seconds=0).run(max_markets=3, page_size=2)
    assert store.meta_get(CRAWL_META) == "2"                                # page 2 not finished: not remembered
    g.markets = [closed(10), closed(11)] + g.markets                        # two games just finished
    summary = CalibrationBackfill(g.client(), store, pace_seconds=0).run(page_size=2)
    assert summary["added"] == 3 and store.calibration_known(["10", "11", "4"]) == {"10", "11", "4"}


# ================================================================ segments

@pytest.mark.parametrize("q", [
    "Will Visconde win by KO or TKO?", "Will Jones win by submission?", "Fighter wins by decision?",
    "Will the fight go the distance?", "Will it end in round 2 winner?", "Game Handicap: NAVI (-1.5) vs Vitality (+1.5)",
    "Set 1 Games O/U 9.5", "Lakers 1st Half Moneyline", "UFC 320: Who will win?", "Djokovic match winner",
    "Will X win by KO/TKO?", "Map 1 Total Rounds: Over/Under 24.5",
    "Map 1 Rounds Handicap: BASEMENT BOYS (-3.5) vs Prestige (+3.5)", "Lakers Over/Under 210",
    "Counter-Strike: 9INE vs Matrix (BO1) - ROG Journey Group C", "Will the Chiefs win the Super Bowl?",
    "Will Filippo Celli lead the 2026 Alfred Dunhill Links Championship following the second round?",
    "Will Arsenal win the Premier League?", "Will Verstappen win the 2026 F1 title?"])
def test_fight_and_prop_wording_is_sports(q):
    assert segment_of(q) == "sports"


@pytest.mark.parametrize("q", [
    "Will Aljamain Sterling fight Kevin Vallejos next?",                     # matchmaking resolves over weeks
    "Will Trump win the 2028 election by decision of the House?",
    "Will Bitcoin dip to $82,500 September 29-October 5?", "Will Bitcoin go up or down this year?",
    "Berlin State Election: SPD Over/Under 13%?",                           # 8C.2: jarvis020 false positives
    "Mecklenburg-Vorpommern Parliamentary Election: Linke Over/Under 11%?",
    "Sweden Parliamentary Election: KD Over/Under 6.5%?",
    "US announces end of Iranian blockade by December 31?", "Will the Fed cut rates in December?"])
def test_non_games_stay_out_of_sports(q):
    assert segment_of(q) == "binary"


# ================================================================ z floor

def test_z_has_a_model_uncertainty_floor(store):
    calib(store, 0.07, 20000, 400)                                          # a very thick bin
    m = longshot_market(store)
    e = FairValueModel(store).evaluate(m)
    assert e["z_statistical"] > 10 and e["z"] < 5                           # 8B.2 reported the first
    assert e["z"] == pytest.approx(e["edge"] / e["total_se"], abs=0.02)
    assert e["total_se"] >= 0.01
    raw = FairValueModel(store, EdgeSettings(model_se=0)).evaluate(m)
    assert raw["z"] == raw["z_statistical"]                                  # floor off = 8B.2 behaviour
    assert FairValueModel(store, EdgeSettings(model_se=0.05)).evaluate(m)["status"] == "NO_EDGE"
    with pytest.raises(ValueError):
        EdgeSettings(model_se=0.5)


def test_model_se_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_EDGE_MODEL_SE", "0.02")
    assert EdgeSettings.from_environment().model_se == 0.02


# ================================================================ CALIBRATION_EDGE signals

def pipeline(store, strategies=LIBRARY, fair=True):
    return QuantPipeline(QuantEngine(store), strategies, fair_model=FairValueModel(store) if fair else None)


def test_edge_becomes_one_signal_per_market(store):
    calib(store, 0.07, 2000, 40)
    longshot_market(store)
    first = pipeline(store).evaluate(now=NOW)
    assert first["by_strategy"].get(EDGE_STRATEGY) == 1
    [sig] = [s for s in store.outcomes(days=3650) if s["strategy"] == EDGE_STRATEGY]
    assert sig["token_id"] == "no1" and sig["direction"] == "UP" and sig["best_ask"] == 0.94
    assert sig["horizon_hours"] == 168 and sig["strategy_version"] == "8C.1"
    import json
    with store._db() as db:
        inputs = json.loads(db.execute("SELECT inputs FROM signals WHERE strategy=?", (EDGE_STRATEGY,)).fetchone()[0])
    assert {"fair", "edge", "z", "z_statistical", "segment", "bin", "bin_samples", "stake_fraction"} <= set(inputs)
    assert inputs["segment"] == "binary" and inputs["edge"] >= 0.02
    again = pipeline(store).evaluate(now=NOW + timedelta(days=30))           # long after the horizon
    assert EDGE_STRATEGY not in again["by_strategy"]                          # never re-counted


def test_no_edge_no_model_or_disabled_means_no_signal(store):
    longshot_market(store)                                                   # no calibration at all
    assert EDGE_STRATEGY not in pipeline(store).evaluate(now=NOW)["by_strategy"]
    calib(store, 0.07, 2000, 40)
    assert EDGE_STRATEGY not in pipeline(store, fair=False).evaluate(now=NOW)["by_strategy"]
    others = tuple(s for s in LIBRARY if s.name != EDGE_STRATEGY)
    assert EDGE_STRATEGY not in pipeline(store, strategies=others).evaluate(now=NOW)["by_strategy"]


def test_edge_scored_held_to_resolution_and_judged_there(store):
    calib(store, 0.07, 2000, 40)
    longshot_market(store)
    pipeline(store).evaluate(now=NOW)
    store.record_resolution("1", {"yes1": 0.0, "no1": 1.0}, now=NOW + timedelta(hours=10))
    Scorer(store).score_due(NOW + timedelta(hours=200))
    [row] = [r for r in store.outcomes(days=3650) if r["strategy"] == EDGE_STRATEGY]
    assert row["status"] == "RESOLVED_BEFORE_EXIT" and row["resolution_net"] == 0.06                                      # bought NO at 94c, paid $1
    card = build_scorecard(store)
    s = {x["strategy"]: x for x in card["strategies"]}[EDGE_STRATEGY]
    assert s["judged_on"] == "resolution" and s["held_to_resolution"]["resolved"] == 1
    assert s["verdict"] == s["held_to_resolution"]["verdict"] and s["verdict"].startswith("INSUFFICIENT_DATA")
    assert "CALIBRATION_EDGE (judged here)" in render_markdown(card, "2026-09-30")


def test_resolution_verdict_needs_the_whole_interval_above_zero(store):
    from jarvis.quant.scorecard import _resolution_stats, verdict
    win = [{"resolution_net": 0.05, "cost": 0.9, "direction": "UP", "best_ask": 0.9, "best_bid": 0.88}] * 40
    assert verdict(_resolution_stats(win)).startswith("PROMISING")
    mixed = win[:20] + [{**win[0], "resolution_net": -0.9}] * 20
    assert verdict(_resolution_stats(mixed)) in ("NEGATIVE_AFTER_COSTS", "INCONCLUSIVE")


def test_resolution_checks_rotate(store):
    ids = [str(i) for i in range(1, 26)]
    past = (NOW - timedelta(days=1)).isoformat()
    with store._db() as db:
        for i in ids:
            db.execute("INSERT INTO markets(market_id, question, end_date, first_seen, last_seen) VALUES(?,?,?,?,?)",
                       (i, f"q{i}", past, past, past))
            db.execute("INSERT INTO signals(strategy, strategy_version, market_id, token_id, direction, strength,"
                       " horizon_hours, observed_at, expires_at, inputs) VALUES('X', '1', ?, ?, 'UP', 0.5, 1, ?, ?, '{}')",
                       (i, f"t{i}", past, past))
    a = store.unresolved_signal_markets(NOW, limit=20)
    b = store.unresolved_signal_markets(NOW, limit=20)
    assert len(a) == 20 and len(b) == 20 and set(ids) <= set(a) | set(b)      # nobody starves
    few = MarketStore(store.path.parent / "few.db")
    assert few.unresolved_signal_markets(NOW) == []


# ================================================================ app

def test_app_records_edge_signals_from_the_live_scan(tmp_path, monkeypatch):
    from jarvis.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    calib(store, 0.07, 2000, 40)
    books = {"yes1": _fresh(raw_book("yes1", [(0.06, 50000)], [(0.08, 50000)])),
             "no1": _fresh(raw_book("no1", [(0.92, 50000)], [(0.94, 50000)]))}
    from jarvis.tests.test_fair_value import IN_A_WEEK
    main = _app(tmp_path, monkeypatch, FakePolymarket([{**gamma_market(1), "endDate": IN_A_WEEK}], books), store)
    with TestClient(main.app) as c:
        [row] = c.get("/api/scan").json()
        assert row["edge_status"] == "EDGE" and "CALIBRATION_EDGE:UP" in row["signals"]
        sigs = c.get("/api/quant/signals", params={"strategy": "calibration_edge"}).json()["signals"]
        assert len(sigs) == 1 and sigs[0]["token_id"] == "no1"
        edges = c.get("/api/quant/edges").json()
        assert "CALIBRATION_EDGE" in edges["note"] and edges["model"]["settings"]["model_se"] == 0.01
        card = c.get("/api/quant/scorecard").json()
        assert {s["strategy"]: s for s in card["strategies"]}[EDGE_STRATEGY]["signals"] == 1
        assert c.get("/health").json()["execution_locked"] is True


# ================================================================ 8C.2: sports are out of scope

@pytest.mark.parametrize("q", ["Bitcoin Up or Down - October 2, 12:35PM-12:40PM ET",
                               "Solana Up or Down - September 2, 4:50PM-4:55PM ET", "XRP Up or Down - July 4, 9AM ET"])
def test_five_minute_markets_are_their_own_out_of_scope_segment(q):
    from jarvis.market.segments import excluded_segments
    assert segment_of(q) == "micro" and "micro" in excluded_segments()


def test_gamma_sports_fields_and_tags_mark_a_market_as_sports():
    from jarvis.market.models import parse_market
    from jarvis.market.segments import sports_hint_of
    plain = gamma_market(1)
    assert parse_market(plain).segment == "binary" and not sports_hint_of(plain)
    assert parse_market({**plain, "sportsMarketType": "moneyline"}).segment == "sports"
    assert parse_market({**plain, "gameId": "g-77"}).segment == "sports"
    assert parse_market({**plain, "events": [{"tags": [{"slug": "sports"}]}]}).segment == "sports"
    assert parse_market({**plain, "tags": ["Politics"]}).segment == "binary"
    assert parse_market({**plain, "negRisk": True}).segment == "field"


def test_scope_setting(monkeypatch):
    from jarvis.market.segments import excluded_segments
    assert excluded_segments() == {"sports", "micro"}                        # the default (9B.2: micro too)
    monkeypatch.setenv("JARVIS_EXCLUDED_SEGMENTS", "none")
    assert excluded_segments() == frozenset()
    monkeypatch.setenv("JARVIS_EXCLUDED_SEGMENTS", "sports, field")
    assert excluded_segments() == {"sports", "field"}
    for bad in ("football", "sports,micro,field,binary"):
        monkeypatch.setenv("JARVIS_EXCLUDED_SEGMENTS", bad)
        with pytest.raises(ValueError):
            excluded_segments()


def test_scan_drops_sports_before_fetching_their_books(tmp_path):
    game = {**gamma_market(1, tokens=("gy", "gn")), "question": "Chicago Cubs vs. San Diego Padres"}
    prop = {**gamma_market(2, tokens=("py", "pn")), "sportsMarketType": "totals"}
    event = gamma_market(3, tokens=("yes1", "no1"))
    books = {t: _fresh(raw_book(t, [(0.48, 5000)], [(0.50, 5000)])) for t in ("gy", "gn", "py", "pn", "yes1", "no1")}
    fake = FakePolymarket([game, prop, event], books)
    client = fake.client()
    client.exclude_segments = frozenset({"sports"})
    client.store = MarketStore(tmp_path / "m.db")
    rows, report = client.snapshot(limit=2)
    assert {r["market_id"] for r in rows} == {"3"} and report.excluded_reasons == {"SEGMENT_SPORTS": 2}
    assert report.tokens == 2                                                # only the event market's books
    assert client.store.find_markets("Cubs Padres") == []                    # never stored
    everything = fake.client()
    assert {r["market_id"] for r in everything.snapshot(limit=3)[0]} == {"1", "2", "3"}   # library default: no scope


def test_universe_never_makes_an_out_of_scope_market_eligible(store):
    from jarvis.quant import UniverseRules
    from jarvis.quant.universe import evaluate
    m = longshot_market(store, yes_mid=0.45, question="Team A vs. Team B")
    assert evaluate(m, UniverseRules()) == []                               # library default: in scope
    assert evaluate(m, UniverseRules(excluded_segments=("sports",))) == ["OUT_OF_SCOPE: sports"]
    assert UniverseRules.from_environment().excluded_segments == ("micro", "sports")
    engine = QuantEngine(store, UniverseRules.from_environment())
    calib(store, 0.45, 2000, 600, horizon=24, question="A vs. B")           # even with a 'real' sports edge
    summary = QuantPipeline(engine, LIBRARY, fair_model=FairValueModel(store)).evaluate(now=NOW)
    assert summary["eligible"] == 0 and summary["signals_new"] == 0
    assert FairValueModel(store).edges(engine.features(now=NOW)) == []


def test_backfill_skips_sports_without_fetching_history_and_reopens_on_scope_change(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    game = {**closed(1), "question": "Tigers vs. Lions"}
    award = {**closed(2), "sportsMarketType": "futures"}
    g = Gamma([game, award, closed(3)], priced([1, 2, 3]))
    bf = CalibrationBackfill(g.client(), store, pace_seconds=0, exclude_segments={"sports"})
    summary = bf.run(max_markets=1)                                          # cheap skips do not use the budget
    assert summary["added"] == 1 and summary["skipped"] == {"out of scope: sports": 2}
    assert {p["market"] for path, p in g.calls if path == "/prices-history"} == {"y3", "n3"}
    assert bf.status()["exclude_segments"] == ["sports"]
    back = CalibrationBackfill(g.client(), store, pace_seconds=0).run()      # sports brought back into scope
    assert back["scope_skips_reopened"] == 2 and back["added"] == 2


def test_app_scope_in_status_and_scan(tmp_path, monkeypatch):
    from jarvis.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    game = {**gamma_market(1, tokens=("gy", "gn")), "question": "Chicago Cubs vs. San Diego Padres"}
    books = {t: _fresh(raw_book(t, [(0.48, 50000)], [(0.50, 50000)])) for t in ("gy", "gn", "yes1", "no1")}
    fake = FakePolymarket([game, gamma_market(2)], books)
    main = _app(tmp_path, monkeypatch, fake, store)
    with TestClient(main.app) as c:
        main.poly.exclude_segments = frozenset({"sports"})
        rows = c.get("/api/scan").json()
        assert [r["market_id"] for r in rows] == ["2"]
        assert c.get("/api/quant/universe").json()["rules"]["excluded_segments"] == ["micro", "sports"]
        assert c.get("/health").json()["execution_locked"] is True
