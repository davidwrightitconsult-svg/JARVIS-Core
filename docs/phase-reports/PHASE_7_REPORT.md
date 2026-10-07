# Phase 7 — Quantitative Strategy: Report

Status: **7A signed off** · **7B signed off** · **7C signed off** — **Phase 7 complete**
Quant Core **7A.1** · Strategies **7B.1** · Scoring/Scorecard **7C.1** · Store **7C.1** · Market context **7C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_7_QUANT.md`

## 7A — Features and tradable universe
| Item | Detail |
|---|---|
| `friday/quant/features.py` | Per-outcome price, half-spread, depth, top-of-book USD, imbalance, 1h/6h/24h change (None until history covers it), volatility, range, quality; per-market hours to end and ask/bid sums; attention score |
| `friday/quant/universe.py` | `UniverseRules` (depth, spread, volume, price band, freshness; `FRIDAY_QUANT_*`) and `evaluate` with reason codes |
| `friday/quant/engine.py` | `QuantEngine`: latest run → features → eligibility; universe summary with rejection counts |
| Store | Bulk reads: `latest_run`, `latest_rows`, `history_bulk`, `levels_bulk` |
| Scan | `/api/scan` is served from recorded data (a live snapshot only when stale); WATCH = eligible; `edge: None` |
| Scan universe | Markets with no order book at all are excluded (`NO_BOOK`); a fetch failure is never mistaken for this |
| Claude | Market context includes each market's `quant` block |
| App / CLI | `/api/quant/universe`, `/api/quant/features`; `python -m friday.quant …` |
| Tests | `test_quant_core.py`: 11 tests (449 in the full regression) |

### 7A live results (jarvis020, 2026-09-27)
The latest run had 50 markets, 39 of them eligible. Rejections: DEPTH<500 7, PRICE_EXTREME 7, SPREAD>0.05 1. Features were computed in 0.01s. Across all 50 usable binary markets, ask_sum was 1.01: no mispricing, just the 1¢ spread. The scan refresh was served from recorded data in 0.01s and created no new run. Spread and over/under markets showed order books leaning heavily to one side (imbalance about ±0.8), while the head-to-head game market was balanced.

One check failed: "1h change available". The check itself was wrong. On game night the top-volume universe churns, so many eligible markets were new and correctly showed no 1h change. 7B adds the sticky watchlist and replaces the check with the correct one: every market observed for more than an hour must have a 1h change.

## 7B — Strategy library, signals, watchlist
| Item | Detail |
|---|---|
| `friday/quant/strategies.py` | BOOK_IMBALANCE (1h), MEAN_REVERSION (6h), MOMENTUM (6h), LONGSHOT_BIAS (24h); primary outcome, eligible markets only; every input recorded; `FRIDAY_QUANT_STRATEGIES` |
| `friday/quant/pipeline.py` | Runs after every stored snapshot (observer): features → universe → watchlist → strategies → signals; no re-firing while a horizon is open; preview mode |
| Store 7B.1 | `signals` and `watchlist` tables (in-place migration), `runs.tracked`; signals pruned with history |
| Market client | Sticky watchlist (`tracked_source`) and observers; failures reported, never breaking the scan |
| App / CLI | `/api/quant/strategies`, `/api/quant/signals`, `/api/quant/watchlist`; open signals on scan rows and in Claude's context; `python -m friday.quant strategies/signals/watchlist/run` |
| Tests | `test_quant_strategies.py`: 14 tests (463 in the full regression) |

### 7B live results (jarvis020, 2026-09-27)
The preview on real history found 36 of 50 markets eligible. Seventeen signals would have fired: 9 LONGSHOT_BIAS, 6 BOOK_IMBALANCE and 2 MEAN_REVERSION. The corrected 1h check passed (13 of 13). In the live pipeline, 19 signals were recorded, all 18 repeats were held back on the next run, and the watchlist kept 19 markets observed beyond the top 25. The service recorded 15 signals on its first run.

These early signals raised three questions for 7C:
- Strategies disagree on the same market.
- On 3–5¢ long shots, a 1¢ spread is 20–30% of the price.
- Imbalance may mislead on very cheap books ($712 bid against $13 ask at 3¢).

## 7C — Scoring, resolutions, scorecard
| Item | Detail |
|---|---|
| `friday/quant/scoring.py` | First usable observation after expiry (within a tolerance window); mid move; executable net (spread crossed both ways); cost and return on cost; NO_EXIT_DATA / RESOLVED_BEFORE_EXIT; held-to-resolution net; idempotent; preview mode |
| Resolutions | `market_resolutions` (read-only Gamma `/markets/{id}`; closed AND 0/1 prices only; 30s budget), hourly, via an injected resolver |
| `friday/quant/scorecard.py` | Per-strategy hit rate, net with 95% CI, t-stat, return on cost, price bands, direction, held to resolution; conservative verdicts; Markdown |
| Knowledge | Daily `FRIDAY/Strategies/Scorecard-<day>.md` (new FRIDAY folder, auto-created); NoteType `strategy-scorecard` |
| Claude | Open signals carry `strategy_track_record` (context 7C.1) |
| App / CLI | `/api/quant/scorecard`, `/api/quant/outcomes`, `POST /api/quant/score`, `POST /api/quant/scorecard/note`; `python -m friday.quant score/scorecard/outcomes` |
| Tests | `test_quant_scoring.py`: 12 tests (475 in the full regression) |

### 7C live results (jarvis020, 2026-09-28)
The read-only resolution probe worked against the real Gamma API: 4 recently finished game markets were reported resolved with clean 1/0 payoffs, and open markets were never called resolved. When validation ran, 42 signals were recorded and none were due yet; the first 1h horizons ended a few minutes later. After the restart, 46 open signals were waiting to be scored: 12 BOOK_IMBALANCE, 18 MEAN_REVERSION and 16 LONGSHOT_BIAS. Every verdict is INSUFFICIENT_DATA until a strategy has 30 scored signals.
