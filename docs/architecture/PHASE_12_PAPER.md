# Phase 12 — Paper Trading

Paper money only. The engine opens and closes positions in the paper ledger at prices FRIDAY itself recorded, under the Risk Governor. It has no network path and cannot place an order anywhere. Live trading stays locked until the Phase 17 audit and the owner's decision.

| Sub-phase | Delivers |
|---|---|
| **12A** | The engine core: entries at the recorded ask, exits at the recorded bid, the paper record with real spreads |
| **12B** | Resting paper orders, and the first forward test that feeds the engine: the Bollinger fade placed as resting orders |
| **12C** | The owner's promotion rule: a forward test that succeeded becomes approved for paper only by the owner's word, and is demoted if its record turns; the paper report; the Phase 12 close |

## Why Phase 12 follows a failed Phase 11

All eleven book rules failed on five months of hourly history (see `PHASE_11_REPORT.md`). Nothing from the books is approved. The engine is still needed: it is the only way to test ideas a backtest cannot (forecasts made today; orders that rest and earn the spread), and it measures the real cost of trading, which Phase 11 could only assume.

## 12A contract

**An order** (`friday.paper.Order`) names a strategy, a market, the token to buy, the stake wanted, a stop distance (1R), an optional target distance and a holding time. To bet against an outcome, the order names the opposite outcome's token.

**Entry** (`PaperEngine.submit`)

| Check | Refused when |
|---|---|
| Quote | FRIDAY has no ask for the token recorded in the last 15 minutes |
| Price | The ask is outside 5 to 95 cents |
| Spread | There is no bid, or ask minus bid is more than 5 cents |
| Duplicate | The same strategy already holds that outcome |
| Risk Governor | The gate does not allow the strategy, a halt or the kill switch is on, or there is no room. The governor may also cut the stake |

A passing order opens at the recorded ask, through `PaperLedger.open_checked`, for the governor's stake. Every refusal is recorded with its reason.

**Exit** (`PaperEngine.tick`, after every stored market scan)

| Event | Fill |
|---|---|
| The market resolved | The token's payoff, 1 or 0 |
| The bid is at or below entry minus the stop | The bid |
| The bid is at or above entry plus the target | The target, never better |
| The holding time has passed | The bid |
| No bid in the last 60 minutes and no resolution | Nothing. The position waits; it is never closed at an invented price. The engine asks the venue (read-only) whether the market has resolved |

Exits never ask the governor. A position can always be closed, kill switch or not.

**Record.** Each closed trade carries its entry and exit spreads and its result in R. `report()` gives, per strategy: trades, wins, profit, average result per share and in R, average entry spread, exit reasons.

**Interfaces.** `GET /api/paper` (read-only). The dashboard carries the paper record. There is no route that submits an order: orders come only from strategy sources inside FRIDAY (12B).

**Settings.** `FRIDAY_PAPER_MAX_QUOTE_AGE_MINUTES` (15), `FRIDAY_PAPER_MAX_SPREAD` (0.05).

**Limits**
- Nothing feeds the engine yet. Until 12B it opens no positions.
- A fill at the recorded ask assumes the size was available at that price. Stakes are small ($20 on a $1,000 bankroll), but depth is not checked yet.
- Quotes arrive at the pace of the market scan, so a stop is seen at the next scan, not the instant it is crossed.
- The stop is measured from the entry ask but triggered by the bid, so a position starts one spread towards its stop. With a 2 cent spread, a 3 cent stop leaves 1 cent of room. Strategy sources in 12B must set stops with the spread in mind.
- A market that stops quoting and never resolves leaves a position open. It is shown as waiting for a price.

## 12B contract

**Resting orders** (`PaperEngine.rest`). A paper buy that waits at a price below the ask.

| Rule | Detail |
|---|---|
| Placement | Needs a fresh two-sided quote, a spread within the limit, a price in the 5 to 95 cent band and below the ask, and a governor that would not reject it now. One waiting order per strategy and outcome |
| Fill | Only when a quote recorded after placement has an ask strictly below the order's price: the market traded through it. Touching the price is not a fill, because FRIDAY cannot know its place in the queue |
| Fill price | The order's price. The position's stop and target are measured from it |
| Governor | Asked again at the fill, through `open_checked`. It may cut the stake or refuse; a refused fill is recorded |
| Expiry | After its waiting time (60 minutes for the forward test) |

This fill rule is pessimistic on purpose. A resting buy fills when the price is falling, so the position usually starts under water. The paper record includes that.

**The forward test: BOLLINGER_RESTING** (`friday/paper/sources.py`), declared in full before its first order.

| Item | Fixed value |
|---|---|
| Signal | The book's Bollinger Bands rule, unchanged: 20 hourly bars, 2 standard deviations. The previous hour closed outside a band and the last hour closed back inside |
| Prices | FRIDAY's own recorded mid-prices, one per completed hour, for markets with two usable outcomes and a price in at least 18 of the last 24 hours |
| Direction | Below the lower band: buy that outcome. Above the upper band: buy the opposite outcome |
| Entry | A resting buy at the recorded bid, waiting up to 60 minutes |
| Target | The 20-bar average at the signal, if at least 1 cent away |
| Stop | 3 cents plus the spread at the signal, at most 8 cents |
| Time limit | 48 hours |
| Stake asked | $20; the Risk Governor sizes it |
| Success | At least 100 closed trades entered on at least 14 days, with the 95% interval of profit per share (clustered by day) above zero |
| Failure | The same count with the interval entirely below zero |
| Loss limit | The test stops for good once it has lost $50 of paper money |

**Why this is a forward test and not an approved strategy.** In 11C this signal made 0.29 cents a share before costs and lost 1.71 after. The idea that earning the spread rescues it was formed after seeing that result, so the backtest is not evidence for it. Its paper record is the only evidence, and the rule above was written before the first order.

**Gate** (12B.1). `build_gate` accepts declared forward tests. One is listed as `FORWARD_TEST` while running, `NOT_APPROVED` once it has failed or used its loss limit. A forward test cannot list itself as approved; promotion to `APPROVED_FOR_PAPER` on success is 12C's job and the owner's sign-off. Switching it off (`FRIDAY_PAPER_BOLLINGER=false`) removes it from the gate, and the governor then refuses it.

**Interfaces.** `GET /api/paper` now carries waiting orders and each forward test's standing. Still read-only.

**Limits**
- Fills are inferred from quotes taken every 15 minutes. A real resting order could fill and reverse between scans; the record cannot see that.
- Depth is not checked. Stakes are about $20.
- The hourly price is FRIDAY's recorded mid, not the venue's last trade, so it is not the same series the 11C backtest used.
- One hypothesis is under test. Each further one tried on the same data raises the chance of a lucky pass and must be counted.
- Research forecasts are not connected. See 12C.

## 12C contract

**From forward test to approved.** A forward test's own rule can say it SUCCEEDED. That approves nothing.

| Step | Rule |
|---|---|
| Promotion | The owner runs `python -m friday.paper promote --confirm PROMOTE` on the server. It is refused unless the test's standing is SUCCEEDED and the word is exact. What it was promoted on (trades, days, profit, interval) is stored |
| Gate | 12C.1 lists a promoted forward test as `APPROVED_FOR_PAPER`. An entry that merely claims approval, without the owner's recorded promotion, is dropped |
| After promotion | The $50 test loss limit no longer applies. Every Risk Governor limit still does |
| Demotion | The strategy is re-judged on its whole paper record each time. If the 95% interval of profit per share falls entirely below zero, the approval is revoked automatically and it stops. It does not come back by itself |
| Revoke | `python -m friday.paper revoke` withdraws the approval at any time |

There is no API route to promote. Approval is for paper trading only; live trading is Phase 17's audit and the owner's decision.

**Paper report.** `python -m friday.paper report | trades`, and `bash scripts/paper_report.sh` for the record plus what the running service last did.

**Research forecasts: deferred to Phase 13, with the reason.** The gate has listed RESEARCH_FORECAST as a forward test since 9C, but nothing produces forecasts. Building that means changing Claude's response contract to carry a probability, scheduling paid Claude calls for every market followed, and giving Claude current information to forecast from; without fresh information there is no reason for a forecast to beat the market price. That is a design of its own, with a running cost, and belongs with the learning loop in Phase 13 rather than at the tail of the paper engine. Until then RESEARCH_FORECAST stays listed and nothing trades under it.

**Limits**
- A promoted strategy trades paper money with real recorded prices. It is still not evidence that live trading would work: depth, queue position and the effect of FRIDAY's own orders are not measured.
- The demotion rule needs a clear negative. A strategy that drifts around zero stays approved and earns nothing; the owner can revoke it.
- One forward test exists. Its record will take weeks to reach 100 trades.
