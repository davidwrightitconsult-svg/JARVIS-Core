"""
FRIDAY cost model (Phase 14B): the fee a fill would really have been charged.

Phase 14A stopped the engine pretending that any size fills at the best price. It left one
optimism in place: trading was free. It is not.

Polymarket charges the TAKER a fee shaped like

    fee = shares * rate * price * (1 - price)

which is quadratic in price: largest at 0.50 (where `rate * 0.25` dollars per share is charged) and
vanishing as the price approaches 0 or 1. The rate depends on the market's category. Makers — a
resting limit order that someone else fills — pay nothing and may earn a rebate.

That shape matters for FRIDAY specifically. A strategy that trades near-certain outcomes (0.95+)
pays almost nothing; a strategy that trades genuine uncertainty around 0.50 pays the most. Any
mid-priced edge thinner than `rate * 0.25` per share, round trip, does not exist.

### The rates here are a starting point, not an authority
The published schedule has changed more than once and sources disagree on at least one category
(sports has been reported at both 0.03 and 0.05). Where sources conflict this module takes the
WORSE number, and an unrecognised category is charged the highest rate in the table rather than the
lowest. `fee_multiplier` exists so the owner can ask "what if fees rise 50%?" without editing code.

**Before any live trade these rates must be confirmed against Polymarket's own fee page.** A paper
record produced with the wrong rate is as misleading as one produced with no rate at all.

### Gas
FRIDAY would trade through a proxy/Safe wallet, whose onchain actions Polymarket's relayer sponsors,
so a CLOB fill costs no gas. That is a service someone else pays for and could withdraw, and an EOA
signer pays gas on every approval, split, merge and redemption. `gas_usd_per_fill` defaults to 0 to
match the relayer arrangement and exists so that assumption can be priced rather than forgotten.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

COSTS_VERSION = "14B.1"

# Polymarket's published taker coefficients, by the category names jarvis.market.segments already
# assigns. `fee = shares * rate * price * (1 - price)`.
TAKER_RATES: dict[str, float] = {
    "crypto": 0.07,
    "crypto_micro": 0.07,       # the 15-minute up/down markets, where fees arrived first
    "mentions": 0.04,
    "politics": 0.04,
    "economy": 0.05,            # covers both the 0.04 "finance" and 0.05 "economics" buckets
    "entertainment": 0.05,      # "culture"
    "weather": 0.05,
    "sports": 0.05,             # reported as both 0.03 and 0.05; the worse one is used
    "geopolitics": 0.0,         # no fee on the published schedule. A concession, not a guarantee.
    "other": 0.05,
}
# An unclassified market is charged the most any category charges. Guessing low here would make an
# unknown market look like the cheapest one to trade, which is exactly backwards.
UNKNOWN_RATE = max(TAKER_RATES.values())


@dataclass(frozen=True)
class CostSettings:
    enabled: bool = True
    rates: dict[str, float] = field(default_factory=lambda: dict(TAKER_RATES))
    unknown_rate: float = UNKNOWN_RATE
    # Stress test the schedule without editing it: 1.5 asks what a 50% rise would do to the record.
    fee_multiplier: float = 1.0
    # 0 matches the relayer sponsoring gas for proxy wallets. Set it to price that assumption away.
    gas_usd_per_fill: float = 0.0
    # 18A: a resting order that someone else fills is a MAKER fill. It pays no taker fee and may
    # earn a rebate. FRIDAY's `rest()` path fills at its OWN limit price when the market trades
    # through it, which is a maker fill -- this was previously documented as the opposite, and the
    # maker treatment was happening by accident rather than by design.
    credit_maker_fills: bool = True
    # The published maker rebate is a share of the taker fees collected in that market (reported at
    # 15-25% by category). It is NOT modelled by default: a rebate depends on a monthly volume tier
    # FRIDAY has no claim to, and crediting income we may not earn is the generous-fill mistake in
    # another costume. Set it only to ask what a rebate would be worth.
    maker_rebate_share: float = 0.0

    def rate_for(self, category: str | None) -> float:
        if not self.enabled:
            return 0.0
        base = self.rates.get(str(category or "").strip().lower())
        if base is None:
            base = self.unknown_rate
        return max(0.0, base * self.fee_multiplier)


@dataclass(frozen=True)
class Cost:
    """What a fill costs beyond its price. `per_share` is what gets folded into the booked price."""
    fee_usd: float
    gas_usd: float
    rate: float
    category: str
    shares: float

    @property
    def total_usd(self) -> float:
        return round(self.fee_usd + self.gas_usd, 6)

    @property
    def per_share(self) -> float:
        return round(self.total_usd / self.shares, 8) if self.shares > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"fee_usd": round(self.fee_usd, 6), "gas_usd": round(self.gas_usd, 6),
                "total_usd": self.total_usd, "per_share": self.per_share,
                "rate": self.rate, "category": self.category}


FREE = Cost(fee_usd=0.0, gas_usd=0.0, rate=0.0, category="", shares=0.0)


def taker_fee(shares: float, price: float, rate: float) -> float:
    """`shares * rate * price * (1 - price)`, the published shape. Zero for nonsense inputs."""
    for v in (shares, price, rate):
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            return 0.0
    if shares <= 0 or rate <= 0 or not 0.0 < price < 1.0:
        return 0.0
    return round(shares * rate * price * (1.0 - price), 8)


def cost_of(settings: CostSettings, shares: float, price: float, category: str | None,
            maker: bool = False) -> Cost:
    """The cost of one fill. A maker fill pays no taker fee, and may be credited a rebate if the
    settings model one (they do not by default)."""
    if not settings.enabled or not isinstance(shares, (int, float)) or not math.isfinite(shares) or shares <= 0:
        return FREE
    gas = max(0.0, float(settings.gas_usd_per_fill))
    if maker and settings.credit_maker_fills:
        # A rebate is negative cost: a share of the taker fee that would have been charged.
        share = max(0.0, min(1.0, float(settings.maker_rebate_share)))
        rebate = taker_fee(shares, price, settings.rate_for(category)) * share
        return Cost(fee_usd=(round(-rebate, 8) if rebate else 0.0), gas_usd=gas, rate=0.0,
                    category=str(category or "unknown"), shares=float(shares))
    rate = settings.rate_for(category)
    return Cost(fee_usd=taker_fee(shares, price, rate), gas_usd=gas, rate=rate,
                category=str(category or "unknown"), shares=float(shares))


def round_trip_per_share(settings: CostSettings, price: float, category: str | None) -> float:
    """The FEE half of a round trip, per share: buy at `price`, sell again at the same price. This
    ignores the spread, which `round_trip` below does not."""
    rate = settings.rate_for(category)
    one = taker_fee(1.0, price, rate)
    gas = max(0.0, float(settings.gas_usd_per_fill))
    return round(2.0 * one + 2.0 * gas, 8)


@dataclass(frozen=True)
class RoundTrip:
    """What it costs, per share, to get into a position and back out of it again.

    This is the number every strategy has to beat, and it is the reason Phase 17's oracle lost
    money while seeing the future: a 2c spread and a 0.05 fee category cost about 4.5c a share at
    mid prices, and an hourly move does not clear that often enough.

    `fraction_of_cost` is the same figure as a share of the capital committed, which is the form
    that compares across prices: 4.5c is 9% of a 50c share and 5% of a 90c one."""
    spread: float          # paid once: in at the ask, out at the bid
    entry_fee: float
    exit_fee: float
    total: float
    cost_basis: float      # what one share costs to buy
    category: str

    @property
    def fraction_of_cost(self) -> float:
        return round(self.total / self.cost_basis, 6) if self.cost_basis > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"spread": round(self.spread, 6), "entry_fee": round(self.entry_fee, 8),
                "exit_fee": round(self.exit_fee, 8), "total": round(self.total, 6),
                "cost_basis": round(self.cost_basis, 6),
                "fraction_of_cost": self.fraction_of_cost, "category": self.category}


def round_trip(settings: CostSettings, bid: float, ask: float, category: str | None,
               maker_entry: bool = False) -> RoundTrip | None:
    """In at the ask and out at the bid, with the fee charged on both legs.

    The spread is paid ONCE, not twice: buying at `ask` and selling at `bid` with an unchanged mid
    loses exactly the spread. A maker entry rests at the bid instead of crossing, which saves both
    the spread and the entry fee -- the structural reason the maker path matters."""
    ok = lambda x: isinstance(x, (int, float)) and math.isfinite(x)      # noqa: E731
    if not (ok(bid) and ok(ask)) or not 0 < bid <= ask < 1:
        return None
    entry_price = bid if maker_entry else ask
    spread = 0.0 if maker_entry else round(ask - bid, 8)
    entry = cost_of(settings, 1.0, entry_price, category, maker=maker_entry)
    exit_ = cost_of(settings, 1.0, bid, category)
    total = spread + max(0.0, entry.fee_usd) + exit_.fee_usd + entry.gas_usd + exit_.gas_usd
    return RoundTrip(spread=spread, entry_fee=entry.fee_usd, exit_fee=exit_.fee_usd,
                     total=round(total, 8), cost_basis=entry_price,
                     category=str(category or "unknown"))
