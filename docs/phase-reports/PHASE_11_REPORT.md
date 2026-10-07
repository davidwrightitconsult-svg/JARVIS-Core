# Phase 11 — Book Strategies: Report

Status: **complete** · 11A, 11B, 11C signed off (11C on 2026-10-06)
Strategy registry **11B.1** · Backtester **11B.1** · Hourly bars **11C.1** · Test run **11C.1** · Gate **11C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_11_STRATEGIES.md`

## 11A — Strategy registry
| Item | Detail |
|---|---|
| `friday/strategies/indicators.py` | Averages, exponential averages, standard deviation, Bollinger bands, Wilder's RSI, MACD, swing pivots; closes only; rounded so binary dust never decides a crossing |
| `friday/strategies/registry.py` | Twelve strategies: 11 signal rules and the Risk Management overlay. Each has its books, source, rule in words, adaptation, fixed settings and exit. `signal`, `signals`, `should_exit`, `books_in_library`, `describe` |
| App | `GET /api/strategies`; the dashboard's validation board and strategy library show the twelve as registered, not yet tested, with their source |
| CLI | `python -m friday.strategies list | books` |
| Tests | `test_strategy_registry.py`: 29 tests (675 in the full regression). No rule can see the future; long and short mirror exactly; settings are fingerprinted; each rule fires on the pattern it names; Wilder's worked RSI example reproduces |
| Validation | `scripts/validate_phase_11a.sh`: the registry printed in full, the library check, a lookahead drill, server smoke, regression |

Nothing was tested for profit in 11A. Zero strategies are approved.

### 11A live results (jarvis020, 2026-10-03 and 2026-10-05)
All checks passed; 675 tests. The lookahead drill passed for all eleven rules. The library then held one of the six books. On 2026-10-05 the owner added nine more: Murphy, Douglas, Tharp, Brooks (*Trends*), Wilder, Chan's *Quantitative Trading* and three others. Two scanned PDFs were rejected (no text layer). Passarelli is not in the library. The check wrongly reported Chan's *Algorithmic Trading* as present and missed Tharp (its PDF title is a hash); both are fixed in 11B.

## 11B — Price history, backtester, source check
| Item | Detail |
|---|---|
| `friday/market/bars.py` | `BarBuilder`: hourly series for resolved markets, week-sized requests, forward-filled gaps, OK / THIN / EMPTY / OUT_OF_SCOPE, never fetched twice |
| Store | `bar_markets`, `bars`; `bars_pending`, `record_bars`, `bar_series`, `bar_markets`, `bar_stats` |
| `friday/lab/bookbt.py` | `trades_for`, `summarise` (day-clustered intervals), `split` (30% holdout by date), `verdict` (fixed in advance), `run_book_backtest`, `render` |
| Registry | Book matching on file name; Chan's two books told apart; Wilder added; `check_sources`; `ResearchLibrary.locate` returns locations only |
| App | `GET /api/lab/bars`; `/api/strategies` carries the source check; dashboard tiles show it |
| CLI | `python -m friday.lab bars | bars-status`; `python -m friday.strategies sources`; `scripts/build_bars_and_report.sh` |
| Tests | `test_book_backtest.py`: 12 tests (687 in the full regression). On random prices no rule profits and none passes |

No strategy was run on real prices in 11B.

### 11B live results (jarvis020, 2026-10-05)
All checks passed; 687 tests.

| Item | Result |
|---|---|
| Books recognised | Murphy, Wilder, Brooks (*Trends* and *Reversals*), Chan *Quantitative Trading*, Tharp, Douglas. Not held: Chan *Algorithmic Trading*, Passarelli |
| Source check | All twelve rules FOUND_IN_SOURCE. Thinnest: Bollinger Bands in Chan (2 passages); that rule rests mainly on Murphy |
| Hourly history | 11,183 of 11,185 resolved markets fetched; 9,669 usable (4,315,907 bars, 99.4% of hours with a reported price); 1,514 thin |
| Period | Markets resolved 2026-05-01 to 2026-10-05 |
| Split when the build finished | 6,768 development, 2,901 holdout (from 2026-08-31) |
| Simulator on random prices | Every rule lost about the 2 cents of round-trip cost; none passed |

A dropped SSH connection left one builder running; a rerun started a second and it stopped on a duplicate market. No data was damaged. Fixed in 11C.

## 11C — The test
| Item | Detail |
|---|---|
| `friday/lab/bookrun.py` | `freeze_boundary`, `parts`, mergeable totals (`aggregate`, `merge`, `summary`), `run_book_test` (development, then holdout for passes only; several processes), `stored_results`, `gate_entries`, `render_test` |
| Gate | 11C.1: one entry per tested book rule; approval needs both stages and today's settings |
| App | `GET /api/lab/book`; the dashboard shows each tested rule with its trades, days and result |
| CLI | `python -m friday.lab book-test | book-results`; `scripts/run_book_test_and_report.sh` (detached, re-attachable) |
| Fix | A series is written once; two builders at once no longer collide |
| Tests | `test_book_run.py`: 8 tests (695 in the full regression) |

### 11C live results (jarvis020, 2026-10-06)
Holdout boundary frozen at 2026-08-31. Development: 6,768 markets. Cost 1 cent a side. Settings fingerprint 0127a702.

| Rule | Trades | Markets | Days | Net ¢/share | 99.5% interval | Before costs | Verdict |
|---|---|---|---|---|---|---|---|
| Support & Resistance | 14,264 | 3,249 | 141 | −3.49 | −3.73 to −3.26 | −1.49 | FAILED |
| RSI | 25,336 | 4,212 | 148 | −3.07 | −3.29 to −2.84 | −1.07 | FAILED |
| Bollinger Bands | 50,825 | 4,421 | 149 | −1.71 | −1.93 to −1.50 | +0.29 | FAILED |
| MACD | 70,179 | 4,297 | 151 | −2.70 | −2.91 to −2.49 | −0.70 | FAILED |
| Price Action | 27,967 | 4,027 | 138 | −3.55 | −3.85 to −3.26 | −1.55 | FAILED |
| Trend Continuation | 7,031 | 2,569 | 137 | −3.58 | −3.92 to −3.24 | −1.58 | FAILED |
| Breakout | 31,363 | 4,235 | 147 | −3.70 | −3.93 to −3.47 | −1.70 | FAILED |
| Pullback | 21,837 | 3,875 | 144 | −3.72 | −3.93 to −3.51 | −1.72 | FAILED |
| Momentum | 37,518 | 4,401 | 148 | −3.33 | −3.65 to −3.01 | −1.33 | FAILED |
| Reversal | 1,833 | 1,015 | 128 | −4.29 | −4.87 to −3.71 | −2.29 | FAILED |
| Moving Averages | 41,773 | 4,195 | 150 | −2.96 | −3.25 to −2.67 | −0.96 | FAILED |

**No rule passed.** Every one loses money after costs, and ten of the eleven lose before costs. The holdout (2,901 markets) was not run and is untouched.

Reading: hourly moves on this venue tend to reverse rather than continue, which punishes the trend-following rules most and leaves the one fading rule (Bollinger Bands) slightly positive before costs, far short of a 2 cent round trip. Prices also jump on news, and a fixed target caps a favourable jump while a stop pays an adverse one in full.

One validation step, the test-run drill on random prices, crashed on the server (Python 3.14 cannot start worker processes from piped code). The real test ran from the proper command and was not affected. Fixed in 12A.

## Phase 11 conclusion
The twelve strategies from the reference image are registered, traced to the owner's own copies of the books, and tested. None is approved for paper trading. The rules were not re-tuned. Phase 12 builds the paper engine and uses it for forward tests that a backtest cannot run.
