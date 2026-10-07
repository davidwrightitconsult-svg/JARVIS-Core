# Phase 6 — Market Data Engine: Report

Status: **6A signed off** · **6B signed off** · **6C signed off** — **Phase 6 complete**
Market data **6C.1** · Store **6C.1** · Market context **6C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_6_MARKET_DATA.md`

## 6A — Correct market data
| Item | Detail |
|---|---|
| `friday/market/models.py` | `Market`/`Outcome`/`Book` parsing (string-encoded lists, tolerant of missing fields); best-first re-sort; mid/spread; USD depth ±2¢; quality flags (hard: NO_BOOK, EMPTY_BOOK, ONE_SIDED, CROSSED, PLACEHOLDER; soft: WIDE_SPREAD, STALE, GAMMA_MISMATCH, PRICE_SUM); snapshot rows keep the legacy keys (title/price/bid/ask/spread/liquidity) |
| `friday/market/client.py` | `PolymarketData`: Gamma `/markets` (active, closed=false, by volume24hr), CLOB `POST /books` in batches of 20 with a `GET /book` fallback, `SnapshotReport` (counts, usable rows, errors); `snapshots(limit)` counts rows |
| App | `friday/app/polymarket.py` is now a thin subclass of the engine; `/api/scan` ignores unusable rows; `GET /api/market/report` |
| Phase 0 finding closed | `bids[0]`/`asks[0]` were the WORST prices, because Polymarket lists levels worst-first. Fixed and tested with realistic fixtures. |
| Tests | `test_market_data.py`: 16 tests |
| Validation | Live read-only contract probe: Gamma field presence, book ordering, full snapshot quality statistics |

### 6A live results (jarvis020, 2026-09-27)
10 of 10 live order books had a first bid that was *not* the best bid, which confirms the Phase 0 client priced every row wrongly. All 11 Gamma fields were present; `POST /books` works, with 50 of 50 books and 0 errors; 46 of 50 rows are usable (4 correctly flagged `ONE_SIDED`).

## 6B — Market data store
| Item | Detail |
|---|---|
| `friday/market/store.py` | `MarketStore` (6B.1): runs, markets, outcomes, snapshots, and top-5 book levels; history, levels, markets, stats; retention of 365 days for snapshots and 30 days for levels, pruned hourly; refused inside Git |
| Capture | The engine records every snapshot (Hermes `MARKET_SCAN` and `/api/scan`). Book levels stay out of job results, and a store failure is reported without breaking the scan. |
| Coverage | Market scan default of 100 rows (~50 markets) every 15 min |
| App / CLI | `/api/market/stats`, `/markets`, `/history`, `/levels`; `python -m friday.market …` |
| Tests | `test_market_store.py`: 10 tests (26 including 6A) |

### 6B live results (jarvis020, 2026-09-27)
Two live runs went into a temporary store: 50 markets, 200 snapshots, 1,626 book levels, 72% usable. Flags were ONE_SIDED 56, GAMMA_MISMATCH 30 and WIDE_SPREAD 12. The service's real store recorded its first run at 74% usable. The sample market ("Chiefs vs. Dolphins", bid 0.999, no asks) was a finished game still open while awaiting resolution. It was correctly unusable, but it occupied a scan slot, which motivated the 6C filter. The depth ratio (0.389 in validation, 0.046 on the service) was dominated by one-sided books, so 6C computes it over fully usable markets only.

## 6C — Clean universe, export, market data for Claude
| Item | Detail |
|---|---|
| Settled filter | `settlement_reason` (NEAR_CERTAIN ≥ 0.98 / ≤ 0.02, PAST_END > 24 h); 2× Gamma over-fetch with page-by-page book fetching; excluded markets counted and named per run; configurable; new soft flag `PAST_END` |
| Store 6C.1 | In-place migration of 6B stores; excluded counts per run; depth check over usable markets; `export_rows`/`to_csv`; `find_markets`; `market_summary` |
| Claude context | `MarketContext` (MEASURED_DATA, no authority, ≤ 3 markets); analysis jobs declare the existing `READ_MARKET_DATA`; `market_sources` in results and notes |
| App / CLI | `GET /api/market/export`, `GET /api/market/context`, `POST /api/market/analyse`; stats include the filter and context status; CLI `export`, `context` |
| Tests | `test_market_context.py`: 21 tests (47 market tests in total; 438 in the full regression) |

### 6C live results (jarvis020, 2026-09-27) and fix
The filter, export and context all passed on live data: 97% usable after the filter, a depth median of 0.325 over usable markets, and the context found "Vikings vs. Buccaneers". Two issues were fixed:
1. 65 of the top 100 markets were finished Sunday games, so only 35 of 50 slots were filled. The scan now pages further down Gamma's ranking, up to 600 candidates.
2. The live Claude market analysis was cut off at 3,000 output tokens and became a NO_TRADE fallback. Market analyses now request a compact reply, and any truncated analysis is retried once, briefly.

After the fix (jarvis020): 58 decided markets were excluded and all 50 slots were filled. The live Claude analysis of "Rams vs. Broncos" succeeded without truncation (1,557 output tokens, NO_TRADE, citing the recorded 1c spreads and depth). The depth check reads 0.79 on the temporary store and 0.68–1.18 on the real one, which closes the 6A units question. About 6 markets per scan had no order book at all (`NO_BOOK`); 7A removes them from the scan.
