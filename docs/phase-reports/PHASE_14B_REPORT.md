# Phase 14B — Trading costs and capacity

## The problem
Phase 14A stopped the engine pretending that any size fills at the best price. It left one optimism
standing: trading was free. It is not, and the cost is not small relative to the edges FRIDAY is
looking for.

Polymarket charges the **taker**:

```
fee = shares × rate × price × (1 − price)
```

That shape matters more than the rate does. The fee is **largest at 0.50** — `rate × 0.25` dollars
per share — and **vanishes as the price approaches 0 or 1**. So:

- A strategy trading genuine uncertainty around 0.50 in a 0.05 category pays **2.5c a share round
  trip**. Any mid-priced edge thinner than that does not exist.
- A strategy trading near-certainties at 0.97 pays about **0.3c a share round trip**.

Every paper result before this phase was computed with that term set to zero. The 9C validation
gate's confidence intervals were therefore measuring a market that does not charge for access.

## The rates
| Category (as `jarvis.market.segments` classifies it) | Rate |
|---|---|
| `crypto`, `crypto_micro` | 0.07 |
| `mentions`, `politics` | 0.04 |
| `economy`, `entertainment`, `weather`, `sports`, `other` | 0.05 |
| `geopolitics` | 0.00 |
| anything unclassified | 0.07 — the highest in the table |

Two deliberate choices:

- **Sources disagree on sports** (0.03 and 0.05 have both been published). The worse number is used.
- **An unclassified market is charged the highest rate, not the lowest.** Guessing low would make an
  unknown market look like the cheapest thing to trade, which is exactly backwards.

`JARVIS_PAPER_FEE_MULTIPLIER` stress-tests the whole schedule — `1.5` asks what a 50% rise would do
to the record — without editing the table.

> **The rates here are a starting point, not an authority.** The published schedule has changed more
> than once. They must be confirmed against Polymarket's own fee page before any live trade. A paper
> record produced with the wrong rate is as misleading as one produced with no rate at all.

### Makers and gas
A resting limit order that someone else fills is a **maker** fill, which pays nothing and may earn a
rebate. FRIDAY's resting strategies submit at market when they trigger, so they are takers;
`credit_maker_fills` defaults to `False` and stays there until a real resting limit order exists on
the exchange. Modelling a rebate we do not earn would be the generous-fill mistake again.

Polymarket's relayer sponsors gas for proxy/Safe wallets, so a CLOB fill costs no gas and
`gas_usd_per_fill` defaults to 0. That is a service someone else pays for and could withdraw, and an
EOA signer pays gas on every approval, split, merge and redemption. The setting exists so that
assumption can be priced rather than forgotten.

## Where the fee is applied
**Folded into the booked price**, not reported alongside it. The entry price the ledger records is
`fill_price + fee_per_share`; the exit price is `fill_price − fee_per_share`.

This is the important design decision. It means the Risk Governor's drawdown numbers, the 7C
scorecard, and the 9C gate's confidence intervals are all net of fees **without any of them knowing
fees exist**. There is exactly one PnL calculation in the system and it is the ledger's. A parallel
"fees to subtract later" column would have let the two drift.

The raw fill price and the fee are both still recorded (`entry_fill_price`, `entry_fee_usd`,
`exit_fill_price`, `exit_fee_usd`, `fee_category`), so the cost of *trading* can be separated from
the cost of *size* in the report below.

The stop and target already hung off the price really paid as of 14A, so they now sit slightly
further away, correctly: a position that cost more to open has further to fall before it has lost
what the plan said it was willing to lose.

## Capacity: at what size does the edge disappear?
An edge is not a number, it is a number **at a size**. A strategy that makes 4c a share on $20 may
make nothing on $500 and lose on $2000, because the extra size fills further down the book and the
fee is charged on all of it. Reporting one PnL figure without the size it was earned at is the same
mistake as reporting a fill at the best price regardless of size — the 14A mistake, one level up.

So every closed trade now stores the book it entered against and the book it exited into
(`paper_plans.entry_book` / `exit_book`). `jarvis/paper/capacity.py` re-walks those real books at a
ladder of stakes ($10 → $2500) and reports, per strategy:

- `realised_gross_usd`, `realised_fees_usd`, `realised_net_usd`, `fee_share_of_gross` — what the
  fee actually took out of the strategy's gross result.
- A rung per stake: `net_usd`, `mean_net_usd`, a 95% interval, `fees_usd`, `filled_usd` and
  `fill_ratio`.
- `max_profitable_stake` — the largest rung that is profitable, where "profitable" requires **all
  three** of: a positive total, a confidence interval that does not straddle zero, and a fill ratio
  of at least 0.99.

That last condition matters. A $500 request against a book holding $100 fills a fifth of the order,
and that fifth may well be profitable. Calling that rung profitable would claim capacity at $500
that never existed. A size the book could not absorb at all is skipped rather than scored as
breakeven, for the same reason: it is a trade that would not have happened, not a trade that broke
even.

Available at `GET /api/paper` → `capacity`.

### What the ladder holds fixed, and therefore cannot tell you
- **Timing.** Each trade is re-run at the same two moments it really happened at. A larger position
  might have been stopped out earlier or held longer; that is not modelled.
- **Market impact.** A $2500 order would have moved the price and drawn a reaction. The book is a
  passive object here, so the ladder is **optimistic at the large end**.
- **Depth beyond what was recorded.** Only the 5 levels a side FRIDAY saw exist, so the ladder is
  **pessimistic in deep markets**.

The two errors point in opposite directions and neither is small. The ladder is a sizing sanity
check, not a capacity guarantee: **if it says the edge dies at $100, believe that; if it says the
edge survives to $2500, do not.**

## Settings
```
JARVIS_PAPER_FEES=true            # off ONLY to reproduce a pre-14B record for comparison
JARVIS_PAPER_FEE_MULTIPLIER=1.0   # 1.5 = what if fees rise 50%
JARVIS_PAPER_GAS_USD=0            # the relayer's sponsorship, priced
```

## Verification
23 new tests (817 → 840). Nine mutations applied, each caught by specific tests:

| Reverted behaviour | Tests that fail |
|---|---|
| Fee made linear in price rather than quadratic | 7 |
| Entry fee not folded into the booked price | 2 |
| Category ignored, one flat rate for everything | 2 |
| Confidence interval ignored — a positive total is enough | 2 |
| Exit fee not netted off | 1 |
| Unknown category charged the cheapest rate | 1 |
| Fee multiplier ignored | 1 |
| A size the book cannot absorb scored as breakeven | 1 |
| A profitable partial fill counted as capacity | 1 |

The last of those was found *by* the mutation run: the original test's unfillable rung was already
unprofitable, so the fill-ratio condition was untested until a test was written for the case that
actually misleads — a profitable partial fill.

Pre-14B test rigs (settlement, signals, resting sources, the live feed, the engine rules) now pass
`CostSettings(enabled=False)` explicitly, so each tests its own concern rather than carrying an
unrelated fee term through every expected price.

## Consequence to expect
**Paper performance gets worse again, and again that is the point.** A flat round trip — buy and
sell at the same price — now loses money, as it does in reality. Combined with 14A:

> Any paper record produced before Phase 14 should be treated as void for validation purposes. It
> was measured against a market with infinite liquidity at the best price and no cost of access.
> Neither is a market.

## Still paper only
No change to who may open a position. LIVE remains hard-rejected in the Risk Governor. No wallet
key, no signer, no order submission exists in this phase.

## Sources
The fee formula and the category rates were taken from Polymarket's documented fee schedule and
cross-checked against two independent public summaries, which agreed on the formula and on every
category except sports. Gas sponsorship is per Polymarket's own gasless-transactions documentation.
These are recorded here as the basis for the defaults, not as a substitute for confirming them.
