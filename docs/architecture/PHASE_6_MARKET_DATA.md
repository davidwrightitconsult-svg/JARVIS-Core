# Phase 6 — Market Data Engine (v0.6)

## Sub-phases
| Sub-phase | Scope |
|---|---|
| **6A Correct data** | `friday.market`: Gamma market list (active, open, by 24h volume), CLOB books in batches (`POST /books`, falls back to `GET /book`); **best bid = highest bid, best ask = lowest ask** regardless of list order; USD depth within ±2¢ of mid; quality flags; errors reported, never swallowed; replaces the Phase 0 client |
| **6B Storage** | Time-series market database on jarvis020 (snapshots + top-of-book), retention, data-quality statistics |
| **6C History and feeds** | History API/CSV export; real market data as Claude context; the feed Phase 7 (quant) builds on |

## 6A contract
1. **Read-only.** There are no keys, no order, cancel or sign endpoints, and the
   client is only a data reader. Execution remains LOCKED.
2. **Order-book correctness.** Polymarket returns levels worst-first, and the
   order is not guaranteed. FRIDAY never trusts list position: it re-sorts
   best-first and takes max(bid) and min(ask). This was the Phase 0 finding: the
   old client read `bids[0]`/`asks[0]`, which are the worst prices.
3. **Depth** is USD (price × size) within ±2¢ of the mid, not a count of shares.
4. **Quality flags:**
   - Hard flags make a row *unusable*: `NO_BOOK`, `EMPTY_BOOK`, `ONE_SIDED`,
     `CROSSED`, `PLACEHOLDER` (0.01/0.99).
   - Soft flags: `WIDE_SPREAD` (>10¢), `STALE` (>5 min), `GAMMA_MISMATCH`
     (mid more than 5¢ from Gamma's own best bid/ask), and `PRICE_SUM` (binary
     YES and NO mids don't sum to about 1).
5. `/api/scan` (the Phase 0 heuristic, until Phase 7) never marks an unusable row
   WATCH. `/api/market/report` shows the last snapshot's counts and errors.

## 6B contract — market data store
1. `friday/market/store.py` (`MarketStore`) writes to a SQLite file at
   `~/friday/data/market_data.db` (`FRIDAY_MARKET_DB`). It is separate from
   FRIDAY's operational database, and refused inside any Git working tree.
2. **Tables:** `runs` (one per snapshot: counts, errors, duration), `markets`,
   `outcomes`, `snapshots` (one per outcome token per run: bid, ask, mid, spread,
   last trade, USD depth, Gamma liquidity and volume, flags, usable),
   `book_levels` (top N per side, best-first; `FRIDAY_MARKET_BOOK_LEVELS`, default 5).
3. **Capture.** The market-data engine records every snapshot itself, so the
   existing Hermes `MARKET_SCAN` builds history with no new authority. Book
   levels never enter Hermes job results. A store failure is reported in the
   scan's errors and never breaks the scan.
4. **Retention.** Snapshots and runs are kept 365 days and book levels 30 days
   (`FRIDAY_MARKET_RETENTION_DAYS`, `FRIDAY_MARKET_BOOK_RETENTION_DAYS`).
   Pruning runs automatically, at most hourly.
5. **Coverage.** The Hermes market scan default rises from 25 to 100 rows (about
   50 markets) every 15 minutes. That's about 10k snapshot rows a day, and
   roughly 0.5 GB/year at steady state.
6. **API:** `/api/market/stats`, `/api/market/markets`, `/api/market/history`,
   `/api/market/levels`. **CLI:** `python -m friday.market snapshot|stats|markets|history|prune`.
7. **Statistics** include `depth_vs_gamma_liquidity`, the median ratio of FRIDAY's
   ±2¢ USD depth to Polymarket's `liquidityNum`. It's a standing check on the
   depth units questioned in 6A.

## 6C contract — clean universe, export, market data for Claude
1. **Settled-market filter** (`settlement_reason`, market data 6C.1). A market
   leaves the scan universe when it is effectively decided (`NEAR_CERTAIN`: some
   outcome's best bid ≥ 0.98, or best ask ≤ 0.02 — the 6B "Chiefs 0.999 bid, no
   asks" case), or when its end date passed more than 24 h ago (`PAST_END`).
   Placeholder 0.01/0.99 books are *not* "settled"; they stay flagged unusable.
   Gamma is read in pages of 2× the scan size (`offset` paging, up to
   `FRIDAY_MARKET_MAX_CANDIDATES`, default 600), and books are fetched page by
   page, so excluded markets are replaced from further down the volume ranking,
   and no extra requests are made when nothing is excluded. (On Sunday
   2026-09-27, 65 of the top 100 markets were finished games; one 2× page left
   only 35 of 50 slots filled.) Each run reports `candidates`,
   `excluded`, `excluded_reasons` and up to 5 `excluded_examples`, and the store
   keeps the counts. Settings: `FRIDAY_MARKET_EXCLUDE_SETTLED`,
   `FRIDAY_MARKET_NEAR_CERTAIN` (0.90–0.999), `FRIDAY_MARKET_PAST_END_HOURS`.
   The new soft flag `PAST_END` marks rows traded after their scheduled end.
2. **Store 6C.1** migrates 6B stores in place (adds two `runs` columns, keeps all
   history). The depth check is now computed over markets whose every outcome is
   usable, so one-sided and settled books no longer distort it.
3. **Export.** `python -m friday.market export [--token|--market] [--hours] [--format csv|json] [--out]`
   and `GET /api/market/export`: rows oldest first, with question and outcome names.
4. **Market data as Claude context** (`friday/market/context.py`, 6C.1). An
   `INTELLIGENCE_ANALYSIS` job now declares the existing `READ_MARKET_DATA`
   capability; no new capability exists. For each command, up to 3 tracked
   markets are matched by significant words (or by `market_ids` when requested),
   and Claude receives, per outcome: the latest bid/ask/mid/spread, ±2¢ USD
   depth, flags, usable, and the 24 h mid change and range, all from FRIDAY's own
   history. The block is `MEASURED_DATA` with `instruction_authority: NONE`, sits
   inside the prompt's data boundary, and cannot be spoofed by callers. Analysis
   notes list "Market data consulted". `GET /api/market/context` (and
   `python -m friday.market context`) previews the block without calling Claude.
   `POST /api/market/analyse {"market_id": …}` asks Claude, via Hermes and the
   analysis budget, for an advisory `MARKET_ANALYSIS` of one tracked market.
5. **Truncated replies.** A Claude reply cut off at the 3,000-token output limit
   can't be parsed and becomes a safe NO_TRADE fallback. The first live 6C market
   analysis hit this. Market analyses now ask for a compact reply, and any
   analysis that is cut off is retried ONCE with a brevity instruction (budget
   checked first; both calls' tokens are counted; `retried_brief` in the result).
6. Nothing in 6C can trade: actions stay within NO_TRADE/ANALYSE/WATCH/RESEARCH,
   `execution_authorized` is always false, and execution remains LOCKED.
