# Phase 13B — Settlement (pre-resolution exit)

## The problem
A Polymarket contract pays $1 or $0 at resolution. In the last minutes before that, the book thins
and the spread widens: a position that needs to be sold then is sold badly. That is the liquidity trap.

## The rule
`settle_before_minutes` (default 30) before the market's recorded end date, an open position is closed
at the recorded/live bid, with the reason `settlement`.

Two deliberate exceptions:
- **`settle_hold_above` (default 0.97).** A position already bid at 0.97+ is left to resolve: selling
  gives up at least 3c of spread to capture at most 3c of remaining value. This is a cost comparison,
  not a view on the outcome.
- **The stop, the target and resolution still come first.** Settlement is only reached by a position
  that none of those have already closed.

A market with no recorded end date is never settled; the ordinary time limit still applies.

## Why it is measured, not assumed
Exiting early is a trade-off, not a free win: in an efficient market, selling at the bid costs the
half-spread, while holding to resolution has variance and oracle risk. Rather than assert which is
better, every settlement is recorded with what the position *would* have paid at resolution.
`GET /api/paper` -> `settlement` reports `gain_from_exiting`: positive means leaving early paid,
negative means this policy is costing money and `JARVIS_PAPER_SETTLE_BEFORE_MINUTES` should change.

## Heartbeat
Settlement is time-based, so it cannot wait for a price move. The 13A trigger now also ticks the
engine every `JARVIS_TRIGGER_HEARTBEAT_S` (default 60s). Without the trigger, the ordinary
post-scan tick still applies.

## Still paper only
No change to who may open a position. LIVE stays locked; the Risk Governor is untouched.
