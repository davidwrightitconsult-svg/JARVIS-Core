# Phase 11 — Book Strategies

The aim of the platform: read the trading books, turn what they teach into exact rules, test those rules honestly, and paper trade only what passes. Phase 11 is the first three of those steps for the twelve strategies on the owner's reference image.

| Sub-phase | Delivers |
|---|---|
| **11A** | The registry: twelve strategies as exact rules, each with its source and with every setting fixed before any test |
| **11B** | Hourly price history for resolved markets, the backtester, the verdict rule fixed in advance, and a check of each rule against FRIDAY's own copies of the books |
| **11C** | Every rule run once on the hourly history: development first, the untouched holdout only for a rule that passes; verdicts in the validation gate |

## 11A contract

**A bar** is one hour. **A price** is the price of one outcome share, between 0 and 1. **LONG** buys the outcome; **SHORT** buys the opposite outcome.

**No lookahead, by construction.** A rule is a function of the closes up to and including the current bar. It is handed that list and nothing else, capped at the last 200 bars.

**Frozen settings.** Each setting is the standard value from the cited source, or one round number chosen here and written down. A test fingerprints them; changing one is a deliberate, documented decision made before a test run. A rule that fails in 11C is reported as failed. It is not re-tuned until it passes.

**Entries** are refused below 5 cents and above 95 cents, on non-finite or out-of-range prices, and with fewer than 3 bars.

**Exits.** Every signal rule has a stop distance (this is 1R), a time limit in bars, and either a fixed target or its own exit rule. The backtester (11B) and the paper engine (12) execute them.

| Strategy | Source | Fixed settings | Exit |
|---|---|---|---|
| Support & Resistance | Murphy ch. 4 | 120-bar window, 5-bar swings, 2 touches, 1 cent | stop 3c, target 6c, 48 bars |
| RSI | Murphy ch. 10 (Wilder) | 14 bars, 30 and 70 | stop 3c, target 6c, 48 bars |
| Bollinger Bands | Murphy ch. 9; Chan ch. 3 | 20 bars, 2 standard deviations | stop 3c, the 20-bar average, 48 bars |
| MACD | Murphy ch. 10 | 12, 26, 9 | stop 3c, lines cross back, 72 bars |
| Price Action | Brooks, Trends | 3 bars of at least half a cent | stop 3c, target 6c, 24 bars |
| Trend Continuation | Murphy ch. 6 | 5 cents in 24 bars, then 8 bars within 2 cents | stop 3c, target 6c, 48 bars |
| Breakout | Murphy ch. 4; Brooks | 48-bar channel | stop 3c, target 6c, 48 bars |
| Pullback | Brooks; Murphy ch. 9 | 20 and 50-bar averages, 10-bar slope | stop 3c, target 6c, 48 bars |
| Momentum | Chan ch. 6 | 24-bar change of 3 cents | stop 3c, 24 bars |
| Reversal | Murphy ch. 5 | double top or bottom: 1 cent, 10 bars apart, 3 cents deep | stop 3c, target 6c, 72 bars |
| Moving Averages | Murphy ch. 9 | 10 and 30 bars | stop 3c, averages cross back, 72 bars |
| Risk Management | Tharp; Douglas | overlay: fixed 1R stop, results in R, expectancy over many trades, size from the Risk Governor | — |

The full wording of each rule is in `friday/strategies/registry.py` and printed by `python -m friday.strategies list`.

**Interfaces.** `GET /api/strategies`; `python -m friday.strategies list | books`. The dashboard shows the twelve as "Registered, not yet tested".

**Limits, stated plainly**
- The rule wording is FRIDAY's. The ideas are the books'. Chapter references are to where each idea is taught; they are not quotations, and they have not yet been checked against the library's own copies. `python -m friday.strategies books` reports which of the six books the library holds.
- These books teach chart reading on stocks, futures and currencies, with highs, lows and volume. Polymarket publishes one price per bar and no volume per bar. Each rule says how it was adapted. Price Action loses the most: Brooks reads the shape of every bar, and one price per bar has no shape.
- A share price here is bounded by 0 and 1 and ends at exactly one of them. Chart methods were not designed for that. Whether any of them works on this venue is the question 11C answers; the Phase 9 result for simple momentum and mean reversion was negative.
- Passarelli's options book yields no testable rule on a venue without listed options. Douglas's book is discipline, not signals; it informs the Risk Management overlay.
- Registered means written down. Nothing is approved for paper trading by being registered. The Risk Governor refuses every one of these until the validation gate approves it.
- The stop of 3 cents and target of 6 cents are FRIDAY's round numbers, not the books'. They are frozen like everything else.

## 11B contract

**Hourly history** (`friday/market/bars.py`, tables `bar_markets` and `bars`). For each resolved two-outcome market in the calibration set, in scope, FRIDAY reads the 30 days before the outcome became known, in one-week requests, and stores one price per hour for the Yes outcome.

- A bar is the last price the venue reported in that hour. An hour with no report repeats the previous price.
- The series stops when the outcome became known.
- A series needs at least 150 bars and 100 hours with a reported price to be tested. Others are marked THIN and kept out.
- A market is fetched once. `python -m friday.lab bars` builds; `bars-status` and `GET /api/lab/bars` report.

**Development and holdout.** Usable markets are ordered by when their outcome became known. The newest 30% are the holdout. Nothing is run on the holdout until a rule has passed development.

**The simulator** (`friday/lab/bookbt.py`). Every choice is the less flattering one.

| Step | Rule |
|---|---|
| Entry | Decided at the close of a bar, filled at the next bar's price. Refused if that price is outside 5 to 95 cents, or if it is the last bar |
| Cost | Half the spread, paid on entry and again on exit. Default 1 cent a side; results are also shown at 0, 0.5 and 2 cents |
| SHORT | Buys the opposite outcome at (1 − price) plus cost |
| Stop | Checked at each close and filled at that close, so a gap through the stop is paid in full |
| Target | Filled at the target, never better |
| Other exits | The rule's own exit, the time limit, or the end of the series, at that bar's close. Nothing is held to resolution |
| Overlap | One position per market per strategy |

**The verdict rule, fixed before any real test**

| Stage | Requirement |
|---|---|
| Development | At least 200 trades entered on at least 30 different days, and the 99.5% interval for average profit per share after costs entirely above zero. The level is 99.5% because eleven rules are tested together |
| Holdout | Only for a rule that passed development: at least 60 trades on 10 days, and the 95% interval entirely above zero |

Intervals are clustered by entry day, because trades opened on the same day move together. A rule that fails is reported as failed.

**Source check** (`python -m friday.strategies sources`, also in `GET /api/strategies`). For each rule and each book it cites, FRIDAY searches its own copy for the idea and reports how many passages mention it and where. It never prints book text. Results: FOUND_IN_SOURCE, PARTLY_FOUND, NOT_FOUND_IN_SOURCE, or NOT_VERIFIED when the book is not in the library. The dashboard shows the result on each strategy tile.

**Changes to citations in 11B.** The library holds Chan's *Quantitative Trading*, not *Algorithmic Trading*; Bollinger Bands and Momentum now cite the book FRIDAY has. RSI cites Wilder's own book first. Books are matched on file name as well as title. No rule setting changed: the fingerprint is the same.

**Limits**
- A source check finds the idea, not the numbers. Stops, targets and bar counts are FRIDAY's.
- Hourly closes hide what happened inside the hour. Real fills could be better or worse than the close.
- The cost is an assumption until Phase 12 measures real spreads on paper trades.
- Only resolved markets with at least $10,000 traded are in the set, so the results describe that kind of market.
- Each market contributes its last 30 days only.

## 11C contract

**Order of events** (`friday/lab/bookrun.py`)
1. The holdout boundary is frozen as a date, from the usable markets held at that moment, and stored. Markets fetched later fall on one side of it by their own date; the boundary never moves.
2. Every signal rule is run on the development markets only.
3. A rule that passes development is run on the holdout. A rule that fails never touches it.
4. The result is stored with a fingerprint of the rule settings and the cost. Asking again returns the stored result. It is not recomputed.

**Gate.** `build_gate` (now 11C.1) carries one entry per tested book rule. A rule is `APPROVED_FOR_PAPER` only if it passed development and the holdout, and the registry still holds the settings it was tested with. Change a setting afterwards and the approval lapses until it is tested again. Live trading is never approved here.

**Running it.** `bash scripts/run_book_test_and_report.sh` runs the test detached, so a dropped connection does not stop it; rerunning re-attaches or prints the stored result. `python -m friday.lab book-results` and `GET /api/lab/book` read it. The app cannot run or change the test.

**Fixed in 11C.** Two history builders running at once crashed the second on a duplicate market. A series is now written once; a second writer keeps the first one's and carries on.

**Limits**
- One test, one set of settings. A rule that failed here failed with these settings on this venue over these five months. That is not proof the idea is worthless everywhere.
- A rule that passed would still be a backtest pass. Paper trading (Phase 12) is the next test, with real spreads.
- Re-testing a changed rule on the same development data is allowed, but every such attempt must be counted: each extra try makes a lucky pass more likely. The holdout is consulted only by rules that pass development.
