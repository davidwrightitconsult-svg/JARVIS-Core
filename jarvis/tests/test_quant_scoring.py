"""
Phase 7C — signal outcome scoring, resolutions, scorecard.

Principles under test:
- A signal is scored against the first USABLE recorded observation at or
  after its expiry, within a tolerance window; the executable result crosses
  the spread at entry AND exit. Nothing is guessed: no data -> NO_EXIT_DATA.
- Resolutions come only from markets that are closed AND settled at 0/1.
- Verdicts are conservative: small samples say how much more is needed;
  PROMISING is never called validated.
- Scoring is idempotent; the Quant Core stays network-free (the resolver is
  injected); nothing can trade.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.knowledge import KnowledgeRecorder, KnowledgeVault
from jarvis.market.client import PolymarketData
from jarvis.market.store import MarketStore
from jarvis.quant import LIBRARY, QuantEngine, QuantPipeline
from jarvis.quant.scorecard import MIN_SAMPLE, build_scorecard, render_markdown, verdict
from jarvis.quant.scoring import Scorer, resolution_net, score_against
from jarvis.tests.test_market_data import gamma_market
from jarvis.tests.test_quant_core import NOW, _record


def _sig(direction="UP", mid=0.50, bid=0.49, ask=0.51, hours_ago=2.0, horizon=1.0, strategy="BOOK_IMBALANCE",
         token="yes1", market="1"):
    at = NOW - timedelta(hours=hours_ago)
    return {"run_id": 1, "observed_at": at.isoformat(timespec="seconds"), "strategy": strategy,
            "strategy_version": "7B.1", "market_id": market, "token_id": token, "outcome": "Yes",
            "direction": direction, "strength": 0.5, "horizon_hours": horizon,
            "expires_at": (at + timedelta(hours=horizon)).isoformat(timespec="seconds"),
            "mid": mid, "best_bid": bid, "best_ask": ask, "half_spread": round((ask - bid) / 2, 4),
            "inputs": {"test": True}}


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "d" / "m.db")


# ================================================================ math

def test_executable_result_crosses_the_spread_both_ways():
    up = score_against(_sig("UP"), exit_mid=0.55, exit_bid=0.54, exit_ask=0.56)
    assert up["mid_move"] == 0.05 and up["net"] == 0.03 and up["cost"] == 0.51      # 0.54 - 0.51
    assert up["return_on_cost"] == round(0.03 / 0.51, 4)
    down = score_against(_sig("DOWN"), exit_mid=0.55, exit_bid=0.54, exit_ask=0.56)
    assert down["mid_move"] == -0.05 and down["net"] == -0.07 and down["cost"] == 0.51   # 0.49 - 0.56
    flat = score_against(_sig("UP"), exit_mid=0.50, exit_bid=0.49, exit_ask=0.51)
    assert flat["mid_move"] == 0 and flat["net"] == -0.02            # no move still costs the spread
    shot = score_against(_sig("DOWN", mid=0.035, bid=0.03, ask=0.04), 0.025, 0.02, 0.03)
    assert shot["net"] == 0.0 and shot["cost"] == 0.97               # right direction, spread ate it
    assert resolution_net(_sig("UP"), 1.0) == 0.49 and resolution_net(_sig("DOWN"), 0.0) == 0.49
    assert resolution_net(_sig("DOWN", bid=0.03, ask=0.04), 0.0) == 0.03


# ================================================================ scorer

def test_scored_against_first_usable_observation_after_expiry(store):
    store.record_signals([_sig("UP", hours_ago=2.0, horizon=1.0)])            # expired 1h ago
    _record(store, NOW - timedelta(hours=1.5), (0.52, 0.48))                 # before expiry: ignored
    _record(store, NOW - timedelta(minutes=50), (0.56, 0.44))                # first after expiry
    _record(store, NOW - timedelta(minutes=20), (0.40, 0.60))                # later: ignored
    summary = Scorer(store).score_due(NOW)
    assert summary["by_status"] == {"SCORED": 1} and summary["waiting_for_exit"] == 0
    [row] = store.outcomes()
    assert row["exit_mid"] == 0.56 and row["exit_bid"] == 0.55 and row["net"] == 0.04 and row["mid_move"] == 0.06
    assert Scorer(store).score_due(NOW)["scored"] == 0                       # idempotent


def test_waiting_then_no_exit_data(store):
    store.record_signals([_sig(hours_ago=1.2, horizon=1.0)])                 # expired 12 min ago
    s = Scorer(store).score_due(NOW)
    assert s["scored"] == 0 and s["waiting_for_exit"] == 1                   # window (1h) still open
    later = NOW + timedelta(hours=2)
    assert Scorer(store).score_due(later)["by_status"] == {"NO_EXIT_DATA": 1}
    assert store.outcomes()[0]["net"] is None                                # never guessed


def test_preview_writes_nothing(store):
    store.record_signals([_sig(hours_ago=2.0)])
    _record(store, NOW - timedelta(minutes=50), (0.56, 0.44))
    preview = Scorer(store, record=False).score_due(NOW)
    assert preview["scored"] == 1 and preview["outcomes"][0]["net"] == 0.04
    assert store.outcomes()[0]["status"] is None


def test_resolution_before_exit_and_after(store):
    store.record_signals([_sig("DOWN", mid=0.05, bid=0.04, ask=0.06, hours_ago=60, horizon=24,
                               strategy="LONGSHOT_BIAS"),                   # exit window closed 24h ago
                          _sig("UP", hours_ago=3, horizon=1)])
    _record(store, NOW - timedelta(hours=1.5), (0.55, 0.45))                 # exit for the 1h UP signal
    Scorer(store).score_due(NOW)
    rows = {r["strategy"]: r for r in store.outcomes()}
    assert rows["BOOK_IMBALANCE"]["status"] == "SCORED" and rows["LONGSHOT_BIAS"]["status"] == "NO_EXIT_DATA"
    store.record_resolution("1", {"yes1": 0.0, "no1": 1.0}, now=NOW)
    assert Scorer(store).apply_resolutions() == 2          # both gain a held-to-resolution result,
    rows = {r["strategy"]: r for r in store.outcomes()}    # even the one with no horizon exit
    assert rows["BOOK_IMBALANCE"]["resolution_value"] == 0.0 and rows["BOOK_IMBALANCE"]["resolution_net"] == -0.51
    assert rows["LONGSHOT_BIAS"]["status"] == "NO_EXIT_DATA" and rows["LONGSHOT_BIAS"]["resolution_net"] == 0.04
    assert Scorer(store).apply_resolutions() == 0          # idempotent
    held = {s["strategy"]: s["held_to_resolution"] for s in build_scorecard(store)["strategies"]}
    h = held["LONGSHOT_BIAS"]
    assert (h["resolved"], h["avg_net"], h["win_rate"]) == (1, 0.04, 1.0) and h["verdict"].startswith("INSUFFICIENT")
    store2 = MarketStore(store.path.parent / "b.db")
    store2.record_signals([_sig("DOWN", mid=0.05, bid=0.04, ask=0.06, hours_ago=30, horizon=24)])
    store2.record_resolution("1", {"yes1": 0.0, "no1": 1.0}, now=NOW - timedelta(hours=10))
    Scorer(store2).score_due(NOW)
    [r] = store2.outcomes()
    assert r["status"] == "RESOLVED_BEFORE_EXIT" and r["net"] == 0.04 and r["resolution_net"] == 0.04


def test_which_markets_to_ask_about(store):
    _record(store, NOW - timedelta(hours=5), (0.5, 0.5))                     # last seen 5h ago
    store.record_signals([_sig(hours_ago=5)])
    assert store.unresolved_signal_markets(NOW) == ["1"]
    store.record_resolution("1", {"yes1": 1.0, "no1": 0.0})
    assert store.unresolved_signal_markets(NOW) == []


# ================================================================ resolver (read-only client)

def _gamma(payloads):
    def handler(request):
        mid = request.url.path.rsplit("/", 1)[-1]
        if mid not in payloads:
            return httpx.Response(404)
        return httpx.Response(200, json=payloads[mid])
    return PolymarketData(transport=httpx.MockTransport(handler))


def test_only_closed_and_settled_markets_count_as_resolved():
    client = _gamma({
        "1": {**gamma_market(1), "closed": True, "outcomePrices": '["1", "0"]'},
        "2": {**gamma_market(2, tokens=("y2", "n2")), "closed": True, "outcomePrices": '["0.5", "0.5"]'},
        "3": {**gamma_market(3, tokens=("y3", "n3")), "closed": False, "outcomePrices": '["0", "1"]'},
    })
    got = {r["market_id"]: r for r in client.market_resolutions(["1", "2", "3", "4"])}
    assert got["1"]["resolved"] and got["1"]["payoffs"] == {"yes1": 1, "no1": 0}
    assert not got["2"]["resolved"] and not got["3"]["resolved"]             # unsettled / still open
    assert not got["4"]["resolved"] and "HTTPStatusError" in got["4"]["error"]
    slow = client.market_resolutions(["1", "2"], deadline_seconds=-1)
    assert all("deadline" in r["error"] for r in slow)


# ================================================================ scorecard

def _outcomes(store, strategy, nets, mid=0.5):
    ids = store.record_signals([_sig(strategy=strategy, mid=mid, hours_ago=3, token=f"t{i}")
                                for i in range(len(nets))])
    store.record_outcomes([{"signal_id": i, "scored_at": NOW.isoformat(), "status": "SCORED",
                            "net": n, "mid_move": n + 0.02, "cost": 0.51, "return_on_cost": n / 0.51}
                           for i, n in zip(ids, nets)])


def test_verdicts_are_conservative(store):
    _outcomes(store, "MOMENTUM", [0.03 + (i % 3) * 0.01 for i in range(35)])
    _outcomes(store, "MEAN_REVERSION", [-0.03 - (i % 3) * 0.01 for i in range(35)])
    _outcomes(store, "BOOK_IMBALANCE", [(-1) ** i * 0.05 for i in range(35)])
    _outcomes(store, "LONGSHOT_BIAS", [0.01] * 5, mid=0.04)
    by = {s["strategy"]: s for s in build_scorecard(store)["strategies"]}
    assert by["MOMENTUM"]["verdict"].startswith("PROMISING") and "not validated" in by["MOMENTUM"]["verdict"]
    assert by["MEAN_REVERSION"]["verdict"] == "NEGATIVE_AFTER_COSTS"
    assert by["BOOK_IMBALANCE"]["verdict"] == "INCONCLUSIVE"
    assert by["LONGSHOT_BIAS"]["verdict"] == f"INSUFFICIENT_DATA ({MIN_SAMPLE - 5} more scored signals needed)"
    assert by["LONGSHOT_BIAS"]["by_band"]["<0.10"]["scored"] == 5 and by["MOMENTUM"]["by_band"]["0.10-0.90"]["scored"] == 35
    assert by["MOMENTUM"]["hit_rate"] == 1.0 and by["MOMENTUM"]["avg_net"] == 0.0397    # 12x3c + 12x4c + 11x5c
    md = render_markdown(build_scorecard(store), "2026-09-28")
    assert "| MOMENTUM | 6h | 35 | 35 | 100% | +3.97 |" in md and "Execution is locked" in md
    assert verdict({"scored": 0, "ci95_net": None}) == f"INSUFFICIENT_DATA ({MIN_SAMPLE} more scored signals needed)"


# ================================================================ pipeline + note

def test_pipeline_scores_resolves_hourly_and_notes_daily(tmp_path, store):
    vault = KnowledgeVault(tmp_path / "vault")
    vault.init()
    recorder = KnowledgeRecorder(vault)
    asked = []

    def resolver(ids):
        asked.append(list(ids))
        return [{"market_id": "1", "resolved": True, "payoffs": {"yes1": 1.0, "no1": 0.0}}]

    _record(store, NOW - timedelta(hours=5), (0.5, 0.5))
    store.record_signals([_sig(hours_ago=5)])
    _record(store, NOW - timedelta(hours=3.9), (0.53, 0.47))
    pipe = QuantPipeline(QuantEngine(store), LIBRARY, resolver=resolver, note_writer=recorder.write_scorecard)
    first = pipe.evaluate(now=NOW)
    assert first["resolutions"] == {"checked": 1, "resolved": 1, "errors": 0}
    assert first["scoring"]["by_status"] == {"SCORED": 1} and first["scorecard_note"] == f"[[Scorecard-{NOW:%Y-%m-%d}]]"
    [row] = store.outcomes()
    assert row["resolution_value"] == 1.0 and row["resolution_net"] == 0.49
    second = pipe.evaluate(now=NOW + timedelta(minutes=15))
    assert "resolutions" not in second and "scorecard_note" not in second and len(asked) == 1   # hourly / daily
    note = (vault.root / "FRIDAY" / "Strategies" / f"Scorecard-{NOW:%Y-%m-%d}.md").read_text()
    assert "strategy-scorecard" in note and "| BOOK_IMBALANCE | 1h | 1 | 1 |" in note


def test_old_vault_gains_strategies_folder(tmp_path, monkeypatch):
    import shutil
    from jarvis.knowledge.recorder import build_recorder_from_environment
    v = KnowledgeVault(tmp_path / "vault")
    v.init()
    shutil.rmtree(v.root / "FRIDAY" / "Strategies")                         # a 7B-era vault
    monkeypatch.setenv("JARVIS_VAULT_PATH", str(v.root))
    recorder, status = build_recorder_from_environment()
    assert recorder is not None and (v.root / "FRIDAY" / "Strategies").is_dir()


# ================================================================ context + app

def test_claude_sees_each_strategys_track_record(store):
    from jarvis.market.context import MarketContext
    _record(store, NOW, (0.5, 0.5))
    store.record_signals([{**_sig(hours_ago=0.1, horizon=6, strategy="MOMENTUM")}])
    block = MarketContext(store, engine=QuantEngine(store)).context_for("x", ["1"])
    [sig] = block["markets"][0]["quant"]["open_signals"]
    assert sig["strategy_track_record"]["verdict"].startswith("INSUFFICIENT_DATA")
    assert "measured result after costs" in block["notice"]


def test_app_scorecard_endpoints(tmp_path, monkeypatch):
    from jarvis.tests.test_market_data import FakePolymarket
    from jarvis.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    store.record_signals([_sig(hours_ago=2)])
    _record(store, NOW - timedelta(minutes=50), (0.56, 0.44))
    main = _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], {}), store)
    monkeypatch.setenv("JARVIS_KNOWLEDGE_ENABLED", "false")
    with TestClient(main.app) as c:
        assert c.post("/api/quant/score").json()["by_status"] == {"SCORED": 1}
        card = c.get("/api/quant/scorecard").json()
        by = {s["strategy"]: s for s in card["strategies"]}
        assert by["BOOK_IMBALANCE"]["scored"] == 1 and "nothing here authorises" in card["note"]
        assert c.get("/api/quant/outcomes").json()["outcomes"][0]["net"] == 0.04
        assert c.post("/api/quant/scorecard/note").status_code == 503              # notes disabled here
        assert c.get("/health").json()["execution_locked"] is True
