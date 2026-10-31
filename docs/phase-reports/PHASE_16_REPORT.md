# Phase 16 — Testing for edge, and the authority to act on it

Two halves. **16A** answers "does this strategy have an edge?" honestly. **16B** is the authority
that stops trading when the answer changes.

---

## 16A — The edge report

### The inconsistency this fixes
Since 14B the paper engine charges the taker fee on both sides of every fill. The 7C scorecard does
not: `jarvis/quant/scoring.py` computes `net = exit_bid - entry_ask` with no cost term, and the 9C
gate judges strategies on those numbers.

So **the gate could approve a strategy for paper trading whose entire measured edge was smaller
than the fee the engine would then charge it.** The two halves of the system were doing different
arithmetic. Everything in the edge report is computed after fees, using the same cost model and the
same category classification the engine trades with.

### The null hypothesis is not zero
For any one signal the two directions are mirror images at the same two moments:

```
UP     net = exit_bid  - entry_ask
DOWN   net = entry_bid - exit_ask
sum        = (exit_bid - exit_ask) + (entry_bid - entry_ask)
           = -(entry_spread + exit_spread)
```

A position taken with **no information at all** therefore loses *half the two spreads, every time*.
That is the cost of participating, and it is the bar a strategy must clear before it can be said to
know anything. Testing `net > 0` conflates two different questions. This separates them:

| Test | Question |
|---|---|
| `net_after_fees > 0` | **Does this make money?** (the commercial bar) |
| `net_after_fees > coin_flip` | **Does this know anything?** (the information bar) |

`edge_vs_coinflip` is not a restatement of `net` — the spread varies signal to signal, so the two
tests rank strategies differently and can disagree. That disagreement is the useful part.

### Six verdicts
| Verdict | Meaning |
|---|---|
| `INSUFFICIENT_DATA` | fewer than 30 scored signals; says how many more are needed |
| `MAKES_MONEY` | after fees, the whole interval is above zero |
| `KNOWS_SOMETHING_BUT_COSTS_EAT_IT` | beats a coin flip, loses money — **real information, eaten by costs** |
| `WORSE_THAN_A_COIN_FLIP` | reliably worse than random: inverted information, not absent information |
| `NO_EDGE` | cannot beat a coin flip on the same signals |
| `NEGATIVE_AFTER_COSTS` | loses money, and the coin-flip comparison cannot rule out an edge either |

Two of these are new ways of being wrong, and both are actionable in a way "it lost money" is not:

- **`KNOWS_SOMETHING_BUT_COSTS_EAT_IT`** means the predictions are real and the costs are the
  problem. Fixable: trade tighter spreads, cheaper fee categories (0.04 politics rather than 0.07
  crypto), or longer horizons so the per-trade cost is amortised over a larger move.
- **`WORSE_THAN_A_COIN_FLIP`** means the mirror of those same signals made money. The report says
  how much. It is deliberately worded as *"a lead to test, not a strategy to run"*: the inverse is
  a different strategy and needs its own forward record before anything may depend on it.

The information bar is judged **before** the commercial one, because a strategy that cannot beat a
coin flip has nothing, and reporting only that it lost money would name the symptom over the cause.

### Worked example from the tests
A signal bought at 0.52 and sold at 0.55, in a market with 4c spreads on both sides, category
`other` (0.05):

```
gross net (what 7C reports)   +0.0300
round-trip fee                 0.0249     0.05 x (0.52x0.48 + 0.55x0.45)
net after fees                +0.0051     <- what the engine actually books
coin flip baseline            -0.0400     half the two spreads
edge vs coin flip             +0.0700
```

A 3c gross gain is half a cent of real money. The gate was reading the 3c.

### Split by fee category
The same prediction is profitable in `geopolitics` (0 fee) and losing in `crypto` (0.07) — the
report breaks every strategy down by category for exactly this reason. A strategy is not one thing
across a venue whose access cost varies by a factor of two.

### Version integrity
A strategy whose scored signals span more than one `strategy_version` is **not one strategy**, and
pooling them produces a confidence interval about nothing. The report flags `mixed_versions` and
the executor refuses to trade it. This matters most for any future model-backed strategy, where a
version bump silently changes the thing being measured.

At `GET /api/edge`.

---

## 16B — The Executor

### The gap
There were two authorities with nothing between them:

```
the 9C gate         may this strategy EVER trade?      slow, once, on 100 signals over 7+ days
[ MISSING ]         may it trade NOW, at THIS size?
the Risk Governor   how much, given the portfolio?     per order, on exposure and drawdown
```

A strategy could pass the gate in October, decay through November, and keep trading on a promotion
it had long stopped deserving — because **the gate is a promotion event, not a continuous check**,
and the Governor judges the portfolio rather than the strategy. Separately, each paper source
carried its own `enabled` flag and its own rules, so there was no single place to turn one off or to
find out why nothing was trading.

### Three authorities, three questions
The Executor is the missing middle, and like every other authority in FRIDAY **it can only say no.**
It originates nothing, never raises a stake, never overrides a gate refusal, and holds no opinion
about any market. Everything it knows comes from the gate and from FRIDAY's own scored record.

Its checks, in order, failing closed at every step:

1. **The master switch.** `JARVIS_EXECUTE=false` by default. The platform can be fully built, fully
   wired, and still not trading.
2. **The per-strategy allow list**, when set. How one suspect strategy is stopped without stopping
   the platform.
3. **The gate.** `APPROVED_FOR_PAPER` trades at full size; `FORWARD_TEST` is capped at
   `JARVIS_EXECUTE_FORWARD_STAKE` — a strategy still gathering evidence should not be able to lose
   much while it gathers it. Anything else, or no gate at all, trades nothing.
4. **Mixed versions.** Refused, per 16A above.
5. **The live edge, after fees.** If the recent window puts the whole interval below zero, the
   strategy is losing money *now*, whatever the gate decided *then*. Refused with:
   `"The gate approved it; the record no longer does."` **This is the decay check the system did
   not have.** Below `min_scored_to_judge` (30) there is nothing to judge, and silence is not a
   refusal — otherwise no new strategy could ever accumulate the record the gate needs.
6. **The measured capacity ceiling.** A stake above `capacity_fraction` of the largest size 14B
   showed the edge surviving is **cut, not refused**: the trade is fine, the size is not.

All three paper sources — `SignalTrader` (13C), `PairTrader` (15) and `BollingerResting` (12B) — now
ask the Executor before proposing anything to the Governor, and take `verdict.max_stake` as their
size. Caps compose and the smallest wins.

### "Why is nothing trading?"
That question previously required reading four endpoints and inferring. `GET /api/paper` →
`execution` (and `/api/edge` → `execution`) now answers it directly: the master switch, the allow
list, what the gate allows, a full per-strategy verdict with every check and its reason, the live
edge verdicts, and a tally of recent decisions.

### Settings
```
JARVIS_EXECUTE=false                  # nothing opens a position until this is true
JARVIS_EXECUTE_STRATEGIES=            # empty = every strategy the gate allows
JARVIS_EXECUTE_FORWARD_STAKE=10       # a strategy still gathering evidence trades small
JARVIS_EXECUTE_CAPACITY_FRACTION=0.5  # fraction of the measured ceiling
JARVIS_EDGE_WINDOW_DAYS=30            # the window the live edge is measured over
```

---

## Verification
50 new tests (865 → 915). Nineteen mutations applied, each caught by specific tests:

| Reverted behaviour | Tests that fail |
|---|---|
| Mirror is the same direction, not the opposite | 9 |
| Coin-flip baseline forced to zero | 7 |
| Gate ignored | 6 |
| A raising executor: stake not bounded by the request | 5 |
| Fees dropped from the net | 4 |
| Decay check removed | 4 |
| Information bar judged after the commercial one | 2 |
| Forward-test cap removed | 2 |
| DOWN priced as if it bought this token | 1 |
| Unknown category charged nothing | 1 |
| A positive mean is enough: interval ignored | 1 |
| Thin samples given a real verdict | 1 |
| Master switch ignored | 1 |
| Allow list ignored | 1 |
| Decay check fires on too-small samples | 1 |
| Mixed-version check removed | 1 |
| Capacity ceiling ignored | 1 |
| A provider that raises becomes permission | 1 |
| Cache never expires | 1 |

The edge report's verdict ordering was changed *because* of a mutation run: a strategy at exactly
the coin flip was being reported as `NEGATIVE_AFTER_COSTS`, which is true but names the symptom
instead of the cause. That reorder is what surfaced `WORSE_THAN_A_COIN_FLIP` as a distinct and
useful third state.

## What this does not do
- It does not make any strategy profitable. It measures, and it withholds permission.
- It is not a backtest. The 9B walk-forward test and the 9C gate remain the authority on whether a
  strategy may trade at all; the Executor can only decline what the gate has already allowed.
- The edge report reads FRIDAY's own scored record. With no scored signals it reports nothing, and
  reports that it is reporting nothing.

## Still paper only
LIVE remains hard-rejected in the Risk Governor. The gate cannot approve live trading. No wallet
key, no signer and no order submission exists anywhere in the codebase.
