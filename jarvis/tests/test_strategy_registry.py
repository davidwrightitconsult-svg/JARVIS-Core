"""Phase 11A: the registry of book strategies - exact rules, fixed settings, no lookahead."""

import hashlib
import json
import random

import pytest
from fastapi.testclient import TestClient

from jarvis.strategies import LONG, LOOKBACK, REGISTRY, SHORT, STRATEGIES, books_in_library, describe, should_exit, signal, signals
from jarvis.strategies import indicators as ta
from jarvis.strategies.__main__ import main as cli

IMAGE = ["Support & Resistance", "RSI", "Bollinger Bands", "MACD", "Price Action", "Trend Continuation", "Breakout",
         "Pullback", "Momentum", "Reversal", "Moving Averages", "Risk Management"]
# Changing any rule setting or exit changes this. That must be a deliberate, documented decision made
# BEFORE a test run - never a response to a failed backtest.
FROZEN = "0127a7025bb1550754debf0bcc9fdcc9d58225b76bddd1aaf9ebaec31948d0e2"
S = REGISTRY


def walk(seed, n=700, vol=0.008):
    rng, p, out = random.Random(seed), 0.5, []
    for i in range(n):
        p = min(0.97, max(0.03, p + rng.gauss(0, vol) + (0.0005 if (i // 150) % 2 else -0.0005)))
        out.append(round(p, 3))
    return out


def test_the_twelve_from_the_image_are_registered_and_nothing_is_approved():
    assert [s.name for s in STRATEGIES] == IMAGE
    d = describe()
    assert d["registered"] == 12 and d["signals"] == 11 and d["approved_for_paper"] == []
    for s in d["strategies"]:
        assert s["status"] == "REGISTERED_NOT_TESTED" and s["source"] and s["rule"] and s["adaptation"] and s["books"]
    overlay = S["BOOK_RISK_MANAGEMENT"]
    assert overlay.kind == "OVERLAY" and overlay.fn is None and signal(overlay, walk(1)) is None


def test_settings_are_frozen():
    blob = json.dumps([(s.key, s.params, s.exit and s.exit.__dict__) for s in STRATEGIES], sort_keys=True)
    assert hashlib.sha256(blob.encode()).hexdigest() == FROZEN
    for s in STRATEGIES:
        if s.kind == "SIGNAL":
            assert s.exit.stop > 0 and s.exit.max_hold > 0                      # 1R and a time limit, always
            assert s.exit.target is not None or s.exit.rule == "" or s.exit_fn is not None
            assert bool(s.exit.rule) == (s.exit_fn is not None)                 # an exit in words has code behind it


@pytest.mark.parametrize("key", [s.key for s in STRATEGIES if s.kind == "SIGNAL"])
def test_no_rule_can_see_the_future(key):
    """The decision at bar i is identical whether or not later bars exist, and whatever they are."""
    s, c = S[key], walk(7)
    fired = dict(signals(s, c))
    assert fired, "the rule never fired on a random walk"
    assert set(fired.values()) <= {LONG, SHORT}
    rng = random.Random(3)
    for i in rng.sample(range(60, len(c)), 60):
        assert signal(s, c[:i + 1]) == fired.get(i)
    twisted = c[:400] + [round(1 - x, 3) for x in c[400:]]                      # a different future
    assert {i: v for i, v in signals(s, twisted) if i < 400} == {i: v for i, v in fired.items() if i < 400}
    assert signal(s, c) == signal(s, c[-LOOKBACK:])                             # only the last 200 bars count


@pytest.mark.parametrize("key", [s.key for s in STRATEGIES if s.kind == "SIGNAL"])
def test_rules_refuse_bad_or_extreme_prices(key):
    s = S[key]
    c = walk(11)
    i, _ = signals(s, c)[0]
    good = c[:i + 1]
    assert signal(s, good)
    assert signal(s, good[:-1] + [float("nan")]) is None and signal(s, good[:-1] + [1.2]) is None
    assert signal(s, good[:2]) is None and signal(s, []) is None
    assert signal(s, [0.5] * 300) is None                                       # a flat market gives no signal
    assert signal(s, [x * 0.05 for x in good]) is None                          # below 5 cents: no entries
    assert signal(s, [1 - x * 0.05 for x in good]) is None                      # above 95 cents: no entries


def test_long_and_short_are_mirror_images():
    c = walk(5)
    mirror = [round(1 - x, 3) for x in c]
    flip = {LONG: SHORT, SHORT: LONG}
    for s in STRATEGIES:
        if s.kind == "SIGNAL":
            assert [(i, flip[v]) for i, v in signals(s, c)] == signals(s, mirror), s.key


def test_indicators():
    c = [float(x) for x in range(1, 31)]
    assert ta.sma(c, 5) == 28.0 and ta.sma(c[:3], 5) is None
    assert ta.rsi(c, 14) == 100.0 and ta.rsi(c[::-1], 14) == 0.0 and ta.rsi([5.0] * 20, 14) == 50.0
    assert ta.rsi(c[:14], 14) is None
    # Wilder's own worked example (New Concepts in Technical Trading Systems): first RSI 70.46..70.53
    wilder = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert 70.4 < ta.rsi(wilder, 14) < 70.6
    lo, mid, hi = ta.bollinger([1.0, 3.0] * 10, 20, 2.0)
    assert mid == 2.0 and abs(hi - 4.0) < 1e-9 and abs(lo - 0.0) < 1e-9          # sd of 1,3,... is 1
    assert ta.ema_series([2.0] * 10, 5) == [2.0] * 6 and ta.ema_series([1.0], 5) == []
    up = ta.macd([0.2 + 0.01 * i for i in range(60)])
    assert up[0] > 0 and ta.macd([0.5] * 60) == (0.0, 0.0) and ta.macd([0.5] * 30) is None
    lows, highs = ta.pivots([5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1, 0.5], 5)
    assert lows == [(5, 0)] and highs == [(11, 6)]                              # the last 5 bars hold no pivot yet


def test_each_rule_fires_on_the_pattern_it_names():
    flat = [0.50] * 60
    assert signal(S["BOOK_BREAKOUT"], flat + [0.52]) == LONG and signal(S["BOOK_BREAKOUT"], flat + [0.52, 0.53]) is None
    assert signal(S["BOOK_BREAKOUT"], flat + [0.48]) == SHORT
    assert signal(S["BOOK_PRICE_ACTION"], flat + [0.506, 0.512, 0.518]) == LONG
    assert signal(S["BOOK_PRICE_ACTION"], flat + [0.506, 0.512, 0.518, 0.524]) is None      # only where the run starts
    assert signal(S["BOOK_PRICE_ACTION"], flat + [0.503, 0.506, 0.509]) is None             # moves too small
    drop = flat + [0.50 - 0.004 * i for i in range(1, 20)]
    assert signal(S["BOOK_RSI"], drop) is None and signal(S["BOOK_RSI"], drop + [0.47]) == LONG
    assert signal(S["BOOK_BOLLINGER"], flat + [0.45]) is None and signal(S["BOOK_BOLLINGER"], flat + [0.45, 0.50]) == LONG
    assert should_exit(S["BOOK_BOLLINGER"], flat + [0.45, 0.50, 0.50], LONG)
    assert not should_exit(S["BOOK_BOLLINGER"], flat + [0.45, 0.46], LONG)
    rise = [0.30 + 0.004 * i for i in range(60)]
    turn = rise + [rise[-1] - 0.01 * i for i in range(1, 12)]
    fired = dict(signals(S["BOOK_MOVING_AVERAGES"], turn))
    assert SHORT in fired.values() and should_exit(S["BOOK_MOVING_AVERAGES"], turn, LONG)
    assert SHORT in dict(signals(S["BOOK_MACD"], turn)).values() and should_exit(S["BOOK_MACD"], turn, LONG)
    assert not should_exit(S["BOOK_RSI"], turn, LONG)                                       # no rule of its own
    trend = [0.40 + 0.0025 * i for i in range(25)] + [0.46, 0.462, 0.458, 0.46, 0.461, 0.459, 0.46, 0.461]
    assert signal(S["BOOK_TREND_CONTINUATION"], [0.40] * 5 + trend + [0.47]) == LONG
    assert signal(S["BOOK_TREND_CONTINUATION"], [0.40] * 5 + trend + [0.461]) is None
    mom = [0.50] * 30 + [0.51, 0.52, 0.535]
    assert signal(S["BOOK_MOMENTUM"], mom) == LONG and signal(S["BOOK_MOMENTUM"], mom + [0.54]) is None
    w = [0.60, 0.58, 0.56, 0.54, 0.52, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.585, 0.57, 0.555, 0.54, 0.525, 0.505,
         0.52, 0.535, 0.55, 0.565, 0.58, 0.59]
    assert signal(S["BOOK_REVERSAL"], w) is None and signal(S["BOOK_REVERSAL"], w + [0.61]) == LONG   # double bottom
    assert signal(S["BOOK_SUPPORT_RESISTANCE"], w + [0.57, 0.55, 0.53, 0.51, 0.52]) == LONG   # bounce off 0.50
    pull = [0.30 + 0.003 * i for i in range(70)]
    pull += [pull[-1] - 0.012, pull[-1] - 0.03]
    assert signal(S["BOOK_PULLBACK"], pull) is None and signal(S["BOOK_PULLBACK"], pull + [pull[-3] + 0.01]) == LONG


def test_books_are_matched_on_whole_words():
    docs = [{"title": "Technical Analysis of the Financial Markets", "author": "John J. Murphy"},
            {"title": "Exchange mechanics and the chance of options", "author": "Someone Else"},
            {"title": "Algorithmic Trading: Winning Strategies", "author": "Ernest Chan"}]
    rows = {r["book"].split(",")[0]: r for r in books_in_library(docs)}
    assert rows["John J. Murphy"]["in_library"] and rows["Ernest P. Chan"]["in_library"]
    assert not rows["Dan Passarelli"]["in_library"] and not rows["Al Brooks"]["in_library"]
    assert "no rule" in rows["Dan Passarelli"]["note"] and rows["Dan Passarelli"]["strategies"] == []
    assert len(rows["John J. Murphy"]["strategies"]) == 9
    assert all(not r["in_library"] for r in books_in_library([]))


def test_api_and_cli(capsys):
    from jarvis.app import main
    with TestClient(main.app) as c:
        d = c.get("/api/strategies").json()
        assert d["registered"] == 12 and len(d["books"]) == 8 and d["approved_for_paper"] == []
        assert d["live_trading"] == "LOCKED" and d["library_available"] is False
        assert c.post("/api/strategies", json={}).status_code == 405
        b = c.get("/api/dashboard").json()["book_strategies"]
        assert b["registered"] == 12 and b["tested"] == 0 and b["items"][0]["source"].startswith("Chapter 4")
        gate = c.get("/api/lab/gate").json()                                    # registration approves nothing
        assert not any(k.startswith("BOOK_") for k in gate["approved_for_paper"] + gate["forward_test"])
        r = c.post("/api/risk/evaluate", json={"strategy": "BOOK_RSI", "market_id": "m", "stake": 10}).json()
        assert r["reason"] == "NOT_ALLOWED_BY_GATE"
    assert cli(["list"]) == 0
    out = capsys.readouterr().out
    assert "12 registered" in out and "0 approved" in out and "Chapter 10" in out
    assert cli(["books"]) == 0 and "NOT IN LIBRARY" in capsys.readouterr().out
