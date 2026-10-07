# Phase 9 — Backtesting & Validation Lab: Report

Status: **Phase 9 complete** (9A, 9B, 9C signed off)
Backfill **9B.2** · Segments **9B.2** · Fair value **9B.1** · Backtest **9B.1** · Robustness **9B.1** · Gate **9C.1** · Inconsistency scan **9C.1** · Market context **9B.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_9_LAB.md`

## 9A — Walk-forward backtest of the calibration edge
| Item | Detail |
|---|---|
| `friday/lab/backtest.py` | Loads in-scope two-outcome resolved markets; chronological folds with a horizon embargo; the live rule at an assumed ask; held-to-resolution result; per-bet and day-clustered intervals; verdicts; splits by market type, segment, bought price and fold; in-sample comparison; cost sensitivity |
| `friday/market/segments.py` | `category_of`: market type from question wording |
| `friday/quant/fair.py` | `shrunk_fair` extracted so the live model and the backtest share one arithmetic |
| App / CLI | `GET /api/lab/backtest` (cached 10 minutes); `python -m friday.lab backtest` |
| Tests | `test_lab_backtest.py`: 13 tests (564 in the full regression) |

### 9A live results (jarvis020, 2026-10-02)
The dataset held 1,575 in-scope two-outcome markets from October 2025 to October 2026, 724 of them inside the 3¢–97¢ band.

At 24h the rule made 60 out-of-sample bets over 33 days: 98% won, +11.37¢ net per share (clustered 95% CI +8.01 to +14.74), PASSES_OUT_OF_SAMPLE. All mechanics checks passed.

The result was judged untrustworthy:
- the points are anchored on the actual resolution, which leaks the future for early-resolved markets;
- the rule made no bets at 72h and was INCONCLUSIVE at 168h (76 bets, 60% won);
- every sampled bet came from one or two price bands;
- asks were assumed.

## 9B — End-anchored calibration and robustness
| Item | Detail |
|---|---|
| `friday/market/backfill.py` 9B.1 | `reanchor()`: prices at 24/72/168/336/720h before the scheduled end, only while the market was open; runs after every crawl; `start_reanchor_background` |
| `friday/market/store.py` | `calib_points_end`, `calib_markets.end_anchor`, `end_anchor_pending`, `end_anchor_stats`, `quote_near`, `snapshot_span`; `calibration_points(anchor=)` |
| `friday/quant/fair.py` 9B.1 | End-anchored tables; five horizons, nearest by ratio; `OUT_OF_CALIBRATED_RANGE` outside 6h–45 days |
| `friday/lab/backtest.py` 9B.1 | `anchor` setting (default end); decision-time folds; recorded asks |
| `friday/lab/robustness.py` | Horizons, the leak, thresholds, real asks, `robust` and a conclusion |
| App / CLI | `POST /api/market/calibration/reanchor`, `GET /api/lab/robustness`, `anchor=` on the backtest; `python -m friday.market reanchor`, `python -m friday.lab robustness`; `scripts/reanchor_and_report.sh` |
| Tests | `test_lab_robustness.py`: 13 tests (579 in the full regression) |

### 9B live results (jarvis020, 2026-10-02) and the 9B.2 rebuild
The 9B block validated except for one check: the 14- and 30-day horizons had no data.

Re-anchoring all 1,962 markets showed why the dataset could not support any conclusion:
- 1,470 markets had resolved more than 30 days before their scheduled end, a consequence of 8A's end-date ordering;
- 199 had end-anchored prices (191 at 24h, 128 at 72h, 57 at 168h, 0 at 336h, 8 at 720h), all from 23 September to 2 October;
- every horizon was INSUFFICIENT_DATA, and the conclusion was NOT SUPPORTED;
- the live model reported no edges (45 INSUFFICIENT_CALIBRATION, 36 OUT_OF_CALIBRATED_RANGE).

**The Phase 8 favourite–longshot finding is withdrawn.** It was measured on a sample dominated by early resolutions.

A diagnostic then showed that Polymarket refuses 30-day price ranges and deep offsets, and that end-date windows with a volume filter work back at least six months.

| 9B.2 item | Detail |
|---|---|
| `friday/market/client.py` | `closed_markets_window` (end-date window, minimum volume) |
| `friday/market/backfill.py` 9B.2 | `run_windows`: 6-hour windows, newest first, 7-day head re-check, saved cursor, end-anchored in the same pass; long horizons by small requests; one-time redo of 9B.1 results; `run_all` uses it |
| `friday/market/segments.py` 9B.2 | `micro` segment, out of scope by default |
| Settings | `FRIDAY_CALIBRATION_MIN_VOLUME` default 10,000; `FRIDAY_CALIBRATION_PACE_SECONDS` |
| App / CLI | `days=` on the backfill endpoint; `python -m friday.market rebuild`; `scripts/rebuild_and_report.sh` replaces `reanchor_and_report.sh` |
| Tests | `test_history_crawl.py`: 9 tests (594 in the full regression) |

### 9B.2 results on the rebuilt dataset (jarvis020, 2026-10-03)
The rebuild reached 30 June 2026: 7,200 markets with end-anchored prices, 6,617 two-outcome in-scope markets at 24h, 2,642 of them inside the 3¢–97¢ band.

| Horizon | Markets | In band | Out-of-sample bets | Verdict |
|---|---|---|---|---|
| 24h | 6,617 | 2,642 | 1 (lost) | INSUFFICIENT_DATA |
| 72h | 6,198 | 3,116 | 0 | INSUFFICIENT_DATA |
| 168h | 3,358 | 1,821 | 0 | INSUFFICIENT_DATA |
| 336h | 3,043 | 1,771 | 0 | INSUFFICIENT_DATA |
| 720h | 2,386 | 1,615 | 0 | INSUFFICIENT_DATA |

- At zero assumed cost the 24h rule made 135 bets for −0.57¢ per share (clustered 95% CI −8.54 to +7.41).
- On the rebuilt data the resolution-anchored test gave −3.10¢ on 89 bets at 24h, so 9A's +11.37¢ was an artefact of the old sample.
- On the 11 markets FRIDAY recorded itself, the recorded ask sat a median 0.5¢ above the history price (90% within 2.1¢).
- The live model reported 49 NO_EDGE and 48 OUT_OF_CALIBRATED_RANGE.

**Conclusion: NOT SUPPORTED.** Between 3¢ and 97¢, Polymarket's prices on these markets are close to well calibrated. The calibration edge is not a source of profit.

## 9C — The validation gate
| Item | Detail |
|---|---|
| `friday/lab/gate.py` | `build_gate`: one status per strategy (APPROVED_FOR_PAPER / FORWARD_TEST / NOT_APPROVED) from the robustness report, the live scorecard and the inconsistency scan; `live_approved` always empty; `allowed()` |
| `friday/lab/inconsistency.py` | Recorded moments where both outcomes cost under $1 together: observations, episodes, size, duration, depth, verdict |
| `friday/market/store.py` | `pair_sums` |
| App / CLI | `GET /api/lab/gate`, `GET /api/lab/inconsistencies`; `python -m friday.lab gate`, `python -m friday.lab inconsistencies` |
| Tests | `test_lab_gate.py`: 10 tests (604 in the full regression) |

### 9C live results (jarvis020, 2026-10-03)
All checks passed. The gate approved nothing.

| Strategy | Status | Evidence |
|---|---|---|
| CALIBRATION_EDGE | NOT_APPROVED | 1 bet in 2,642 in-band markets at 24h; none at longer horizons |
| BOOK_IMBALANCE | NOT_APPROVED | 2,938 scored over 6 days; net −0.82¢ (95% −1.02 to −0.62) |
| MEAN_REVERSION | NOT_APPROVED | 437 scored; net −0.04¢ (−3.03 to +2.95) |
| MOMENTUM | NOT_APPROVED | 225 scored; net −1.60¢ (−5.08 to +1.87) |
| LONGSHOT_BIAS | NOT_APPROVED | 329 scored over 5 days; net −0.81¢ (−3.11 to +1.48) |
| PRICING_INCONSISTENCY | NOT_APPROVED | 34,867 observations over 583 scans and 209 markets; lowest ask sum 1.001 |
| RESEARCH_FORECAST | FORWARD_TEST | Paper trading is the evidence |

**Phase 9 conclusion.** No strategy testable on recorded data has evidence of an edge.

**Direction set by the owner (2026-10-03).** FRIDAY is a strategy research platform: it reads trading books, turns ideas into testable rules, tests them, and paper-trades what passes before any live commitment. Phases 10–12 are re-scoped accordingly: a market-agnostic Risk Governor (10), a strategy registry built from the books (11), and crypto price data, a general backtester and the paper engine (12). Polymarket remains as a side test for research forecasts.
