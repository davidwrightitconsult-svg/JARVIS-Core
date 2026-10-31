# Phase 15 — Multi-leg arbitrage

## The gap this closes
A two-outcome market pays exactly $1 to one side. If the best ask of YES plus the best ask of NO is
below $1, buying one share of each locks in the difference whatever happens — no forecast, no view.

The 9C scanner has been reporting these since Phase 9. **Nothing traded them.** It was the one place
in FRIDAY where the research found something concrete and the execution side had no answer.

## Why this had to come third
Both 14A and 14B had to exist first, or this module would have been a machine for inventing money.

**Depth (14A).** The gap is almost always tiny and sits on a few dollars of resting size. Filling
"any size at the best ask" would have turned a $3 opportunity into a $500 one, and the paper record
would have looked spectacular.

**Cost (14B).** The fee is the whole story here, and it is why `scan_inconsistencies` takes a `fee`
parameter that defaulted to `0.0`. The taker fee is `rate × price × (1 − price)` per share, so a
pair at prices p and (1 − p) costs roughly `rate × 2p(1 − p)` per pair to enter:

| Pair | Gross gap | Fee at 0.05 | Net per pair |
|---|---|---|---|
| 0.49 / 0.49 | 2.0c | 2.5c | **−0.5c — a loss** |
| 0.95 / 0.03 | 2.0c | 0.4c | +1.6c |
| 0.49 / 0.49, geopolitics (0 fee) | 2.0c | 0 | +2.0c |

**A balanced pair near 0.50 pays the maximum fee on both legs, so a 2c arbitrage there is a losing
trade.** The same 2c gap in a lopsided market, or in a zero-fee category, is real. None of that is
visible without the cost model. This is the single most useful thing Phase 14 bought.

So the trader refuses with the arithmetic in the refusal: `gross gap 0.0200 does not survive fees:
-0.0051 a pair net (other)`.

## Atomicity
An arbitrage is riskless only **as a whole**. One leg filled and the other not is a naked
directional bet — the opposite of what was approved.

`PaperLedger.open_legs_checked` is the new way in:

1. One governor evaluation for the **combined** position, under the entry lock.
2. Both legs priced against the stake the governor actually approved (same `price_for` pattern as
   14A, extended to a group).
3. Stakes checked against cash **as a group**.
4. All legs inserted in a **single transaction**: any failure rolls every one of them back.
5. The legs share one `decision_id`, and that decision cannot be reused.

`units` is passed explicitly per leg rather than re-derived from a rounded price. A pair is *equal
shares* on both legs; deriving them from `stake / round(price)` left the two legs fractionally
unequal, which is a small naked bet. That was caught by a mutation test, not by reasoning.

## Sizing: bisection, not an even split
The legs can cost wildly different amounts. Splitting a $50 budget evenly between a 0.95 leg and a
0.03 leg buys 26 pairs and leaves most of the budget idle. `price_pair` instead bisects on the
*number of pairs*, since cost per pair is monotone in the number of pairs, and the top-of-book sum
gives a hard upper bound. The result is exact to the penny and never exceeds the budget.

A size that cannot be matched **completely on both legs** is refused outright: 100 shares of one and
10 of the other is a directional bet on 90 shares.

## The exit is resolution, and nothing else
A complete pair pays exactly $1 whichever way the market resolves. So it is held to resolution and
closed at the recorded payoffs. Selling early would realise `bid_yes + bid_no`, which is *below* $1
— it would hand back the entire edge and the spread with it.

These positions deliberately carry **no plan row**, which is how they stay out of reach of the
engine's stop, target, time limit and 13B settlement. Every one of those four exits would destroy
the thing being captured. A market that has already resolved is also never opened, even though its
last snapshot still shows a gap.

## What can still go wrong, and is not modelled
Stated plainly, because this strategy looks safer than it is:

- **The gap may be stale.** The scan sees one snapshot every 15 minutes. A gap that was there when
  recorded may be gone before an order could reach the exchange. **This is the largest unmodelled
  risk** and the main reason a paper record here is weaker evidence than it looks. The live feed
  (13A) narrows the window but does not close it.
- **Real atomicity does not exist on the CLOB.** Two orders are two orders. The live version needs a
  leg-failure policy — unwind the filled leg immediately at the bid and wear the loss — which paper
  trading cannot test. `open_legs` gives atomicity *in the ledger*, not at the exchange.
- **Resolution risk.** The pair pays $1 only if the market resolves at all. A voided market or an
  oracle dispute breaks the identity the whole trade rests on.
- **Multi-candidate events** (buy every candidate in a field for under $1) are not covered: FRIDAY
  does not record which markets belong to one event.

The report exposes `slippage_vs_expected` — realised minus expected locked profit — which is exactly
the measurement of these unmodelled risks. If it is persistently negative, the gaps are stale and
this strategy does not work, whatever the scan says.

## Settings
```
JARVIS_PAPER_ARB=false            # OFF by default, and only trades if the gate allows the strategy
JARVIS_PAPER_ARB_BUDGET=50        # what one opportunity asks for; the governor decides the real size
JARVIS_PAPER_ARB_MIN_LOCKED=0.005 # minimum locked profit per pair, NET OF FEES
```
Available at `GET /api/paper` → `arbitrage`.

## Verification
25 new tests (840 → 865). Ten mutations applied, each caught by specific tests:

| Reverted behaviour | Tests that fail |
|---|---|
| A resolved market opened again | 2 |
| Fees ignored when pricing a pair | 1 |
| Net-of-fee floor removed | 1 |
| Incomplete legs accepted as a pair | 1 |
| Gate check removed | 1 |
| A gate failure treated as permission | 1 |
| An already-held market opened again | 1 |
| Unequal legs: units re-derived from a rounded price | 1 |
| Legs allowed to exceed the approved stake | 1 |
| Decision-reuse guard removed | 1 |

Rollback is tested two ways: validation refusing before the first insert, and an exception raised
*after* the rows are written, which must still leave no position and unchanged cash.

## Still paper only
LIVE remains hard-rejected in the Risk Governor. The gate cannot approve live trading. No wallet
key, no signer and no order submission exists in this phase — and for this strategy in particular,
the leg-failure policy that live trading would require has not been designed, let alone tested.
