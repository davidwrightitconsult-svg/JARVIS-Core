# Phase 8 — Probability & Edge: Report

Status: **Phase 8 complete** (8A, 8B, 8C signed off)
Backfill **8C.2** · Segments **8C.2** · Calibration **8B.2** · Store **8A.1** · Fair value **8C.1** · Strategies **8C.1** · Scorecard **8C.1** · Market context **8C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_8_EDGE.md`

## 8A — Calibration dataset and analysis
| Item | Detail |
|---|---|
| `friday/market/client.py` | `closed_markets` (Gamma closed, newest first, falls back if ordering is rejected) and `price_history` (CLOB `/prices-history`), both read-only |
| `friday/market/backfill.py` | Clean resolutions only; reference time; price at 1h/6h/24h/72h/168h (never a later price); skips remembered; transient errors retried; paced; one run at a time; background thread |
| `friday/quant/calibration.py` | Price bins with Wilson intervals, bias and verdicts; pooled long shots and favourites; Brier and log loss against a no-skill baseline; Markdown |
| Store 8A.1 | `calib_markets`, `calib_points`, `calib_skipped` (in-place) |
| Pipeline | Starts a background backfill batch every 3h (outside Hermes jobs) |
| App / CLI | `/api/quant/calibration`, `POST /api/market/calibration/backfill`; `python -m friday.market backfill`, `python -m friday.quant calibration`; calibration section in the daily scorecard note |
| Tests | `test_calibration.py`: 13 tests (488 in the full regression) |

### 8A live results (jarvis020, 2026-09-28)
A live read-only backfill added 131 resolved markets (1,256 price points) in 104s, with 19 skipped for volume under $1,000. The Brier score was 0.090 at 7d, 0.039 at 24h and 0.000 at 1h, against a no-skill 0.25, so prices sharpen toward resolution as they should. No price came from after resolution, and there was exactly one winner per market.

In the first table, most samples sat at the extremes and were CONSISTENT. The middle bins were thin but pointed the favourite–longshot way: 10–20¢ won 0 of 14 and 60–90¢ won 27 of 27. The service backfills about 300 markets every 3 hours.

## 8B — Fair probability and edge
| Item | Detail |
|---|---|
| `friday/quant/fair.py` | Horizon choice; bin bias shrunk by n/(n+200), capped ±15¢; fair value for both sides; edge vs the ask + fee with a z-score; EDGE / NO_EDGE / INSUFFICIENT_CALIBRATION; ¼ Kelly capped at 2% (arithmetic only); cached tables |
| Scan | `edge`, `edge_status`, `edge_side`, `fair` (edge is None until calibration backs it) |
| App / CLI | `/api/quant/edges`; `python -m friday.quant edges`; `fair_value` in Claude's market context (8B.1) |
| Settings | `FRIDAY_EDGE_FEE`, `FRIDAY_EDGE_MIN`, `FRIDAY_EDGE_MIN_Z`, `FRIDAY_EDGE_KELLY_FRACTION`, `FRIDAY_EDGE_MAX_STAKE` |
| Tests | `test_fair_value.py`: 12 tests (500 in the full regression) |

### 8B live results (jarvis020, 2026-09-30) and the 8B.2 fix
The calibration dataset held 1,428 resolved markets (2,759 samples at 24h, Brier 0.042 against a no-skill 0.25). At 24h the favourite–longshot bias is significant:
- long shots were priced 1.8% and won 0.5% (969 samples, OVERPRICED);
- favourites were priced 98.1% and won 99.5% (UNDERPRICED);
- the 5–30¢ bands were OVERPRICED, and the 70–95¢ bands UNDERPRICED.

The first edge run flagged 20 EDGE rows, but the edges were identical across unrelated markets: four MLB games all showed +6.17¢. That came from one pooled price-band bias, and the sports horizon was mismatched as well. 8B.2 calibrates and applies bias per segment (sports / field / binary) and never across segments, and sports always use 24h. Tests: 16 in `test_fair_value.py` (504 in the full regression).

8B.2's live run gave 4 EDGE rows, all binary, all with different values (Iran blockade YES +5.36¢, BTC $82.5k dip NO +5.07¢ at z 17, Iran ceasefire YES, Visconde KO/TKO NO +2.79¢). Sports had no calibration at all. A diagnostic found why: Gamma's `order=endDate` listed 2029 end dates first, so the crawl never reached finished games.

## 8C — Edge estimates tracked to resolution; sports calibration gap closed
| Item | Detail |
|---|---|
| `friday/market/client.py` | `closed_markets` orders by **closedTime** (newest closures first), falling back to endDate, then to no ordering |
| `friday/market/backfill.py` 8C.1 | Reads volume from every Gamma field (`volumeNum`, `volume`, `volumeClob`+`volumeAmm`); a market with none is skipped as "volume unknown". 8A's volume skips (a blank field read as 0) are reopened once. The crawl resumes at the depth the last run finished, and depth is remembered only after complete pages |
| `friday/quant/segments.py` | Fight and prop wording is sports (KO/TKO, submission, decision, the distance, rounds, handicaps, halves/quarters, moneyline, match winner, UFC). Matchmaking ("fight X next?") and politics stay binary |
| `friday/quant/fair.py` 8C.1 | z = edge / √(statistical se² + `FRIDAY_EDGE_MODEL_SE`²) (default 1¢); the purely statistical z is still reported |
| `friday/quant/strategies.py` / `pipeline.py` | **CALIBRATION_EDGE**: every EDGE estimate on an eligible market is recorded once per market, on the side to buy, with all model inputs. It is scored at its horizon and held to resolution |
| `friday/quant/scorecard.py` 8C.1 | Held-to-resolution results get a 95% CI and a verdict. CALIBRATION_EDGE is judged on them, over a 365-day window |
| Store | Resolution checks rotate through due markets (none starve); `signalled_markets`; `forget_calibration_skips` |
| App | `POST /api/market/calibration/backfill?markets=N` (max 2000) to catch up faster |
| Tests | `test_edge_tracking.py`: 28 tests (532 in the full regression) |

Nothing here can trade. A CALIBRATION_EDGE verdict of PROMISING would still be in-sample evidence only; Phase 9 validates it out of sample.

### 8C live results (jarvis020, 2026-10-02) and the 8C.2 scope change
8C validated in full on jarvis020. The z floor left one EDGE row (Iran blockade YES, +5.09¢, z 2.69, statistical 3.18), and it was queued as the first CALIBRATION_EDGE signal. Two defects showed up:
- 12 election markets ("Berlin State Election: SPD Over/Under 13%?") were classed as sports by the new over/under wording.
- 40 of the newest 100 closed markets carried no volume field; most were match props.

The owner then set the scope: **FRIDAY is not a sports betting system.** 8C.2 implements that decision.

| Item | Detail |
|---|---|
| `friday/market/segments.py` (moved from quant, re-exported) | `FRIDAY_EXCLUDED_SEGMENTS` (default `sports`, `none` to include everything). Sports are recognised by game/prop wording, league and competition names, and Gamma's own sports fields and tags. "Over/Under N%" is not sports |
| Scan | Out-of-scope markets are dropped before any order book is fetched and are never stored; watchlist entries are released. Counted as `SEGMENT_SPORTS` in the run report |
| Universe | `OUT_OF_SCOPE: sports` — never eligible, so no signals and no edges |
| Backfill 8C.2 | Out-of-scope markets are skipped before price history is fetched and do not use the batch budget. The skips are forgotten if the segment returns to scope |
| Data already recorded | Sports rows already in the calibration data stay, unused. Open sports signals finish scoring |
| Tests | 47 in `test_edge_tracking.py` (551 in the full regression) |

Focus from here: non-sports event markets (politics and geopolitics, crypto price levels, economy), where the calibration data shows the favourite–longshot bias.
