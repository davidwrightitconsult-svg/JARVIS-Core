"""
Indicators for the book strategies (Phase 11A). Pure functions of a list of
closing prices, oldest first. No I/O. Each returns None when there are not
enough bars, so a rule can never act on a half-formed number.

Polymarket history gives one price per bar: no open, high, low or volume.
Every indicator here is therefore defined on closes only. Results are rounded
to 9 decimals so that binary dust never decides a crossing.
"""

from __future__ import annotations

import math
from typing import Sequence


def sma(c: Sequence[float], n: int) -> float | None:
    return round(sum(c[-n:]) / n, 9) if n > 0 and len(c) >= n else None


def ema_series(c: Sequence[float], n: int) -> list[float]:
    """Exponential average seeded with the simple average of the first n bars."""
    if n <= 0 or len(c) < n:
        return []
    k, out = 2.0 / (n + 1), [sum(c[:n]) / n]
    for x in c[n:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def stdev(c: Sequence[float], n: int) -> float | None:
    """Population standard deviation of the last n closes (Bollinger's definition)."""
    if n <= 1 or len(c) < n:
        return None
    w = c[-n:]
    m = sum(w) / n
    return round(math.sqrt(sum((x - m) ** 2 for x in w) / n), 9)


def bollinger(c: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """(lower, middle, upper)."""
    m, s = sma(c, n), stdev(c, n)
    return None if m is None or s is None else (round(m - k * s, 9), m, round(m + k * s, 9))


def rsi(c: Sequence[float], n: int = 14) -> float | None:
    """Wilder's RSI: simple average of the first n changes, then Wilder smoothing."""
    if len(c) < n + 1:
        return None
    d = [c[i] - c[i - 1] for i in range(1, len(c))]
    gain = sum(max(x, 0.0) for x in d[:n]) / n
    loss = sum(max(-x, 0.0) for x in d[:n]) / n
    for x in d[n:]:
        gain = (gain * (n - 1) + max(x, 0.0)) / n
        loss = (loss * (n - 1) + max(-x, 0.0)) / n
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return round(100.0 - 100.0 / (1.0 + gain / loss), 7)


def macd(c: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float] | None:
    """(MACD line, signal line) at the last bar."""
    if len(c) < slow + signal:
        return None
    f, s = ema_series(c, fast), ema_series(c, slow)
    line = [a - b for a, b in zip(f[slow - fast:], s)]
    sig = ema_series(line, signal)
    return (round(line[-1], 9), round(sig[-1], 9)) if sig else None


def pivots(c: Sequence[float], span: int = 5) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(lows, highs) as (index, price). A pivot is the strict extreme of the `span` bars on each
    side, so it is only known `span` bars after it happened: the last `span` bars hold none."""
    lows, highs = [], []
    for i in range(span, len(c) - span):
        w = c[i - span:i + span + 1]
        if c[i] == min(w) and w.count(c[i]) == 1:
            lows.append((i, c[i]))
        elif c[i] == max(w) and w.count(c[i]) == 1:
            highs.append((i, c[i]))
    return lows, highs
