"""
FRIDAY market data models + data-quality checks (Phase 6A).

Everything here is pure, deterministic parsing — no network.

Order-book correctness (the Phase 0 finding): Polymarket's CLOB /book
returns levels WORST-first (best bid / best ask at the END of each list) and
the order is not guaranteed. FRIDAY therefore never trusts list position:
best bid = highest bid price, best ask = lowest ask price, and levels are
re-sorted best-first before any depth calculation.

Prices are probabilities in [0, 1]; sizes are shares; depth is reported in
USD (price x size) within a band around the mid.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

MARKET_DATA_VERSION = "6C.1"
DEPTH_BAND = 0.02            # USD depth counted within +/- 2 cents of mid
WIDE_SPREAD = 0.10
STALE_SECONDS = 300
GAMMA_TOLERANCE = 0.05
BINARY_SUM_TOLERANCE = 0.05
NEAR_CERTAIN = 0.98          # 6C: best bid >= this (or best ask <= 1 - this) = effectively decided
PAST_END_HOURS = 24          # 6C: end date passed by more than this = awaiting resolution

# Flags that make a price unusable for any downstream decision.
HARD_FLAGS = frozenset({"EMPTY_BOOK", "ONE_SIDED", "CROSSED", "PLACEHOLDER", "NO_BOOK"})


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _bool(value: Any, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return default


# ------------------------------------------------------------------ markets

@dataclass
class Outcome:
    name: str
    token_id: str
    gamma_price: float | None = None


@dataclass
class Market:
    market_id: str
    question: str
    condition_id: str | None = None
    slug: str | None = None
    end_date: str | None = None
    active: bool | None = None
    closed: bool | None = None
    accepting_orders: bool | None = None
    enable_order_book: bool | None = None
    neg_risk: bool | None = None
    volume_24h: float | None = None
    volume: float | None = None
    liquidity: float | None = None          # Gamma's own USD liquidity figure
    best_bid: float | None = None           # Gamma summary (first outcome), for cross-checks
    best_ask: float | None = None
    last_trade_price: float | None = None
    outcomes: list[Outcome] = field(default_factory=list)
    sports_hint: bool = False               # 8C.2: Gamma marks it as a sports market

    @property
    def segment(self) -> str:
        from .segments import segment_of
        return segment_of(self.question, self.neg_risk, self.sports_hint)

    @property
    def tradable_book(self) -> bool:
        return (self.enable_order_book is not False and self.closed is not True
                and self.accepting_orders is not False and len(self.outcomes) >= 2)


def _sports_hint(raw: dict[str, Any]) -> bool:
    from .segments import sports_hint_of
    return sports_hint_of(raw)


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Gamma /markets item -> Market, tolerant of string-encoded lists and
    missing fields. Returns None if it has no id/question or no tokens."""
    if not isinstance(raw, dict):
        return None
    market_id = str(raw.get("id") or raw.get("market_id") or "").strip()
    question = " ".join(str(raw.get("question") or raw.get("title") or "").split())
    tokens = _json_list(raw.get("clobTokenIds")) or _json_list(raw.get("tokens"))
    names = _json_list(raw.get("outcomes"))
    prices = _json_list(raw.get("outcomePrices"))
    outcomes = []
    for i, token in enumerate(tokens):
        token_id = str(token.get("token_id") if isinstance(token, dict) else token or "").strip()
        if not token_id:
            continue
        name = (token.get("outcome") if isinstance(token, dict) else None) or \
            (names[i] if i < len(names) else f"Outcome {i + 1}")
        outcomes.append(Outcome(str(name)[:80], token_id, _num(prices[i]) if i < len(prices) else None))
    if not market_id or not question or not outcomes:
        return None
    return Market(
        market_id=market_id, question=question[:300],
        condition_id=raw.get("conditionId") or raw.get("condition_id"),
        slug=raw.get("slug"), end_date=raw.get("endDate") or raw.get("end_date_iso"),
        active=_bool(raw.get("active")), closed=_bool(raw.get("closed")),
        accepting_orders=_bool(raw.get("acceptingOrders")),
        enable_order_book=_bool(raw.get("enableOrderBook")),
        neg_risk=_bool(raw.get("negRisk")),
        volume_24h=_num(raw.get("volume24hr")), volume=_num(raw.get("volumeNum") or raw.get("volume")),
        liquidity=_num(raw.get("liquidityNum") or raw.get("liquidity")),
        best_bid=_num(raw.get("bestBid")), best_ask=_num(raw.get("bestAsk")),
        last_trade_price=_num(raw.get("lastTradePrice")),
        outcomes=outcomes, sports_hint=_sports_hint(raw),
    )


# ------------------------------------------------------------------ books

@dataclass
class Book:
    token_id: str
    bids: list[tuple[float, float]]          # (price, size), BEST first
    asks: list[tuple[float, float]]          # (price, size), BEST first
    timestamp: datetime | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    last_trade_price: float | None = None
    raw_first_bid: float | None = None       # what bids[0] said — for the ordering probe
    raw_first_ask: float | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round((self.best_bid + self.best_ask) / 2, 6)

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    def depth_usd(self, band: float = DEPTH_BAND) -> float:
        """USD (price x size) resting within +/- band of the mid, both sides."""
        mid = self.mid
        if mid is None:
            return 0.0
        total = sum(p * s for p, s in self.bids if p >= mid - band - 1e-9)
        total += sum(p * s for p, s in self.asks if p <= mid + band + 1e-9)
        return round(total, 2)


def _levels(raw_levels: Any) -> list[tuple[float, float]]:
    out = []
    for level in raw_levels if isinstance(raw_levels, list) else []:
        if isinstance(level, dict):
            price, size = _num(level.get("price")), _num(level.get("size"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = _num(level[0]), _num(level[1])
        else:
            continue
        if price is None or size is None or size <= 0 or not 0 <= price <= 1:
            continue
        out.append((price, size))
    return out


def _timestamp(value: Any) -> datetime | None:
    number = _num(value)
    if number is not None:
        seconds = number / 1000 if number > 1e11 else number      # ms or s
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def parse_time(value: Any) -> datetime | None:
    """ISO date/datetime (Gamma endDate) -> aware UTC datetime, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_book(raw: dict[str, Any], token_id: str | None = None) -> Book:
    bids, asks = _levels(raw.get("bids")), _levels(raw.get("asks"))
    raw_first_bid = bids[0][0] if bids else None
    raw_first_ask = asks[0][0] if asks else None
    bids.sort(key=lambda lv: -lv[0])          # best (highest) bid first
    asks.sort(key=lambda lv: lv[0])           # best (lowest) ask first
    return Book(
        token_id=str(token_id or raw.get("asset_id") or raw.get("token_id") or ""),
        bids=bids, asks=asks, timestamp=_timestamp(raw.get("timestamp")),
        tick_size=_num(raw.get("tick_size")), min_order_size=_num(raw.get("min_order_size")),
        last_trade_price=_num(raw.get("last_trade_price")),
        raw_first_bid=raw_first_bid, raw_first_ask=raw_first_ask,
    )


# ------------------------------------------------------------------ snapshot rows

def quality_flags(book: Book | None, market: Market | None = None, *, outcome_index: int = 0,
                  now: datetime | None = None) -> list[str]:
    if book is None:
        return ["NO_BOOK"]
    flags = []
    if not book.bids and not book.asks:
        return ["EMPTY_BOOK"]
    if not book.bids or not book.asks:
        flags.append("ONE_SIDED")
    else:
        if book.best_bid >= book.best_ask:
            flags.append("CROSSED")
        if book.best_bid <= 0.01 and book.best_ask >= 0.99:
            flags.append("PLACEHOLDER")
        elif book.spread is not None and book.spread > WIDE_SPREAD:
            flags.append("WIDE_SPREAD")
    now = now or datetime.now(timezone.utc)
    if book.timestamp and (now - book.timestamp).total_seconds() > STALE_SECONDS:
        flags.append("STALE")
    if market is not None and outcome_index == 0 and book.mid is not None \
            and market.best_bid is not None and market.best_ask is not None:
        gamma_mid = (market.best_bid + market.best_ask) / 2
        if abs(book.mid - gamma_mid) > GAMMA_TOLERANCE:
            flags.append("GAMMA_MISMATCH")
    end = parse_time(market.end_date) if market is not None else None
    if end is not None and end < now:
        flags.append("PAST_END")                 # soft: trading past the scheduled end
    return flags


@dataclass
class SnapshotRow:
    market_id: str
    question: str
    outcome: str
    token_id: str
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    last_trade_price: float | None
    depth_usd: float
    gamma_liquidity: float | None
    volume_24h: float | None
    end_date: str | None
    flags: list[str]
    usable: bool
    observed_at: str
    # Top-of-book levels for the market store only (6B); never part of
    # as_dict(), so Hermes job results stay small.
    top_bids: list[tuple[float, float]] = field(default_factory=list)
    top_asks: list[tuple[float, float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("top_bids", None)
        row.pop("top_asks", None)
        # Phase 0/3/4 consumers (scan, recorder snapshot table) read these keys:
        title = self.question if self.outcome in ("", None) else f"{self.question} — {self.outcome}"
        row.update({
            "title": title,
            "price": self.mid if self.mid is not None else self.last_trade_price,
            "bid": self.best_bid, "ask": self.best_ask,
            "liquidity": self.depth_usd,
        })
        return row


def snapshot_rows(market: Market, books: dict[str, Book | None],
                  now: datetime | None = None, levels: int = 5) -> list[SnapshotRow]:
    now = now or datetime.now(timezone.utc)
    rows = []
    for i, outcome in enumerate(market.outcomes):
        book = books.get(outcome.token_id)
        flags = quality_flags(book, market, outcome_index=i, now=now)
        rows.append(SnapshotRow(
            market_id=market.market_id, question=market.question, outcome=outcome.name,
            token_id=outcome.token_id,
            best_bid=book.best_bid if book else None, best_ask=book.best_ask if book else None,
            mid=book.mid if book else None, spread=book.spread if book else None,
            last_trade_price=(book.last_trade_price if book and book.last_trade_price is not None
                              else market.last_trade_price if i == 0 else None),
            depth_usd=book.depth_usd() if book else 0.0,
            gamma_liquidity=market.liquidity, volume_24h=market.volume_24h, end_date=market.end_date,
            flags=flags, usable=not (set(flags) & HARD_FLAGS),
            observed_at=now.isoformat(timespec="seconds"),
            top_bids=list(book.bids[:levels]) if book else [],
            top_asks=list(book.asks[:levels]) if book else [],
        ))
    # Binary market: YES mid + NO mid should be ~1.
    if len(rows) == 2 and all(r.mid is not None for r in rows):
        total = rows[0].mid + rows[1].mid
        if abs(total - 1) > BINARY_SUM_TOLERANCE:
            for row in rows:
                row.flags.append("PRICE_SUM")
    return rows


# ------------------------------------------------------------------ settlement (6C)

def settlement_reason(market: Market, rows: list[SnapshotRow], now: datetime | None = None,
                      near_certain: float = NEAR_CERTAIN,
                      past_end_hours: float = PAST_END_HOURS) -> str | None:
    """
    Why a market is no longer worth observing for decisions, or None.

    NEAR_CERTAIN: some outcome's best bid >= near_certain, or best ask
      <= 1 - near_certain — the outcome is effectively decided (typically a
      finished game or called event still open while it awaits resolution;
      the Phase 6B "Chiefs 0.999 bid, no asks" case). No edge is left and
      the remaining risk is resolution/dispute risk.
    PAST_END: the scheduled end date passed more than past_end_hours ago.

    Placeholder books (0.01 / 0.99) are NOT settled: they are empty
    markets, already flagged unusable by quality_flags.
    """
    now = now or datetime.now(timezone.utc)
    for row in rows:
        if row.best_bid is not None and row.best_bid >= near_certain:
            return "NEAR_CERTAIN"
        if row.best_ask is not None and row.best_ask <= 1 - near_certain:
            return "NEAR_CERTAIN"
    end = parse_time(market.end_date)
    if end is not None and (now - end).total_seconds() > past_end_hours * 3600:
        return "PAST_END"
    return None
