# Phase 18 — The economy of a trade

Three findings from Phases 14, 17 and 17B all pointed the same way, so this phase acts on them:

- Perfect foresight lost money (17), because a 2c spread plus a 0.05 fee is a ~4.5c round trip and
  an hourly move does not clear it often enough.
- Fees took 63% of a winning trade (17B).
- `BollingerResting`'s minimum target was a flat **1c** — below the cost of the trade it authorised.

The conclusion is not "trade less". It is that **the round trip is the first thing any strategy has
to beat, and it varies by more than five-fold across the markets FRIDAY records.** The same
prediction is profitable in one market and a loss in another.

---

## 18A — The maker path, made deliberate

`rest()` fills at **its own limit price** when the market trades through it. That is a maker fill:
the order was resting and someone else crossed into it. Three things were wrong with how that was
handled:

1. **It paid no fee only by accident.** No cost was applied on that code path at all. Meanwhile
   `costs.py` documented the opposite — `credit_maker_fills=False` with the comment *"FRIDAY's
   resting strategies submit at market when they trigger, so they are takers."* That comment was
   simply wrong; I wrote it from reading the wrong function.
2. **It was not depth-bounded.** `open_checked` was called without `price_for`, so a resting order
   filled its whole stake at the limit regardless of whether anyone was there to sell it. That is
   the pre-14A generosity, still live on one path.
3. **Nothing recorded that a fill was a maker fill**, so the cost of a maker strategy could not be
   told apart from a taker one after the fact.

All three are fixed. `credit_maker_fills` now defaults to `True` and means what it says; resting
fills are bounded by the sell interest observable at or better than the limit; and `paper_plans`
carries a `maker_entry` flag.

### Why this is the largest economic lever in the platform

A maker entry rests at the bid, so **no spread is crossed going in**. The exit sells at the bid, so
none is crossed coming out either. A maker-in / bid-out round trip therefore costs *only the exit
fee*:

| Entry style | Spread | Fees | Round trip | % of capital |
|---|---|---|---|---|
| Taker (cross to the ask) | 2c | 2.5c | **4.5c** | 9.0% |
| **Maker (rest at the bid)** | **0c** | **1.25c** | **1.25c** | **2.6%** |

That is a 3.5× reduction in the cost of doing business, available today, on a path the platform
already has.

### The honest caveat, which is not modelled
**You get filled precisely when you are wrong.** A bid resting at 0.48 fills when the market falls
to 0.48 — often because someone knows something. That is adverse selection, and it is the real
price of being a maker. The model captures the timing but not the information asymmetry, so a maker
strategy will look better in paper than it trades. Nothing here fixes that, and it should temper
any enthusiasm the table above produces.

### The rebate is not credited
The published maker rebate (reported at 15–25% of taker fees collected, by category) depends on a
monthly volume tier FRIDAY has no claim to. `maker_rebate_share` defaults to **0**: booking income
it may not earn is the same mistake as filling at a price it could not get. The setting exists so
the owner can ask what a rebate would be worth, not so it can be assumed.

---

## 18B — Round-trip cost as a first-class gate

`jarvis.paper.costs.round_trip(settings, bid, ask, category, maker_entry=False)` is now the one
definition of what a trade costs, returning the spread, both fee legs, the total, and — the figure
that compares across prices — the total as a **fraction of the capital committed**.

The spread is paid **once**, not twice: buying at the ask and selling at the bid with an unchanged
mid loses exactly the spread.

```
economy      0.48/0.50   spread 0.020  fees 0.0250  total 0.0450 =  9.0% of capital
crypto       0.48/0.50   spread 0.020  fees 0.0350  total 0.0550 = 11.0%
economy      0.88/0.90   spread 0.020  fees 0.0098  total 0.0298 =  3.3%
geopolitics  0.48/0.50   spread 0.020  fees 0.0000  total 0.0200 =  4.0%
economy      0.44/0.50   spread 0.060  fees 0.0248  total 0.0848 = 17.0%
maker entry  0.48/0.50   spread 0.000  fees 0.0125  total 0.0125 =  2.6%
```

Two gates now use it:

**`PaperSettings.max_round_trip_fraction` (default 0.15).** An entry is refused when getting in and
out again would cost more than this share of the capital committed, with the arithmetic in the
refusal:

> `the round trip costs 0.0848 a share, 17.0% of the 0.50 it commits (spread 0.050 + fees 0.0348, crypto); the limit is 15%`

**`BollingerResting`'s target floor** is now `max(1c, 1.5 × round_trip)` instead of a flat 1c, so it
adapts to where the trade is: small in the tails and in zero-fee categories, large in dear ones.
The same signal with the same 2.45c target is placed in a cheap venue and refused in a 0.07
category where the exit fee alone is 1.7c.

---

## 18C — Venue economics: where is it cheapest to trade?

`python -m jarvis.lab economics` reports, over the recorded universe: the round trip by fee category
and price band, the same as a fraction of capital, what a maker entry would save, and the cheapest
and dearest individual markets.

### What it found that I did not expect
The **dearest** market in a test universe spanning three categories was not the 0.07 crypto one. It
was the `No` leg of the **zero-fee** geopolitics contract, priced at 0.06/0.08 — because a 2c spread
is a quarter of an 8c contract.

> The fee rate is only one of three drivers, and on cheap contracts the spread dominates everything.
> A zero-fee market can be the worst place to trade on the venue.

That is now an asserted test rather than a footnote, because it is exactly the kind of thing a
strategy aimed at "cheap longshots in free categories" would walk straight into.

---

## Verification
19 new tests (954 → 973). Twelve mutations applied, each caught:

| Reverted behaviour | Tests that fail |
|---|---|
| Spread counted twice in the round trip | 6 |
| Round trip measured against 1.0 rather than the cost basis | 6 |
| Maker charged the taker fee | 5 |
| Rebate credited by default | 4 |
| Round-trip entry gate removed | 3 |
| Economics report misclassifies every market | 3 |
| Resting fill priced as a taker | 2 |
| Maker entry still crosses the spread | 1 |
| Resting fill not bounded by observable liquidity | 1 |
| Resting depth floor removed | 1 |
| Target floor back to the flat 1c | 1 |
| Economics report ignores the maker saving | 1 |

Two of those survived the first run and prompted real work: the maker assertion was loose enough to
pass a mutation that entered at the ask (now pinned to `cost_basis == bid`), and **nothing at all
covered the new target floor** until a test was written for it. Writing that test is what surfaced
the spread-free nature of a maker round trip, which had been stated wrongly in my own first draft of
this document.

## Settings
```
JARVIS_PAPER_MAX_ROUND_TRIP=0.15        # refuse markets that cost more than this to trade
JARVIS_PAPER_MAKER_REBATE_SHARE=0       # off: income FRIDAY may not earn is not income
```

## What this does not do
It does not make any strategy profitable. It removes two places where the platform was lying to
itself about cost, and it refuses trades whose economics cannot work regardless of how good the
prediction is. Whether any prediction is good remains unanswered and unanswerable without live data.

## Still paper only
LIVE remains hard-rejected in the Risk Governor.
