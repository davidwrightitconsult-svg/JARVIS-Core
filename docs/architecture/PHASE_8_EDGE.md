# Phase 8 — Probability & Edge

Phase 7 measures what FRIDAY's *own* signals do. Phase 8 asks the question
underneath every edge: **are Polymarket's prices well calibrated, and where
are they not?** It then turns the answer into a cost-aware fair probability
and edge for live markets.

| Sub-phase | Delivers |
|---|---|
| **8A** | Calibration dataset (resolved markets and their prices 1h–7d before resolution), calibration analysis |
| **8B** | Fair-probability model (calibration-adjusted), edge versus the live ask after costs, capped Kelly fraction (numbers only) |
| 8C | Edge estimates recorded as signals and scored at resolution; Phase 8 close |

## 8A contract
1. **Backfill (`friday/market/backfill.py`, 8A.1)** is read-only against
   Polymarket. It walks Gamma `/markets?closed=true`, most recently ended
   first. A market is kept only if it resolved **cleanly** (closed, with every
   outcome price exactly 0 or 1, summing to 1) and traded at least
   `FRIDAY_CALIBRATION_MIN_VOLUME` (default $1,000).
   - **Reference time** is when the outcome became known: the earlier of
     Gamma's endDate and closedTime.
   - **Price at each horizon.** For each outcome token, FRIDAY fetches the
     hourly CLOB `/prices-history`. It takes the **last price at or before**
     the reference time minus each horizon (1h, 6h, 24h, 72h, 168h, within a
     tolerance) and records it with the token's 1/0 payoff. A later price is
     never used.
   - **Skips.** Unresolved, ambiguous, thin or history-less markets are
     remembered with a reason and never fetched twice. A transient error is
     retried on a later run and never recorded as "no data".
   - **Tables:** `calib_markets`, `calib_points`, `calib_skipped` in the
     market store (store 8A.1, in-place).
2. **When it runs.** A background thread, *never inside a Hermes job*, starts
   every `FRIDAY_CALIBRATION_INTERVAL_HOURS` (default 3) from the quant
   pipeline, with up to `FRIDAY_CALIBRATION_BATCH` (300) new markets and a
   15-minute budget. Only one runs at a time, and requests are paced. It can
   also be run manually: `python -m friday.market backfill --markets N`, or
   `POST /api/market/calibration/backfill`. Switch it off with
   `FRIDAY_CALIBRATION_BACKFILL`.
3. **Analysis (`friday/quant/calibration.py`, network-free).**
   - **Per price bin:** the bins are 0–2–5–10–20…90–95–98–100¢. Each bin
     reports samples, distinct markets, mean price, the actual win rate with a
     **Wilson 95% interval**, the bias (win rate − price) and a verdict:
     `OVERPRICED` or `UNDERPRICED` when the interval excludes the price,
     `CONSISTENT` otherwise, or `FEW_SAMPLES` below 30.
   - **Pooled:** long shots (<10¢) and favourites (≥90¢) are also reported
     as groups.
   - **Accuracy:** the Brier score and log loss, against a no-skill baseline.
   - **Samples:** every outcome token is one sample, so a binary market gives
     p and 1−p and the table is symmetric by construction.
4. **Honest limits.**
   - The history is a traded or mid price, not the executable ask; 8B prices
     edges against the live ask.
   - A market resolved early may already reflect the news at short horizons,
     so 24h+ is the default analysis horizon.
   - A bias measured on past prices is not yet an executable edge.
5. **Where it surfaces.** `GET /api/quant/calibration?horizon=&min_volume=`,
   `python -m friday.quant calibration`, and a calibration section in the
   daily scorecard note.
6. Execution remains LOCKED. There is no new capability and no new job type,
   and the Quant Core stays network-free.

## 8B contract — fair probability and edge
1. **Fair value (`friday/quant/fair.py`, 8B.1)** is transparent, additive and
   shrunk:
   - Choose the calibration horizon closest to the market's time to end:
     24h, 72h or 168h. A market ending further out uses 168h.
   - Take the primary outcome's live mid, p, and its price bin's measured bias
     b (win rate − mean price) from resolved markets.
   - Shrink the bias by the evidence, w = n / (n + 200), so 30 samples give
     w = 0.13. The adjustment is capped at ±15¢.
   - fair = p + w·b, and the other outcome's fair value is 1 − fair (the table
     is symmetric).
   - With no data, fair = p and the status says so. The standard error of the
     adjustment is carried with the estimate.
2. **Edge** is measured against the **live ask** (what FRIDAY would pay), never
   the mid:
   - edge = fair − ask − fee (`FRIDAY_EDGE_FEE`, default 0).
   - z = edge / (w·se).
   - The status is `EDGE` only if edge ≥ `FRIDAY_EDGE_MIN` (2¢) **and**
     z ≥ `FRIDAY_EDGE_MIN_Z` (2). Otherwise it is `NO_EDGE`, or
     `INSUFFICIENT_CALIBRATION` when the bin has fewer than 30 samples. With no
     calibration, the only "edge" is minus the half-spread.
3. **Sizing, as arithmetic only.** Kelly for a $1 contract bought at a with
   probability q is f* = (q − a)/(1 − a). FRIDAY reports ¼ Kelly
   (`FRIDAY_EDGE_KELLY_FRACTION`) capped at 2% of bankroll
   (`FRIDAY_EDGE_MAX_STAKE`), and only for `EDGE` rows. It has no authority:
   the Risk Governor (Phase 10) and Decision Engine (Phase 11) own sizing and
   approval.
4. **Where it surfaces.**
   - **Scan:** `/api/scan` rows gain `edge`, `edge_status`, `edge_side` and
     `fair`. `edge` stays `None` until the price band has enough calibration
     data to back a number.
   - **API:** `GET /api/quant/edges?market_id=&edges_only=`.
   - **CLI:** `python -m friday.quant edges`.
   - **Claude:** market context carries `fair_value` (context 8B.1).
   - Every number is labelled **ESTIMATED — UNVALIDATED**.
5. **Honest limits.**
   - Calibration uses traded/mid history prices, while edges use the live
     ask, which is conservative.
   - The bias of a price band is an average across many kinds of market.
   - An estimated edge is a hypothesis until 8C records it and Phase 9
     validates it out of sample.
6. Execution remains LOCKED. There is no new capability, no new job type and
   no trade action.

## 8B.2 — per-segment calibration (fix after the first live run)
On jarvis020 the first 8B run found 20 "EDGE" rows. Four different MLB games
all showed the **identical** +6.17¢ (z 3.93), and four candidate markets sat
within 0.5¢ of each other. One pooled bias for the 40–50¢ band, measured
across every kind of market, was being applied to every market priced there.
That is a property of the dataset mix, not information about those markets.
A 6¢ edge on major-league moneylines is a red flag, not an opportunity. A
second defect: sports markets were matched to the 168h calibration horizon,
because a game market's end date is a resolution buffer set days after the
game.

Fix (`friday/quant/segments.py`, fair value 8B.2, calibration 8B.2, context 8B.2):
1. Every market, in the calibration dataset and live, is classified by its
   question and Polymarket's neg-risk flag:
   - **sports:** "A vs. B", "Spread:", "O/U", "win on <date>", "(BO5)",
     "Game N Winner", and similar.
   - **field:** one outcome of a multi-outcome event.
   - **binary:** every other event question.
2. The fair-value tables are built **per (segment, horizon)**, and a segment
   **never borrows another segment's bias**. A segment without data gets no
   adjustment and is marked `INSUFFICIENT_CALIBRATION`.
3. Sports markets always use the 24h horizon.
4. `python -m friday.quant calibration --segment sports|field|binary` and
   `/api/quant/calibration?segment=` show each segment. `edges` rows, the scan
   and Claude's `fair_value` carry the segment.
5. The validation reports calibration per segment and checks segment isolation
   and the sports horizon. It also prints how many *distinct* edge values the
   EDGE rows have, so a pooled-bias artefact is visible at a glance.

## 8C contract — edge estimates tracked to resolution

**What 8C answers.** Does the fair-value model make money when its bets are held to the end? 8B produces estimates; 8C records every one of them and scores it against what actually happened.

### Recording
- Strategy `CALIBRATION_EDGE` (8C.1) is part of `FRIDAY_QUANT_STRATEGIES=all`.
- After each stored snapshot, the pipeline asks the fair-value model about every eligible market.
- Each `EDGE` row becomes a signal:
  - one per market, ever, so one bet is never counted twice;
  - the token is the side to buy, and the direction is UP;
  - it records the entry bid and ask and every model input (fair, edge, z, statistical z, segment, bin, samples, weight, stake fraction, versions).
- The signal's horizon is the calibration horizon used (24/72/168h).

### Scoring
Scoring reuses the 7C machinery unchanged:
- **Mark-to-market at the horizon:** exit bid minus entry ask.
- **Held to resolution:** payoff minus entry ask.

Resolution checks rotate through due markets, so long-dated markets cannot starve the rest.

### Verdict
The scorecard judges CALIBRATION_EDGE on the held-to-resolution result, over 365 days:
- **INSUFFICIENT_DATA:** fewer than 30 resolved signals.
- **NEGATIVE_AFTER_COSTS:** the 95% CI of the mean is entirely below 0.
- **INCONCLUSIVE:** the CI straddles 0.
- **PROMISING:** the CI is entirely above 0. This is still in-sample; Phase 9 validates it out of sample.

### Uncertainty floor
The statistical standard error only measures sampling noise in the bin. It ignores the fact that a price band's bias drifts over time and differs between markets. A 20,000-sample bin gave the BTC dip NO side z 17.

8C adds a model floor in quadrature:

z = edge / √((w·se)² + model_se²), where model_se = `FRIDAY_EDGE_MODEL_SE` (default $0.01).

A 5¢ edge is therefore at most about z 5. The purely statistical `z_statistical` is still reported.

### Sports calibration gap
- **Crawl order.** Gamma `order=endDate` listed far-future end dates first (2029 appointment markets), so the backfill never reached recent games. 8C crawls by `closedTime` (newest closures first), then resumes at the depth the last run finished.
- **Volume.** Volume comes from `volumeNum`, `volume` and `volumeClob`+`volumeAmm`. A market with no volume field is skipped as "volume unknown", not treated as 0. 8A's volume skips are reopened once.
- **Wording.** Fight and prop wording is classified as sports. Segments are computed when the data is read, so existing calibration markets move to sports without a migration.

## 8C.2 — scope: sports excluded

**Decision (owner, 2026-10-02).** FRIDAY does not trade or study sports markets.

**Why.** Games resolve within hours, so a 15-minute scan has no speed advantage. Sports prices follow professionally set lines, and many props barely trade. The mispricing FRIDAY has measured is in non-sports yes/no event markets.

**Setting.** `FRIDAY_EXCLUDED_SEGMENTS` (default `sports`; `none` includes everything). It is read by the app's market client, the universe rules and the backfill. The library classes default to no exclusion.

**Where it applies**
- **Scan:** an out-of-scope market is dropped at the candidate stage, before its order books are requested, so its slot goes to the next event market. It is never stored.
- **Watchlist:** out-of-scope entries are released.
- **Universe:** `OUT_OF_SCOPE: <segment>` makes a market ineligible whatever its liquidity. Signals and edges only exist for eligible markets.
- **Backfill:** skipped as `out of scope: <segment>` before any price-history request.

**How sports are recognised**
1. Game and prop wording (8B.2 and 8C).
2. League, competition and esports names.
3. Gamma's `sportsMarketType`, `gameId` and `gameStartTime` fields, or a sports tag or category.

A percentage over/under ("SPD Over/Under 13%") is an election market, not sports.
