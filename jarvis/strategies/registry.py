"""
FRIDAY strategy registry (Phase 11A): the twelve strategies from the owner's
reference image, each written as an exact rule with its source and with every
setting fixed BEFORE any test is run.

What this is, and is not:
- A rule sees only the closes up to and including the current bar (it is
  handed that prefix and nothing else), so it cannot look ahead.
- The settings are the standard ones from the cited source, or one round
  number chosen here and written down. They are frozen: Phase 11C tests
  these values, once, and a strategy that fails is reported as failed. It is
  not re-tuned until it passes.
- The rule wording is FRIDAY's. The idea is the cited book's; where the book
  describes a chart with highs, lows or volume, the rule says how it was
  adapted to a venue that publishes one price per bar.
- Registered means written down, not profitable. Nothing here is approved
  for paper trading; only the validation gate can do that.

A bar is one hour. A price is the price of one outcome share, 0 to 1.
LONG buys the outcome. SHORT buys the opposite outcome.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

from . import indicators as ta

REGISTRY_VERSION = "11B.1"
BAR_HOURS = 1
LOOKBACK = 200          # a rule is handed at most the last 200 closes
MIN_PRICE, MAX_PRICE = 0.05, 0.95   # no entries in the tails, where a share is nearly settled
LONG, SHORT = "LONG", "SHORT"

# `match`: any one of these phrases, as whole words, in the document's title, author or file name.
# `exclude`: a document containing one of these is a different book.
BOOKS = {
    "murphy": {"author": "John J. Murphy", "title": "Technical Analysis of the Financial Markets",
               "match": ("murphy", "technical analysis of the financial markets")},
    "wilder": {"author": "J. Welles Wilder", "title": "New Concepts in Technical Trading Systems",
               "match": ("wilder", "new concepts in technical trading")},
    "brooks": {"author": "Al Brooks", "title": "Trading Price Action (Trends; Reversals)",
               "match": ("brooks", "trading price action")},
    "chan_qt": {"author": "Ernest P. Chan", "title": "Quantitative Trading", "match": ("quantitative trading",)},
    "tharp": {"author": "Van K. Tharp", "title": "Trade Your Way to Financial Freedom",
              "match": ("tharp", "trade your way to financial freedom")},
    "douglas": {"author": "Mark Douglas", "title": "Trading in the Zone", "match": ("trading in the zone",)},
    "chan": {"author": "Ernest P. Chan", "title": "Algorithmic Trading: Winning Strategies and Their Rationale",
             "match": ("algorithmic trading winning strategies", "winning strategies and their rationale"),
             "exclude": ("quantitative trading",)},
    "passarelli": {"author": "Dan Passarelli", "title": "Trading Options Greeks",
                   "match": ("passarelli", "option greeks", "options greeks")},
}


@dataclass(frozen=True)
class Exit:
    stop: float                 # price distance against the position (this is 1R)
    target: float | None        # price distance in favour; None = no fixed target
    max_hold: int               # bars; the position is closed at the latest after this many
    rule: str = ""              # an extra exit in words, implemented in `exit_fn`


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str                   # as written on the owner's reference image
    kind: str                   # SIGNAL | OVERLAY
    books: tuple[str, ...]
    source: str                 # where in the book the idea is taught
    rule: str                   # the exact rule, in words
    adaptation: str             # what changed because the venue has one price per bar
    params: dict[str, Any] = field(default_factory=dict)
    exit: Exit | None = None
    fn: Callable[[Sequence[float], dict[str, Any]], str | None] | None = None
    exit_fn: Callable[[Sequence[float], str, dict[str, Any]], bool] | None = None

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "books": [f"{BOOKS[b]['author']}, {BOOKS[b]['title']}" for b in self.books], "source": self.source,
                "rule": self.rule, "adaptation": self.adaptation, "params": dict(self.params),
                "exit": asdict(self.exit) if self.exit else None, "status": "REGISTERED_NOT_TESTED"}


def _d(a: float, b: float) -> float:
    """a - b without binary dust, so 0.505 - 0.500 is exactly half a cent on both sides of the book."""
    return round(a - b, 9)


def _crossed_up(prev: float, now: float, level: float) -> bool:
    return prev <= level < now


def _crossed_down(prev: float, now: float, level: float) -> bool:
    return prev >= level > now


# ---------------------------------------------------------------- the rules
# Each takes the closes up to now (oldest first) and returns LONG, SHORT or None.

def support_resistance(c, p):
    lows, highs = ta.pivots(c[-p["window"]:], p["pivot"])
    tol = p["tolerance"]

    def level(points, lowest):
        prices = sorted(x for _, x in points)
        if not lowest:
            prices.reverse()
        for x in prices:                                   # the outermost level touched at least twice
            near = [y for y in prices if abs(_d(y, x)) <= tol]
            if len(near) >= p["touches"]:
                return round(sum(near) / len(near), 9)
        return None
    if len(c) < 3:
        return None
    s, r = level(lows, True), level(highs, False)
    if s is not None and _d(c[-2], s) <= tol and c[-1] > c[-2] and c[-1] > s:
        return LONG
    if r is not None and _d(r, c[-2]) <= tol and c[-1] < c[-2] and c[-1] < r:
        return SHORT
    return None


def rsi_rule(c, p):
    now, prev = ta.rsi(c, p["period"]), ta.rsi(c[:-1], p["period"])
    if now is None or prev is None:
        return None
    if _crossed_up(prev, now, p["oversold"]):
        return LONG
    if _crossed_down(prev, now, p["overbought"]):
        return SHORT
    return None


def bollinger_rule(c, p):
    now, prev = ta.bollinger(c, p["period"], p["width"]), ta.bollinger(c[:-1], p["period"], p["width"])
    if now is None or prev is None:
        return None
    if c[-2] < prev[0] and c[-1] >= now[0]:               # closed below the lower band, now back inside
        return LONG
    if c[-2] > prev[2] and c[-1] <= now[2]:
        return SHORT
    return None


def bollinger_exit(c, side, p):
    b = ta.bollinger(c, p["period"], p["width"])
    return b is not None and (c[-1] >= b[1] if side == LONG else c[-1] <= b[1])


def macd_rule(c, p):
    now, prev = ta.macd(c, p["fast"], p["slow"], p["signal"]), ta.macd(c[:-1], p["fast"], p["slow"], p["signal"])
    if now is None or prev is None:
        return None
    a, b = prev[0] - prev[1], now[0] - now[1]
    return LONG if a <= 0 < b else SHORT if a >= 0 > b else None


def macd_exit(c, side, p):
    m = ta.macd(c, p["fast"], p["slow"], p["signal"])
    return m is not None and (m[0] < m[1] if side == LONG else m[0] > m[1])


def price_action(c, p):
    n = p["bars"]
    if len(c) < n + 2:
        return None
    moves = [_d(c[i], c[i - 1]) for i in range(len(c) - n, len(c))]
    before = _d(c[-n - 1], c[-n - 2])
    if all(m >= p["min_move"] for m in moves) and before < p["min_move"]:      # the run starts here, not earlier
        return LONG
    if all(m <= -p["min_move"] for m in moves) and before > -p["min_move"]:
        return SHORT
    return None


def trend_continuation(c, p):
    t, f = p["trend_bars"], p["flag_bars"]
    if len(c) < t + f + 1:
        return None
    flag = c[-f - 1:-1]
    hi, lo = max(flag), min(flag)
    run = _d(c[-f - 1], c[-f - 1 - t])
    if _d(hi, lo) > p["flag_range"]:
        return None
    if run >= p["trend_move"] and c[-1] > hi:
        return LONG
    if run <= -p["trend_move"] and c[-1] < lo:
        return SHORT
    return None


def breakout(c, p):
    n = p["channel"]
    if len(c) < n + 2:
        return None
    before = c[-n - 1:-1]
    if c[-1] > max(before) and c[-2] <= max(c[-n - 2:-2]):               # the first close outside the channel
        return LONG
    if c[-1] < min(before) and c[-2] >= min(c[-n - 2:-2]):
        return SHORT
    return None


def pullback(c, p):
    fast, slow, slow_then = ta.sma(c, p["fast"]), ta.sma(c, p["slow"]), ta.sma(c[:-p["slope_bars"]], p["slow"])
    fast_prev = ta.sma(c[:-1], p["fast"])
    if None in (fast, slow, slow_then, fast_prev):
        return None
    if fast > slow > slow_then and c[-2] <= fast_prev and c[-1] > fast:  # dipped to the average, closed back above
        return LONG
    if fast < slow < slow_then and c[-2] >= fast_prev and c[-1] < fast:
        return SHORT
    return None


def momentum(c, p):
    n = p["lookback"]
    if len(c) < n + 2:
        return None
    now, prev = _d(c[-1], c[-1 - n]), _d(c[-2], c[-2 - n])
    if _crossed_up(prev, now, p["threshold"]):
        return LONG
    if _crossed_down(prev, now, -p["threshold"]):
        return SHORT
    return None


def reversal(c, p):
    w = c[-p["window"]:]
    lows, highs = ta.pivots(w, p["pivot"])
    if len(w) < 3:
        return None
    if len(lows) >= 2:                                                   # double bottom
        (i1, a), (i2, b) = lows[-2], lows[-1]
        between = [x for i, x in highs if i1 < i < i2]
        if between and abs(_d(a, b)) <= p["tolerance"] and i2 - i1 >= p["separation"]:
            neck = max(between)
            if _d(neck, max(a, b)) >= p["depth"] and w[-2] <= neck < w[-1]:
                return LONG
    if len(highs) >= 2:                                                  # double top
        (i1, a), (i2, b) = highs[-2], highs[-1]
        between = [x for i, x in lows if i1 < i < i2]
        if between and abs(_d(a, b)) <= p["tolerance"] and i2 - i1 >= p["separation"]:
            neck = min(between)
            if _d(min(a, b), neck) >= p["depth"] and w[-2] >= neck > w[-1]:
                return SHORT
    return None


def moving_averages(c, p):
    f, s = ta.sma(c, p["fast"]), ta.sma(c, p["slow"])
    fp, sp = ta.sma(c[:-1], p["fast"]), ta.sma(c[:-1], p["slow"])
    if None in (f, s, fp, sp):
        return None
    return LONG if fp <= sp and f > s else SHORT if fp >= sp and f < s else None


def moving_averages_exit(c, side, p):
    f, s = ta.sma(c, p["fast"]), ta.sma(c, p["slow"])
    return None not in (f, s) and (f < s if side == LONG else f > s)


_MURPHY, _BROOKS = ("murphy",), ("brooks",)
_CLOSES = "Highs and lows are closing prices; the venue publishes no intrabar range."

STRATEGIES: tuple[Strategy, ...] = (
    Strategy("BOOK_SUPPORT_RESISTANCE", "Support & Resistance", "SIGNAL", _MURPHY,
             "Chapter 4, Basic Concepts of Trend: support and resistance",
             "Find swing lows and highs (a close that is the extreme of the 5 bars either side) in the last 120 bars. "
             "Support is the lowest price touched by at least 2 swing lows within 1 cent of each other; resistance "
             "is the mirror. LONG when the previous close was within 1 cent of support or below it and this close is "
             "higher and above support. SHORT is the mirror at resistance.", _CLOSES,
             {"window": 120, "pivot": 5, "touches": 2, "tolerance": 0.01}, Exit(0.03, 0.06, 48), support_resistance),
    Strategy("BOOK_RSI", "RSI", "SIGNAL", ("wilder", "murphy"),
             "Wilder, New Concepts in Technical Trading Systems: the Relative Strength Index, 14 periods, 70 and 30; "
             "Murphy chapter 10, Oscillators",
             "Wilder's 14-bar RSI. LONG when it crosses up through 30. SHORT when it crosses down through 70.",
             "None; the RSI is defined on closes.",
             {"period": 14, "oversold": 30.0, "overbought": 70.0}, Exit(0.03, 0.06, 48), rsi_rule),
    Strategy("BOOK_BOLLINGER", "Bollinger Bands", "SIGNAL", ("murphy", "chan_qt"),
             "Murphy chapter 9, Moving Averages: Bollinger Bands (20 periods, 2 standard deviations); "
             "Chan, Quantitative Trading: mean-reverting strategies",
             "20-bar average with bands 2 standard deviations either side. LONG when the previous close was below the "
             "lower band and this close is back inside. SHORT is the mirror at the upper band.",
             "None; the bands are defined on closes.",
             {"period": 20, "width": 2.0}, Exit(0.03, None, 48, "close when price reaches the 20-bar average"),
             bollinger_rule, bollinger_exit),
    Strategy("BOOK_MACD", "MACD", "SIGNAL", _MURPHY,
             "Chapter 10, Oscillators: Moving Average Convergence/Divergence (12, 26, 9)",
             "MACD line = 12-bar minus 26-bar exponential average; signal = its 9-bar exponential average. LONG when "
             "the MACD line crosses above the signal line. SHORT when it crosses below.",
             "None; MACD is defined on closes.",
             {"fast": 12, "slow": 26, "signal": 9}, Exit(0.03, None, 72, "close when the lines cross back"),
             macd_rule, macd_exit),
    Strategy("BOOK_PRICE_ACTION", "Price Action", "SIGNAL", _BROOKS,
             "Brooks, Trading Price Action Trends: consecutive strong trend bars as the sign of a trend starting",
             "LONG at the third consecutive bar that closes at least half a cent above the bar before it, when the "
             "bar before the run did not. SHORT is the mirror.",
             "Brooks reads the open, high, low and close of each bar. With one price per bar, a trend bar is "
             "reduced to the change between consecutive closes. This is a much thinner rule than the book's.",
             {"bars": 3, "min_move": 0.005}, Exit(0.03, 0.06, 24), price_action),
    Strategy("BOOK_TREND_CONTINUATION", "Trend Continuation", "SIGNAL", _MURPHY,
             "Chapter 6, Continuation Patterns: flags and pennants",
             "A move of at least 5 cents over 24 bars, then 8 bars that stay within a 2 cent range. LONG when the "
             "prior move was up and this close is above the range. SHORT when it was down and this close is below.",
             _CLOSES + " The book also asks for falling volume in the flag; there is no volume per bar.",
             {"trend_bars": 24, "trend_move": 0.05, "flag_bars": 8, "flag_range": 0.02}, Exit(0.03, 0.06, 48),
             trend_continuation),
    Strategy("BOOK_BREAKOUT", "Breakout", "SIGNAL", ("murphy", "brooks"),
             "Murphy chapter 4: the breaking of support and resistance; Brooks: breakouts from trading ranges",
             "LONG on the first close above the highest close of the previous 48 bars. SHORT on the first close "
             "below the lowest.", _CLOSES,
             {"channel": 48}, Exit(0.03, 0.06, 48), breakout),
    Strategy("BOOK_PULLBACK", "Pullback", "SIGNAL", ("brooks", "murphy"),
             "Brooks: pullbacks to the moving average in a trend; Murphy chapter 9: averages as support in an uptrend",
             "Uptrend = 20-bar average above the 50-bar average and the 50-bar average higher than 10 bars ago. "
             "LONG when the previous close was at or below the 20-bar average and this close is above it. SHORT is "
             "the mirror in a downtrend.",
             "Brooks uses a 20-bar exponential average and bar shapes; this uses simple averages of closes.",
             {"fast": 20, "slow": 50, "slope_bars": 10}, Exit(0.03, 0.06, 48), pullback),
    Strategy("BOOK_MOMENTUM", "Momentum", "SIGNAL", ("chan_qt",),
             "Chan, Quantitative Trading: mean-reverting versus momentum strategies",
             "24-bar change in price. LONG when it crosses above +3 cents. SHORT when it crosses below -3 cents. "
             "Hold 24 bars.",
             "Chan works with returns on stocks and futures over days to months; this is the same idea on a share price "
             "bounded between 0 and 1, over hours.",
             {"lookback": 24, "threshold": 0.03}, Exit(0.03, None, 24), momentum),
    Strategy("BOOK_REVERSAL", "Reversal", "SIGNAL", _MURPHY,
             "Chapter 5, Major Reversal Patterns: double tops and bottoms",
             "Double bottom: two swing lows within 1 cent of each other, at least 10 bars apart, with a swing high "
             "at least 3 cents above them in between. LONG when a close crosses above that swing high. Double top "
             "is the mirror, SHORT.",
             _CLOSES + " The book also asks for volume to confirm; there is none.",
             {"window": 120, "pivot": 5, "tolerance": 0.01, "separation": 10, "depth": 0.03}, Exit(0.03, 0.06, 72),
             reversal),
    Strategy("BOOK_MOVING_AVERAGES", "Moving Averages", "SIGNAL", _MURPHY,
             "Chapter 9, Moving Averages: the double crossover method",
             "LONG when the 10-bar average crosses above the 30-bar average. SHORT when it crosses below.",
             "None; the averages are of closes. Murphy's common pairs are in days (5 and 20, 10 and 50); 10 and 30 "
             "bars is the choice fixed here.",
             {"fast": 10, "slow": 30}, Exit(0.03, None, 72, "close when the averages cross back"),
             moving_averages, moving_averages_exit),
    Strategy("BOOK_RISK_MANAGEMENT", "Risk Management", "OVERLAY", ("tharp", "douglas"),
             "Tharp: R-multiples, expectancy and position sizing; Douglas: define the risk before entering and "
             "accept it, think in probabilities",
             "Applies to every strategy above. (1) The stop distance is fixed before entry and is 1R. (2) Every "
             "result is recorded in R. (3) A strategy is judged on its expectancy in R after costs over many trades, "
             "never on single trades. (4) Size comes from the Risk Governor, never from the strategy.",
             "This is a discipline, not a signal. It is enforced by the exits in this registry and by the Phase 10 "
             "Risk Governor; it is not tested as a separate strategy.", {}, None, None),
)

REGISTRY: dict[str, Strategy] = {s.key: s for s in STRATEGIES}
NO_RULE = {
    "passarelli": "Options strategies need listed options with strikes and expiries. Polymarket has none, so no "
                  "rule from this book can be tested on this venue.",
    "douglas": "A book on discipline, not signals. Its contribution is the Risk Management overlay.",
    "chan": "Named on the reference image. FRIDAY's library holds the same author's Quantitative Trading instead, "
            "and the Bollinger Bands and Momentum rules cite that book.",
}

# What the source check looks for: for each rule and each of its books, words that must all appear
# together in at least one passage of FRIDAY's own copy. Finding them shows the book teaches the idea;
# it does not show the book endorses FRIDAY's exact numbers.
SOURCE_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "BOOK_SUPPORT_RESISTANCE": {"murphy": ("support resistance",)},
    "BOOK_RSI": {"wilder": ("relative strength index",), "murphy": ("relative strength index", "RSI 70 30")},
    "BOOK_BOLLINGER": {"murphy": ("bollinger bands", "standard deviations bands"), "chan_qt": ("bollinger", "mean reverting")},
    "BOOK_MACD": {"murphy": ("MACD signal line",)},
    "BOOK_PRICE_ACTION": {"brooks": ("trend bars", "consecutive trend bars")},
    "BOOK_TREND_CONTINUATION": {"murphy": ("flags pennants", "continuation patterns")},
    "BOOK_BREAKOUT": {"murphy": ("breakout resistance",), "brooks": ("breakout trading range",)},
    "BOOK_PULLBACK": {"brooks": ("pullback moving average",), "murphy": ("moving average support",)},
    "BOOK_MOMENTUM": {"chan_qt": ("momentum strategies", "mean reverting momentum")},
    "BOOK_REVERSAL": {"murphy": ("double top", "double bottom")},
    "BOOK_MOVING_AVERAGES": {"murphy": ("double crossover", "moving averages crossover")},
    "BOOK_RISK_MANAGEMENT": {"tharp": ("R multiple", "expectancy", "position sizing"), "douglas": ("probabilities", "risk")},
}


def signal(strategy: Strategy, closes: Sequence[float]) -> str | None:
    """The strategy's decision at the LAST bar of `closes`. Uses at most the last LOOKBACK closes."""
    if strategy.fn is None or len(closes) < 3:
        return None
    c = closes[-LOOKBACK:]
    if not all(isinstance(x, (int, float)) and 0.0 <= x <= 1.0 for x in c):
        return None
    if not MIN_PRICE <= c[-1] <= MAX_PRICE:
        return None
    return strategy.fn(c, strategy.params)


def should_exit(strategy: Strategy, closes: Sequence[float], side: str) -> bool:
    """The strategy's own exit rule at the last bar (stops, targets and time are the engine's job)."""
    return bool(strategy.exit_fn and strategy.exit_fn(closes[-LOOKBACK:], side, strategy.params))


def signals(strategy: Strategy, closes: Sequence[float]) -> list[tuple[int, str]]:
    """Every (bar index, side) the rule fires on, each decided from the closes up to that bar only."""
    out = []
    for i in range(len(closes)):
        side = signal(strategy, closes[:i + 1])
        if side:
            out.append((i, side))
    return out


def _matches(book: dict[str, Any], document: dict[str, Any]) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", " ".join(str(document.get(k) or "") for k in ("title", "author", "source", "original")).lower())
    if any(re.search(rf"\b{re.escape(x)}\b", text) for x in book.get("exclude", ())):
        return False
    return any(re.search(rf"\b{re.escape(m)}\b", text) for m in book["match"])


def books_in_library(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which of the cited books FRIDAY's library actually holds (matched on title, author and file name)."""
    rows = []
    for key, b in BOOKS.items():
        hits = [d for d in documents if d.get("kind", "book") == "book" and _matches(b, d)]
        rows.append({"key": key, "book": f"{b['author']}, {b['title']}", "in_library": bool(hits),
                     "library_title": hits[0].get("title") if hits else None,
                     "doc_ids": [d["doc_id"] for d in hits if d.get("doc_id")],
                     "strategies": [s.name for s in STRATEGIES if key in s.books],
                     "note": NO_RULE.get(key, "")})
    return rows


def check_sources(library) -> dict[str, Any]:
    """For each rule, is its idea found in FRIDAY's own copy of each cited book? Reports counts and
    locations only - never the book's text. `library` is a ResearchLibrary (or None)."""
    docs = library.documents() if library is not None else []
    held = {r["key"]: r for r in books_in_library(docs)}
    rows = []
    for s in STRATEGIES:
        checks = []
        for book in s.books:
            entry = {"book": held[book]["book"], "in_library": held[book]["in_library"], "found": [], "missing": []}
            if entry["in_library"]:
                for terms in SOURCE_TERMS.get(s.key, {}).get(book, ()):
                    hits = [h for doc_id in held[book]["doc_ids"] for h in library.locate(doc_id, terms)]
                    if hits:
                        where = []
                        for h in hits:
                            loc = str(h.get("location") or f"passage {h['seq']}")
                            if loc not in where:
                                where.append(loc)
                        entry["found"].append({"terms": terms, "passages": len(hits), "where": where[:4]})
                    else:
                        entry["missing"].append(terms)
            checks.append(entry)
        present = [c for c in checks if c["in_library"]]
        status = ("NOT_VERIFIED" if not present else
                  "FOUND_IN_SOURCE" if all(c["found"] for c in present) and len(present) == len(checks) else
                  "PARTLY_FOUND" if any(c["found"] for c in present) else "NOT_FOUND_IN_SOURCE")
        rows.append({"key": s.key, "name": s.name, "status": status, "books": checks})
    return {"registry_version": REGISTRY_VERSION, "library_documents": len(docs), "strategies": rows,
            "note": "FOUND means the cited book, in FRIDAY's own copy, has passages on the idea. The exact "
                    "numbers in each rule (stop, target, bar counts) are FRIDAY's and are stated as such."}


def describe() -> dict[str, Any]:
    return {"registry_version": REGISTRY_VERSION, "bar_hours": BAR_HOURS, "lookback_bars": LOOKBACK,
            "price_band": [MIN_PRICE, MAX_PRICE], "registered": len(STRATEGIES),
            "signals": sum(1 for s in STRATEGIES if s.kind == "SIGNAL"),
            "strategies": [s.public() for s in STRATEGIES],
            "approved_for_paper": [], "note": "Registered means written down with fixed settings. None of these "
            "has been tested yet; Phase 11B builds the price history and 11C runs each through the validation gate."}
