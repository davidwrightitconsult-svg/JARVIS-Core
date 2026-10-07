# Phase 7 — Quantitative Strategy (Quant Core)

Phase 7 turns FRIDAY's recorded market data into **numbers that can be
checked**. It works through features, then candidate signals, then measured
outcomes. Claude's opinion is never the input to a number here.

| Sub-phase | Delivers |
|---|---|
| **7A** | Feature engine, tradable-universe gate, and the UI scan served from recorded data |
| **7B** | Strategy library: candidate signals (hypotheses) recorded with their inputs; sticky watchlist |
| **7C** | Signal scoring: what prices did next after the spread, held-to-resolution results, per-strategy scorecard |

Phases 8–9 (Probability & Edge; Backtesting & Validation) decide whether any
measured effect is a real edge. Phase 7 claims no edge.

## 7A contract
1. **`friday/quant/features.py` (7A.1)** contains pure, deterministic functions
   over the Phase 6 store.
   - **Per outcome:** best bid/ask, mid, spread, and half-spread (the cost of
     crossing). Also USD depth within 2¢, USD at the best bid and ask, and
     top-of-book imbalance: (bid USD − ask USD) / total over the recorded
     levels, in −1…1.
   - **Mid change over 1h, 6h and 24h.** This is `None` until the recorded
     history reaches back that far; it is never faked as 0.
   - **Movement and data quality:** volatility (the standard deviation of
     successive mid changes), 24h range, observations, usable ratio, data age,
     and flags.
   - **Per market:** hours to end. For complete outcome sets, it also reports
     the sum of best asks and the sum of best bids. `ask_sum < 1` or
     `bid_sum > 1` is a pricing inconsistency, reported and never acted on.
2. **Universe gate (`friday/quant/universe.py`).** A market is **eligible** only
   if every outcome is usable and fresh (≤ 30 min), and it meets all of these:
   - the worst outcome spread is ≤ 5¢;
   - every outcome has depth ≥ $500 within 2¢;
   - 24h volume is ≥ $1,000;
   - the primary outcome's mid is within 0.03–0.97.

   Every rejection has a reason code (`data quality: …`, `STALE_OBSERVATION`,
   `SPREAD>…`, `DEPTH<…`, `VOLUME<…`, `PRICE_EXTREME`, `INCOMPLETE_MARKET`). The
   limits are set with `FRIDAY_QUANT_*`. Eligibility is a data and liquidity
   gate, not a signal.
3. **Attention score (0–100).** It is computed from depth, tightness and
   24h volume, and ranks what is worth *watching*. It is not value or edge.
4. **`/api/scan` is now served by the Quant Core from recorded data.** The UI
   refreshes every 30 s, and until now each refresh fetched Polymarket live
   *and* wrote another run into the history store. Now the scan takes one live
   snapshot only when the latest run is older than 30 min.
   - `WATCH` means eligible for strategy work, and `IGNORE` comes with its reasons.
   - `score` is the attention score. `edge` is `None`: nothing has been measured yet.
   - If the market store is disabled, the Phase 6 heuristic is used.
5. **Scan universe.** Markets that Polymarket lists but that have no order book
   for any outcome are excluded (`NO_BOOK`). This applies only when the page's
   book fetch had no errors: a fetch failure keeps the market, flagged, with the
   error reported.
6. **Claude's market context** now carries each market's `quant` block
   (changes, volatility, imbalance, half-spread, eligibility and reasons),
   labelled descriptive. Market context is at version 7A.1.
7. **API:** `GET /api/quant/universe` and `GET /api/quant/features?market_id=&eligible_only=`.
   **CLI:** `python -m friday.quant universe|features [--market ID] [--eligible] [--json]`.
8. Execution remains LOCKED. There is no new Hermes capability and no new job
   type. The Quant Core has no Polymarket write path and no Claude path.

## 7B contract — strategy library, signals, watchlist
1. **Strategies (`friday/quant/strategies.py`, 7B.1)** are transparent,
   deterministic rules over the 7A features of **eligible** markets. Each one
   is a written hypothesis, marked *unvalidated*.

   | Strategy | Rule (primary outcome) | Direction | Horizon |
   |---|---|---|---|
   | BOOK_IMBALANCE | \|imbalance\| ≥ 0.6 | toward the heavy side | 1h |
   | MEAN_REVERSION | \|1h change\| ≥ max(3¢, 3 × volatility) | against the move | 6h |
   | MOMENTUM | \|6h change\| ≥ 5¢ and 1h ≥ 1¢, same sign | with the trend | 6h |
   | LONGSHOT_BIAS | mid ≤ 0.10 → DOWN; mid ≥ 0.90 → UP | favourite–longshot bias | 24h |

   Only the primary outcome is evaluated: for a binary market, "YES down" and
   "NO up" are the same bet, and counting both would double every statistic.
   Strategies are selected with `FRIDAY_QUANT_STRATEGIES` (`all`, `none`, or a
   list).
2. **Signals** (`signals` table in the market store, store 7B.1) record the
   strategy and version, market, token, direction (UP/DOWN = the expected move
   of that token's price), strength 0–1, horizon and expiry. They also record
   the mid, bid, ask and half-spread at the time, and **every input the rule
   used**, so that 7C can score each signal against the price that followed,
   net of the spread, and any result can be reproduced.
   - There are no size, stake or order fields.
   - A strategy does not fire again on the same token and direction while its
     previous signal's horizon is still running, so signals are not counted twice.
   - Signals are pruned with the snapshot history.
3. **Pipeline (`friday/quant/pipeline.py`).** It observes the market-data
   engine and runs after every stored snapshot (the Hermes 15-min market scan,
   or a stale-data refresh of `/api/scan`). Each run goes through features,
   the universe gate, the watchlist, the strategies, and then signals. There is
   no new job type and no new capability, and a pipeline failure is reported
   in the scan's errors without breaking the scan.
4. **Sticky watchlist.** A market that has been eligible within the last 48h
   (`FRIDAY_MARKET_WATCHLIST_HOURS`) is observed again on every scan, up to
   `FRIDAY_MARKET_WATCHLIST_MAX` (50) markets beyond the top-volume page, even
   after it leaves the ranking. This gives strategies and 7C continuous
   history. The 7A live run showed why this matters: on game night, many
   eligible markets had been observed for less than an hour. Watched markets
   that become decided or lose their book are dropped. Runs record how many
   markets were `tracked`.
5. **Where signals surface.** Scan rows list their market's open signals, and
   Claude's market context lists them as "hypotheses under test" (context
   7B.1). The APIs are `GET /api/quant/strategies`, `GET /api/quant/signals`
   and `GET /api/quant/watchlist`. The CLI is
   `python -m friday.quant strategies | signals | watchlist | run [--record]`;
   `run` is a preview by default.
6. `edge` stays `None` everywhere, and execution remains LOCKED.

## 7C contract — scoring, resolutions, scorecard
1. **Scoring (`friday/quant/scoring.py`, 7C.1).** When a signal's horizon
   ends, it is scored against the token's **first usable recorded
   observation at or after expiry**. The exit window is expiry + max(1h, ½ ×
   horizon). No estimates are used.
   - `mid_move` = direction × (exit mid − entry mid). This asks whether the
     price moved the predicted way.
   - `net` is the executable result after crossing the spread at entry *and*
     exit. For UP it is exit bid − entry ask. For DOWN, taking the opposite
     side, it is entry bid − exit ask.
   - `cost` is the capital per $1 share committed at entry, and
     `return_on_cost` = net / cost. For example, 1¢ made on a 4¢ long shot is
     25% of the capital.
   - A signal with no usable observation in its window is `NO_EXIT_DATA`,
     counted and never guessed. A signal whose exit window is still open waits
     for a later run. Scoring is idempotent, and results go to the
     `signal_outcomes` table (store 7C.1).
2. **Resolutions.** Once an hour the pipeline asks an *injected* read-only
   resolver (the market-data client's `market_resolutions`, which calls Gamma
   `GET /markets/{id}`) about up to 20 signalled markets. It asks only about
   markets that have not been observed for 2h or are past their end date, and
   the whole check is capped at 30s so it cannot stall the Hermes scan.
   - A market counts as resolved **only** if it is closed *and* every outcome
     price is exactly 0 or 1. Anything else (still open, closed but unsettled,
     disputed, unreachable) counts as not resolved.
   - Every signal on a resolved market then gets `resolution_net`, the result
     had it been held to resolution: UP is payoff − entry ask, and DOWN is
     entry bid − payoff. This is how LONGSHOT_BIAS is judged, since its claim
     is about final resolution rather than 24h drift. It applies even to
     signals that had no horizon exit.
   - A market that resolved before a signal's exit is scored at the payoff
     (`RESOLVED_BEFORE_EXIT`).
   - The setting is `FRIDAY_QUANT_RESOLUTIONS`. The Quant Core itself still
     has no network path.
3. **Scorecard (`friday/quant/scorecard.py`).** For each strategy it reports:
   signals, scored, open, no-exit; hit rate; win rate after costs; average mid
   move; average net with a 95% confidence interval and t-statistic; average
   return on cost; held-to-resolution results; and a split by entry price band
   (<0.10 / 0.10–0.90 / >0.90) and by direction.

   The verdicts are conservative:

   | Verdict | Condition |
   |---|---|
   | `INSUFFICIENT_DATA` | fewer than 30 scored signals; states how many more are needed |
   | `NEGATIVE_AFTER_COSTS` | the confidence interval is entirely below zero |
   | `INCONCLUSIVE` | the confidence interval straddles zero |
   | `PROMISING` | the confidence interval is entirely above zero. This is explicitly *not validated*: Phases 8–9 test it out of sample |
4. **Where it surfaces.**
   - **Daily note:** once each UTC day the pipeline writes
     `FRIDAY/Strategies/Scorecard-<day>.md`, a new FRIDAY-zone folder that is
     auto-created on existing vaults. Vault sync pushes it.
   - **Claude:** each open signal in Claude's market context carries its
     strategy's `strategy_track_record` (context 7C.1).
   - **API:** `GET /api/quant/scorecard`, `GET /api/quant/outcomes`,
     `POST /api/quant/score` and `POST /api/quant/scorecard/note`.
   - **CLI:** `python -m friday.quant score [--record] | scorecard | outcomes`.
5. Execution remains LOCKED. There is no new capability and no new job type.
   A verdict never changes what FRIDAY is allowed to do.
