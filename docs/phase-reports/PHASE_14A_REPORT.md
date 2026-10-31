# Phase 14A — Depth-aware fills

## The problem
Every paper result FRIDAY has produced so far was measured with a fill model that gave away free
money. `PaperEngine.submit` read the best ask from the recorded snapshot and filled the whole order
at that price, whatever the size. Ask for $500 in a market where $40 rests at the best ask and the
engine booked $500 at the best ask.

This matters more than it sounds:

- The error **grows with position size**, which is exactly the direction that makes a strategy look
  scalable when it is not.
- It is **largest in thin markets**, which is where mispricings — the things the scanner flags —
  actually live. The signal and the error are correlated.
- Exits had the same flaw in reverse: an entire position cleared at the best bid.

So a profitable paper record was not evidence that a strategy worked. It was evidence that the fill
model was generous. Nothing downstream could be trusted: not the scorecard, not the validation
gate's confidence interval, not the Risk Governor's drawdown numbers.

## What changed

**`jarvis/paper/fills.py` (new).** Pure arithmetic, no I/O: walk a book level by level the way a
taker does.

- `buy_for_stake(levels, stake, max_slippage)` — spend up to `stake` taking asks, best first.
  Returns the volume-weighted average price actually paid, the shares received, how many levels
  were consumed, and whether the request filled completely.
- `sell_shares(levels, shares, max_slippage)` — the exit side, where the quantity is shares held
  rather than a sum of money.
- `capacity(levels, max_slippage)` — the most that could be bought inside the slippage band. This
  is the honest ceiling on a position: ask for more and the extra is imaginary.

Prices are dollars per $1-payoff share and `size` is a share count, so the money resting at a level
is `price * size`. Levels arrive best first (bids descending, asks ascending), which is how
`Book` in `jarvis/market/models.py` sorts them before storage and how `MarketStore.levels` returns
them.

**Entries are gated on depth and priced by walking the book.** `submit()` now computes capacity
inside `max_slippage` (default 0.02) of the best ask. Below `min_depth_usd` (default $5) the order
is refused — `not enough depth: $X rests within 0.02 of the best ask` — rather than filled against
a book too thin to show a real price. Above it, the requested stake is **cut to what actually
rests there** before the Risk Governor ever sees it. Entries behave like a limit order: levels
beyond the band are left alone.

**The fill is priced against the stake the Governor approved, not the one requested.** The Governor
reduces stakes; pricing the fill against the original request would charge slippage that was never
incurred. `PaperLedger.open_checked` now takes an optional `price_for(approved_stake)` callable and
prices the fill after the verdict is known.

**The stop and the target hang off the price really paid.** Previously both were derived from the
hoped-for best ask. A fill that walked the book starts further from its stop than the plan
intended, and the plan row now records that honestly.

**Exits walk the bids with no slippage cap.** Leaving is not optional, so `_exit_fill` takes what
the book shows rather than refusing. It is still optimistic — it assumes the whole visible book is
ours — but it no longer pretends a position clears at the best bid.

**Fill quality is recorded.** `paper_plans` gains seven columns via the usual additive migration:
`entry_requested_stake`, `entry_slippage`, `entry_levels`, `entry_complete`, `exit_slippage`,
`exit_levels`, `exit_complete`. The cost of being large is now a measurable quantity rather than an
assumption.

**`MarketStore.levels` tie-break fix.** The query ordered by `observed_at DESC` alone. Two scans
can share a timestamp, and the book belonging to the quote being filled is the one from the *latest*
of them. `snapshot_id DESC` now breaks the tie. Without this, a trade could be priced against a
book that was never the book at that moment — observed as a real test failure, not a theoretical one.

## The live feed now carries real depth
The 13A trigger streams quotes that are fresher than any snapshot, and a fresher live quote wins.
Before 14A that quote had no book attached, so the freshest quotes silently fell back to the
generous model — the defect returning through the side door.

- A `book` event is parsed into full best-first levels and passed through `on_quote`; `LiveQuotes`
  stores and returns them.
- A `price_change` reports the new size resting at one price (`0` removes the level), so the book is
  **kept current incrementally** between full snapshots instead of being discarded on every tick.
- After folding a change in, the maintained top is compared against the `best_bid`/`best_ask` the
  same frame reports. If they disagree, a change was missed: the depth is dropped and FRIDAY waits
  for the next full snapshot. Pricing a fill against a book known to be wrong is worse than having
  no depth at all. The count is exposed as `book_desync` on `/api/trigger/status`.

## What this model still assumes
Named explicitly, because these are the remaining reasons a paper record is not a live record:

- **The whole visible book is available to us.** In reality part of it may be cancelled in the
  moments it takes to act.
- **We are alone.** Our own order does not move the price and nobody reacts to it. There is no
  market-impact term.
- **Only the levels FRIDAY recorded exist** (5 a side by default). Deeper liquidity is invisible,
  so a large order is modelled pessimistically — the safe direction.
- **A recorded snapshot is up to 15 minutes old.** The book has moved since. The live feed narrows
  this to seconds when the trigger is running.
- **No fees and no gas.** That is Phase 14B, and until it exists the numbers are still optimistic.

## Settings
```
JARVIS_PAPER_DEPTH_AWARE=true     # off ONLY to reproduce a pre-14A result for comparison
JARVIS_PAPER_MAX_SLIPPAGE=0.02    # how far past the best ask an entry will pay
JARVIS_PAPER_MIN_DEPTH_USD=5      # below this a market is not tradeable at all
```

## Verification
21 new tests (796 -> 817). `jarvis/tests/test_paper_fills.py` checks the arithmetic against
hand-computed answers, then drives the engine against books of known shape.

Six mutations were applied and each one is caught by a specific test:

| Reverted behaviour | Tests that fail |
|---|---|
| Fill priced at the best ask instead of walking the book | 2 |
| Depth gate removed | 2 |
| Stake not cut to capacity | 1 |
| Exit fills at the best bid again | 1 |
| Stop measured from the hoped-for ask | 2 |
| Book tie-break removed from the store | 1 |

## Consequence to expect
**Paper performance should get worse, and that is the point.** Entries will be refused in thin
markets, stakes will be cut, and realised prices will be worse than the quotes that triggered them.
Any strategy that was only profitable under the old model was never profitable. Results recorded
before this phase should be treated as void for validation purposes — the 9C gate's confidence
intervals were computed on fills that could not have happened.

## Still paper only
No change to who may open a position. LIVE remains hard-rejected in the Risk Governor; the gate
cannot approve live trading. No wallet key, no signer, no order submission exists in this phase.
