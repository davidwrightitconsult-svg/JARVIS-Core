# Phase 9 — Backtesting & Validation Lab

Phase 8 measured a pricing bias and built a fair-value model on it. That evidence is in-sample: the same resolved markets taught the model and judged it. Phase 9 tests FRIDAY's models and strategies on data they have not seen, before the Risk Governor (Phase 10) or the Decision Engine (Phase 11) may rely on them.

| Sub-phase | Delivers |
|---|---|
| **9A** | Walk-forward backtest of the calibration edge, split by market type, with cost sensitivity |
| **9B** | Calibration data re-anchored on the scheduled end date; fair-value tables rebuilt on it; robustness across horizons, thresholds and FRIDAY's recorded asks |
| **9C** | The validation gate (which strategies FRIDAY may paper-trade), the pricing-inconsistency scan; Phase 9 close |

Scope: sports are excluded (`FRIDAY_EXCLUDED_SEGMENTS`, 8C.2).

## 9A contract — walk-forward backtest

**Question.** Had FRIDAY traded its rule on markets it had not yet seen, what would it have made after costs?

**Data.** The 8A calibration dataset: resolved two-outcome markets in scope, with a recorded price for both outcomes `horizon` hours before resolution. Markets whose two prices do not sum to about 1 are dropped. Only markets whose primary price sits inside the live universe's price band (3¢–97¢ by default) can be bet, because FRIDAY never looks at markets outside it.

**Folds.** Markets are sorted by resolution time and cut into `folds + 1` chunks. Chunk 0 only trains. Each later chunk is tested with tables built from markets that had resolved at least `horizon` hours before the chunk's first resolution, which is before the first test price was observed. A fold with fewer than 100 training markets is skipped and says so.

**Rule.** Exactly the live rule, sharing the `fair.py` arithmetic (`bin_table`, `shrunk_fair`, `kelly`):
- per-segment bin bias, shrunk by n/(n+200) and capped;
- bet the better side only with at least 30 training samples in the band, edge ≥ `FRIDAY_EDGE_MIN` and z ≥ `FRIDAY_EDGE_MIN_Z` (with the model-uncertainty floor).

**Costs.** The dataset holds traded or mid prices, not asks. The ask is assumed: recorded price + half-spread (default 1¢). Results are also reported at 0, 0.5, 2 and 3¢.

**Result per bet.** Held to resolution: payoff − assumed ask − fee, per $1 share.

**Statistics.** Bets that resolve on the same day are not independent. Two intervals are reported: per-bet, and day-clustered (each resolution day is one observation). The verdict uses the clustered one.

| Verdict | Meaning |
|---|---|
| INSUFFICIENT_DATA | Fewer than 30 bets or fewer than 5 resolution days |
| NEGATIVE_OUT_OF_SAMPLE | Clustered 95% interval entirely below zero |
| INCONCLUSIVE | Interval straddles zero |
| PASSES_OUT_OF_SAMPLE | Interval entirely above zero. Necessary, not sufficient: costs are assumed, and paper trading (Phase 12) must reproduce it with real asks |

**Splits.** By market type (`category_of`: crypto_micro, mentions, weather, crypto, entertainment, economy, geopolitics, politics, other), segment, bought-price band and fold. The in-sample figure is shown beside the out-of-sample one.

**Interfaces.** `python -m friday.lab backtest [--horizon 24] [--folds 5] [--half-spread 0.01] [--json]`; `GET /api/lab/backtest`.

**Honest limits**
- The assumed half-spread is the largest unknown; thin markets can be wider than 1¢.
- The dataset starts when FRIDAY's backfill started, so the test window is weeks, not years.
- Market type comes from question wording and will misfile some markets.
- There is no capacity or fill modelling: one $1 share per bet.

The lab is read-only over recorded data, has no network path, and has no execution authority.

## 9B contract — end anchor and robustness

**The 9A finding.** 9A passed out of sample at 24h with +11.4¢ per share. The result was not trustworthy:
- The 8A points are prices a fixed time before the *actual* resolution. For a market that resolved early, that is knowledge of the future. "NVIDIA all-time high by December 31?" resolved on 2 October; its "24h before" price was observed when FRIDAY, live, would have seen three months to the end.
- The same test made no bets at 72h and was inconclusive at 168h.
- Asks were assumed at 1¢ above a traded or mid price.

### End-anchored points
`CalibrationBackfill.reanchor()` builds `calib_points_end`: the price at 24h, 72h, 168h, 336h and 720h before the **scheduled end date**, and only when the market was still open at that moment.
- A market that resolved before `end − h` has no point at `h`.
- A market that resolved more than 30 days before its end gets no points and costs no request.
- The last week before the end is read at 60-minute fidelity. The 14- and 30-day horizons are read at 6-hour fidelity, with one extra request per token. If Polymarket refuses that range, the market keeps its short horizons.
- A transient error leaves the market pending for a later run.
- Out-of-scope markets are marked and skipped.
- Every background batch crawls new markets and then re-anchors what is pending. `POST /api/market/calibration/reanchor?markets=N` and `python -m friday.market reanchor` do it on demand.

### Fair value 9B.1
- Tables are built from end-anchored points only.
- A live market uses the nearest horizon by ratio.
- A market more than 45 days (1080h) or less than 6h from its end is `OUT_OF_CALIBRATED_RANGE`: no estimate, no edge, no signal. 8B applied the 168h table to any longer-dated market.

### Backtest 9B.1
- `anchor="end"` is the default. A bet's decision time is `end − h`. Folds are cut by decision time, and a fold trains only on markets that had resolved before its first decision.
- `anchor="resolution"` is kept for comparison.
- `asks` prices bets at recorded asks.

### Robustness report
`run_robustness` reports four things:
- **Horizons:** the end-anchored out-of-sample result at each of the five horizons.
- **The leak:** resolution-anchored against end-anchored at 24h, 72h and 168h.
- **Thresholds:** minimum edge of 1, 2, 3 and 5¢ at the headline horizon.
- **Real asks:** for markets whose order book FRIDAY recorded within 3 hours before the decision time, how far the recorded ask sat above the history price, and the rule's result when it pays that ask.

`robust` is true only if the end-anchored test passes at two or more horizons and two or more thresholds, with no horizon negative. It feeds the 9C gate and authorises nothing.

**Interfaces.** `python -m friday.lab robustness`; `GET /api/lab/robustness`; `scripts/reanchor_and_report.sh` (asks the running service to re-anchor, waits, prints the report).

**Honest limits**
- FRIDAY has recorded order books only since late September 2026, so the real-ask sample is small.
- End dates on Polymarket are sometimes nominal; a market can resolve after its end date. The anchor is still what FRIDAY sees live.
- Markets more than 45 days from their end, which is most of the live election markets, are outside the model.

## 9B.2 — the dataset rebuilt from markets that reached their end

**What the first re-anchor showed (jarvis020, 2026-10-02).**
- Of 1,962 calibration markets, 1,470 had resolved more than 30 days before their scheduled end. 8A crawled closed markets with the furthest end dates first, and a closed market whose end date is years away has resolved early by definition. The Phase 8 bias finding was measured on that sample and is withdrawn.
- Only 199 markets had end-anchored prices, all from the last nine days. Every horizon was INSUFFICIENT_DATA.
- Polymarket refuses any price-history request spanning 30 days (400), at every fidelity. A one-week request works, even for markets that ended six months ago.
- Gamma refuses offsets of 5,000 and beyond (422). It does filter by end-date window and minimum volume.

**The rebuild.** `CalibrationBackfill.run_windows(days)` walks 6-hour end-date windows backwards.
- Gamma is asked for closed markets whose end date lies in the window and whose volume is at least `FRIDAY_CALIBRATION_MIN_VOLUME` (now $10,000).
- The last 7 days are re-checked on every run, because a market can close after its end date. The crawl then continues from a saved cursor, which only moves past a window once every market in it was handled.
- Each market is end-anchored in the same pass: one request per token for the last week, plus one 12-hour request for each of the 14- and 30-day moments, and only when the market was already trading at the start of that week.
- A window holding more markets than Gamma's offset limit is counted in `truncated_windows`.
- 9B.1's end-anchor results are redone once with the small-request method.

**Scope.** Recurring five-minute markets ("Bitcoin Up or Down - 12:35PM-12:40PM ET") are a new segment, `micro`, and are out of scope by default alongside sports. FRIDAY scans every 15 minutes and cannot act inside a five-minute window, and Polymarket lists over a thousand of them a day.

**Interfaces.** `POST /api/market/calibration/backfill?markets=N&days=D`; `python -m friday.market rebuild`; `scripts/rebuild_and_report.sh` (drives the running service, shows progress, prints the robustness report).

## 9C contract — the validation gate

**Purpose.** One answer, in one place, to "what is FRIDAY allowed to act on?". The Risk Governor (Phase 10), Decision Engine (Phase 11) and Paper Trading (Phase 12) read the gate. Nothing else decides it.

**Statuses.** Each strategy gets exactly one.

| Status | Meaning |
|---|---|
| APPROVED_FOR_PAPER | The evidence passes FRIDAY's own test. Paper money only |
| FORWARD_TEST | It cannot be tested on history without knowing the answers. It may be paper-traded as a hypothesis; its paper record is the evidence |
| NOT_APPROVED | Tested and not supported, or not enough evidence. It may be recorded and scored, never sized |

**Evidence**
- **CALIBRATION_EDGE:** the 9B robustness report. Approved only if `robust`.
- **Feature strategies** (BOOK_IMBALANCE, MEAN_REVERSION, MOMENTUM, LONGSHOT_BIAS): the live scorecard, where every signal is scored after crossing the spread. Approved only if the 95% interval of the net result is entirely above zero, on at least 100 scored signals over at least 7 days.
- **PRICING_INCONSISTENCY:** the scan below. Approved only if PRESENT.
- **RESEARCH_FORECAST:** FORWARD_TEST. A forecast Claude makes today about a market that has already resolved would leak the outcome.

**Live trading.** `live_approved` is always empty. The gate cannot approve live trading; that is the Phase 17 audit and the owner's decision. `allowed(gate, strategy)` is true for approved and forward-test strategies and means paper only.

### Pricing-inconsistency scan
A two-outcome market pays exactly $1 to one side. If the best ask of both outcomes sums to less than $1, buying both locks in the difference.

- The scan reads FRIDAY's recorded order books (usable quotes, in-scope markets).
- For thresholds of 0.5¢, 1¢ and 2¢ it reports observations, episodes (one market on consecutive scans), markets, days, size, duration and depth.
- PRESENT needs at least 30 episodes of 1¢ or more on at least 5 days.

Limits:
- one snapshot every 15 minutes, so a gap seen may be gone before an order could be placed;
- depth is "within 2¢ of the mid", not the size at the best ask;
- both legs must fill;
- multi-candidate events are not covered, because FRIDAY does not yet record which markets belong to one event.

**Interfaces.** `GET /api/lab/gate`, `GET /api/lab/inconsistencies`; `python -m friday.lab gate`, `python -m friday.lab inconsistencies`.
