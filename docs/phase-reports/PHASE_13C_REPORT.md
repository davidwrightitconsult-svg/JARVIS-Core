# Phase 13C — Signal trading: the gate's judgement reaches the engine

## The gap this closes
The scorecard (7C) measured every signal. The gate (9C) judged the evidence and could mark a
strategy APPROVED_FOR_PAPER. But the only thing that ever placed an order was the Bollinger forward
test (12B). **The gate could approve a strategy and the platform would still trade nothing.**

`SignalTrader` is the hands for the gate's own judgement. It originates nothing.

## The bet placed is the bet that was measured
This is the whole point. The 7C scorecard scores a signal as:

```
UP    buy the token at the entry ask, sell at the exit bid when the horizon ends
DOWN  the other side of a two-outcome market, the same way
net   exit_bid - entry_ask          <- there is no stop and there is no target
```

So that is exactly what is placed: **entry at the recorded ask, exit at the signal's own expiry, no
stop, no target.** Adding a stop would be a different bet from the one the gate approved, and its
paper record would not be evidence for anything. Size is the Risk Governor's job; a long position
cannot lose more than its stake, which the governor already caps at 2% of equity.

## Two honest differences, both conservative
- **DOWN buys the other outcome's token at its own recorded ask.** The scorecard models that side as
  `1 - best_bid` of the signalled token. A real book is usually a little worse, so the paper record
  is the stricter test.
- **The engine refuses stale quotes, wide spreads and tail prices.** The scorecard scores those
  signals anyway. The paper record will cover fewer signals than the scorecard, never more.

## Rules fixed in advance
- only strategies the gate currently allows (APPROVED_FOR_PAPER or FORWARD_TEST)
- only signals still inside their horizon, observed within 30 minutes
- DOWN only where the market has exactly two outcomes
- one paper order per signal, ever, across restarts
- held to the signal's own expiry, then closed at the bid

## Fails closed
No gate, a gate that raises, or a gate that approves nothing -> nothing trades. `JARVIS_PAPER_SIGNALS`
is off by default, and turning it on does not by itself cause a single trade: the gate must first
have evidence. That is the discipline working, not a fault.

## Operate
`GET /api/paper` -> `signal_trading` shows which strategies the gate allows, how many orders were
placed, refused or skipped, and why.
