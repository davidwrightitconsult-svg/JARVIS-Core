# Phase 17 — Calibrating the instrument

## The problem
Phases 7 through 16 built an apparatus for measuring edge. Nothing ever checked that the apparatus
can measure.

Its arithmetic is unit-tested to the penny, but that is a different claim. A thermometer with
perfect arithmetic and a bad sensor reads 20 degrees in a furnace. Every number this platform has
produced rests on an instrument that had never been pointed at something whose answer was known in
advance.

## The method
Plant strategies whose informational content is **known by construction**, run them through the
real pipeline end to end, and check the instrument reaches the verdict the construction requires.

Nothing is mocked. It drives the real `MarketStore`, the real `Scorer`, the real edge report, the
real `CostSettings` and the real `Executor`. If any of those are wrong, the calibration fails —
which is the point.

| Planted strategy | What it knows | What the instrument must find |
|---|---|---|
| `ORACLE` | sees the exit price, **and only trades moves that clear the round trip** | `MAKES_MONEY` |
| `WEAK` | sees the exit price, trades only the small moves | `KNOWS_SOMETHING_BUT_COSTS_EAT_IT` |
| `COINFLIP` | nothing at all (seeded random direction) | **not credited with information** |
| `ANTI` | sees the exit price and bets against it | conclusively worse than random |

The two failure modes that would make every number worthless are opposite, so one harness has to
catch both:

- **A false positive on noise.** If `COINFLIP` is credited with an edge, every result ever produced
  is suspect.
- **A false negative on a real edge.** If `ORACLE` is dismissed, the platform would veto something
  that works.

The required outcomes are asserted as **properties, not verdict strings**, and written down before
the run. Several verdicts can be defensible for one record — a coin flip both fails to show
information *and* loses money, and both statements are true — so what is pinned is the thing that
would actually matter if it were wrong.

## The result
```
INSTRUMENT CALIBRATION 17.1 — can the edge report measure?
40 synthetic markets · 30 scans · spread 0.02 · seed 17
planted: ORACLE=145 · WEAK=419 · COINFLIP=1040 · ANTI=911

  [PASS] ORACLE    must makes money: the whole after-fee interval above zero
            145 scored · net 0.034834 · vs coinflip 0.074753 · MAKES_MONEY
  [PASS] WEAK      must knows something, but does not make money
            419 scored · net -0.021485 · vs coinflip 0.017477 · KNOWS_SOMETHING_BUT_COSTS_EAT_IT
  [PASS] COINFLIP  must is NOT credited with information it does not have
           1040 scored · net -0.041111 · vs coinflip -0.00195 · NEGATIVE_AFTER_COSTS
  [PASS] ANTI      must is shown to be conclusively worse than random
            911 scored · net -0.074821 · vs coinflip -0.035453 · WORSE_THAN_A_COIN_FLIP

  the 16B executor, reading that report, on a $20 order:
    ORACLE    ALLOW $20  — allowed up to 20 (APPROVED_FOR_PAPER)
    WEAK      REFUSE  — WEAK is losing money now: 419 scored signals ...
    COINFLIP  REFUSE  — COINFLIP is losing money now: 1040 scored signals ...
    ANTI      REFUSE  — ANTI is losing money now: 911 scored signals ...

CALIBRATED
```

A thousand coin flips, and the information interval still straddles zero (`-0.00195`, ci95
`[-0.0074, +0.0040]`). The gate approves all four; the Executor lets exactly one of them open a
position. The full chain — measure, judge, permit — works on data whose answer was known in advance.

Calibration holds at seeds 1, 99 and 2026 as well as the default, so it is not a property of one
lucky draw.

## What the first run taught, which was not in the plan
The original `ORACLE` traded **every** move it foresaw. It came out as
`KNOWS_SOMETHING_BUT_COSTS_EAT_IT`:

```
gross       +0.0138     what the 7C scorecard would report
after fees  -0.0060     what it actually makes
coin flip   -0.0397
vs coinflip +0.0337     it clearly knows something
```

**Perfect foresight loses money.** With a 2c spread and a 0.05 fee category the round trip costs
about 4c a share, and over a one-hour horizon the price does not move far enough often enough to
clear that.

That is Phase 14's lesson arriving in concrete form, and it reframes the whole problem:

> On this venue **information is not the binding constraint — selectivity about size is.** A
> profitable strategy does not need to be right more often. It needs to only trade when the
> expected move is a multiple of the round trip. "Be right" is not a strategy; "be right about
> moves bigger than 4c" might be.

`ORACLE` was therefore rebuilt as a *selective* oracle (`oracle_min_move`, default 0.06), which is
the only version of it that is profitable — and that change is itself the finding.

Two of the four original expectations were wrong rather than the code:

- `COINFLIP` reads `NEGATIVE_AFTER_COSTS`, not `NO_EDGE`. Its information interval straddles zero
  because with a finite sample you can never *prove* zero information, only fail to find any —
  while it definitely loses money. The verdict is accurate; the expectation was naive.
- `ORACLE` as first built genuinely was not a profitable strategy.

## A real bug this found
`MarketStore.due_signals` returns at most 500 rows per call, and `Scorer.score_due` makes one call.
The first run therefore scored 500 of 2515 planted signals and truncated every strategy unevenly —
`ORACLE` came back with 24 of its 145, enough to be reported `INSUFFICIENT_DATA`.

In production this self-heals (signals stay due until scored, and scans run every 15 minutes), so
it is not a defect in the scorer. But any analysis that scores a large backlog in one pass gets a
biased subsample, and nothing said so. The rehearsal now loops until nothing is due, and asserts
that every planted signal was scored.

## What this does not prove
Stated in the tool's own output, not just here:

> **A planted edge being found says the measuring device works. It says nothing about whether a
> real edge exists.**

`ORACLE` cheats by construction. The synthetic market has no relationship to Polymarket beyond
having plausible spreads and depth. Only forward paper trading on live data can say whether any
real strategy has an edge — which remains the one thing this project has never done.

---

# Phase 17B — Does the whole machine turn?

Phase 17 calibrated the *measuring* apparatus. This asks the other half, which nothing had ever
asked as one question: does a signal actually become a position, and does that position survive
every stage of the machine with depth, fees, atomicity and reporting intact?

Every component is unit-tested in isolation. That is a different claim. A mis-wired observer order,
a settings object that never reaches the engine, a fill priced against the wrong stake — each of
those passes every unit test and breaks the platform.

```
  1. SIGNAL AND FILL (14A depth + 14B cost)
     wanted $40 at the best ask 0.50, where only $5 rests
     only $15.30 rests within 0.02 of it, so the order was cut to that BY THE BOOK,
       not by the governor
     approved $15.30 · walked 3 level(s) · filled at 0.51 (slippage 0.01)
     fee $0.3749 (economy) folded in -> booked at 0.522495
     stop set at 0.0 — measured from what was PAID, not from 0.50

  2. EXIT (walks the bids, no slippage cap — leaving is not optional)
     reason time · best bid 0.56 · sold at 0.55 (slippage 0.01) across 3 level(s)
     gross +1.1713 − fees 0.7372 = booked PnL +0.4430

  3. ARBITRAGE PAIR (15: both legs or neither, held to resolution)
     two asks summed to 0.98 · 2 legs on ONE governor decision · equal shares: True
     20.4 pairs cost $20.00 (fees $0.00) to lock $0.41
     resolved: expected $0.41, realised $0.41, slippage +0.00 · pays exactly $1 a pair: True

  4. WHAT THE REPORTS SAY
     capacity ladder (stake, net, fill ratio):
       [(10.0, +0.4417, 1.00), (40.0, -2.7719, 0.38), (200.0, -2.7719, 0.08)]
     edge verdicts: {'RESEARCH_FORECAST': 'INSUFFICIENT_DATA'}
```

## What it found

**A negative stop price.** `SignalTrader` deliberately passes a stop *distance* of 0.999 to make
the stop unreachable, and the engine recorded `stop_price = entry - 0.999 = -0.4765`. Behaviourally
correct — a bid can never reach it — but meaningless as data and a trap for anything that reads the
column assuming prices are in `[0, 1]`. Now clamped to 0, which is equally unreachable and is a
price.

**A one-second miss in the scorer's exit window.** The window is
`[expires_at, expires_at + max(1h, half the horizon)]`. The first run's exit snapshot landed one
second past it and scored `NO_EXIT_DATA`, which is why the first edge report came back empty. Not a
defect — but it means a scan cadence that straddles the window boundary silently loses signals, and
nothing says so.

**The demonstration mislabelled its own constraint.** The run reported "governor approved $15.30"
when the governor had nothing to do with it: $15.30 is exactly the capacity inside the slippage
band, so the *book* cut the order and the governor approved that in full. Worth getting right,
because which constraint binds is the whole question when sizing.

**Fees took 63% of a winning trade.** A gross gain of $1.17 became a booked $0.44. Not a bug — the
point of 14B, arriving on a concrete trade rather than in a table.

**Arbitrage legs never appear in the capacity report**, because they carry no plan row (that is how
they stay out of reach of the engine's exits) and the report joins positions to plans. Correct by
design, now asserted so it cannot change silently.

## Running it
```bash
python -m jarvis.lab lifecycle            # one position, signal to settlement
```

## Verification
19 tests (930 → 949), covering each stage's artifact plus the controls that matter: with
`JARVIS_EXECUTE` off the identical lifecycle commits not a cent; a strategy the gate does not allow
trades nothing; a market too thin is refused rather than filled generously; a resolved arbitrage
market is never reopened.

---

## Running it
```bash
python -m jarvis.lab calibrate            # exits 0 if calibrated, 1 if not
python -m jarvis.lab calibrate --seed 99
python -m jarvis.lab edge --days 30       # the 16A report on FRIDAY's real record
```
`calibrate` needs no market store: it builds a synthetic one in a temp directory, because the
question is whether the measuring device works, not what any real market did. It is worth running
after any change to the scorer, the cost model or the edge report — a change that breaks the
instrument now breaks a test rather than quietly corrupting every number downstream.

## Verification
15 new tests (915 → 930), covering both failure modes directly, reproducibility, calibration across
four seeds, and that the harness drives the real store rather than a stub (recorded snapshot and
book-level counts are asserted against the simulation's own parameters).

## Still paper only
LIVE remains hard-rejected in the Risk Governor. Nothing in this phase touches execution; it only
reads.
