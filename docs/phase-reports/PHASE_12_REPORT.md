# Phase 12 — Paper Trading: Report

Status: 12A, 12B signed off · **12C awaiting sign-off**
Paper engine **12C.1** · Sources **12C.1** · Gate **12C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_12_PAPER.md`

## 12A — Engine core
| Item | Detail |
|---|---|
| `friday/paper/engine.py` | `Order`, `PaperSettings`, `PaperEngine`: `submit` (fresh-quote, price, spread, duplicate and governor checks; entry at the recorded ask), `tick` (resolution, stop, target, time; exit at the recorded bid), `prices`, `trades`, `report`. Plans and refusals kept in the risk store |
| App | Engine built after the governor and ledger; `tick` runs after every stored market scan; open positions are valued by the engine; `GET /api/paper`; dashboard section `paper`; health component `paper` |
| Settings | `FRIDAY_PAPER_MAX_QUOTE_AGE_MINUTES`, `FRIDAY_PAPER_MAX_SPREAD` |
| Tests | `test_paper_engine.py`: 12 tests (707 in the full regression) |
| Validation | `scripts/validate_phase_12a.sh`: a day of paper trading on made-up quotes, server smoke, regression |
| Also fixed | The 11C validation drill no longer starts worker processes from piped code, which failed on Python 3.14 |

### 12A live results (jarvis020, 2026-10-06)
All checks passed; 707 tests. Drill (a day of made-up quotes): 27 opened, 69 refused; 24 stops, 1 target, 2 time exits; average entry spread 2.3 cents. The books balanced and no cap was passed. Finding: a position is bought at the ask and stopped on the bid, so a 3 cent stop is too tight when the spread is 2 cents.

## 12B — Resting orders and the first forward test
| Item | Detail |
|---|---|
| Engine | `rest`, `resting`, fill on trade-through only, governor asked again at the fill, expiry; `report` shows placed, filled, expired and refused per strategy |
| `friday/paper/sources.py` | `BollingerResting`: the book's frozen Bollinger rule on FRIDAY's recorded hourly mids; resting buy at the bid; `verdict` (the written success, failure and loss-limit rule); `gate_entry` |
| Gate | 12B.1: declared forward tests listed; they cannot approve themselves |
| App | The source runs after each market scan, after the engine's fills and exits; `FRIDAY_PAPER_BOLLINGER`; the gate cache is cleared at start |
| Scope change | Research forecasts moved to 12C: FRIDAY stores no probability forecasts yet |
| Tests | `test_paper_sources.py`: 10 tests (717 in the full regression) |
| Validation | `scripts/validate_phase_12b.sh`: the declared rule printed, ten days of the forward test on made-up markets, server smoke, regression |

### 12B live results (jarvis020, 2026-10-06)
All checks passed; 717 tests. Drill (ten days of made-up markets): 199 resting orders placed, 29 filled, 170 expired; 28 closed trades (14 stops, 14 targets), −$1.57; standing RUNNING. The forward test went live on paper money at the restart. Its real paper record had not been read back when 12C was built.

## 12C — Promotion rule, paper report, close
| Item | Detail |
|---|---|
| Engine | `paper_promotions`; `promotion`, `record_promotion`, `revoke_promotion` |
| Sources | `promote` (needs SUCCEEDED and the word PROMOTE), `revoke`, standing APPROVED / DEMOTED, automatic demotion when the whole record turns clearly negative |
| Gate | 12C.1: a forward test is `APPROVED_FOR_PAPER` only on the owner's recorded promotion |
| CLI | `python -m friday.paper report | trades | promote | revoke`; `scripts/paper_report.sh` |
| Scope | Research forecasts deferred to Phase 13 (needs a change to Claude's contract, paid calls and fresh information; see the design note) |
| Tests | `test_paper_promotion.py`: 6 tests (723 in the full regression) |
| Validation | `scripts/validate_phase_12c.sh`: promotion drill, forward-test drill, server smoke, regression, and the real paper record so far |

Live results are added after the jarvis020 run.

## What Phase 12 leaves running
- The paper engine, checking positions after every market scan.
- One forward test, BOLLINGER_RESTING, placing resting paper orders under a rule fixed before its first order. It ends in SUCCEEDED, FAILED or STOPPED. Only the owner can promote it.
- Nothing from the books is approved. Live trading is locked.
