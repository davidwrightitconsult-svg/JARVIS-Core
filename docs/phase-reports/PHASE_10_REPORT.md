# Phase 10 — Risk Governor: Report

Status: **complete** · 10A, 10B, 10C signed off (10C on 2026-10-03)
Risk Governor **10C.1** · Paper ledger **10C.1** · Drills **10C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_10_RISK.md`

## 10A — Governor core
| Item | Detail |
|---|---|
| `friday/risk/governor.py` | `RiskSettings` (nested limits, hard ceilings), `Proposal`, `Portfolio`, `Position`, `RiskGovernor.evaluate` (eight ordered checks, size caps, APPROVED / REDUCED / REJECTED), `status` |
| `friday/risk/state.py` | Own SQLite store: kill switch, drawdown halt (persisted), `resume` needs `RESUME`, decision log |
| App | Governor built at startup with the 9C gate as its provider (fails closed); `/api/risk/status`, `/evaluate` (dry run), `/decisions`, `/kill`, `/resume`; health component `risk` |
| CLI | `python -m friday.risk status | decisions | kill | resume` |
| Settings | `FRIDAY_RISK_*`, `FRIDAY_RISK_DB` |
| Tests | `test_risk_governor.py`: 20 tests (624 in the full regression) |

### 10A live results (jarvis020, 2026-10-03)
All checks passed; 624 tests. Against the gate built from the real data (nothing approved for paper; RESEARCH_FORECAST on forward test):

| Proposal | Decision |
|---|---|
| $10, allowed strategy, fresh portfolio | APPROVED $10 |
| $200 (ten times the position cap) | REDUCED to $20 |
| A strategy the gate has not approved | REJECTED, NOT_ALLOWED_BY_GATE |
| LIVE mode | REJECTED, LIVE_TRADING_LOCKED |
| Book already at 30% exposure | REJECTED, NO_ROOM (total exposure) |
| After losing 3% today | REJECTED, DAILY_LOSS_LIMIT |
| Equity 10% below peak, and after recovery without RESUME | REJECTED, DRAWDOWN_HALT |
| Kill switch engaged | REJECTED, KILL_SWITCH |

## 10B — Paper ledger and dashboard
| Item | Detail |
|---|---|
| `friday/risk/ledger.py` | `PaperLedger`: positions opened only from a recorded PAPER approval, closed at an exit price; equity, peak, day start and daily equity kept in the risk store |
| App | Ledger built with the governor; `/api/risk/status` reads it; `GET /api/portfolio`; `GET /api/dashboard` (every section isolated); `GET /api/scan?live=false` |
| Dashboard | `/` serves the read-only dashboard (`static/dashboard.html`, `.css`, `.js`); the console moved to `/console` |
| Scope | The dashboard follows the owner's reference image. The twelve book strategies are shown as not yet registered; Phase 11 builds and tests them |
| Tests | `test_paper_ledger.py`: 14 tests (638 in the full regression) |
| Validation | `scripts/validate_phase_10b.sh`: ledger drill, real-server dashboard smoke, regression, read-only checks |

### 10B live results (jarvis020, 2026-10-03)
All checks passed; 638 tests. Ledger drill: a $500 request was cut to $20; a price move from 0.40 to 0.50 showed +$5.00 and closed at +$5.00; two total losses took equity to $965.30 and the next proposal was refused with DAILY_LOSS_LIMIT. The dashboard served every section, made no venue call and refused writes.

## 10C — Stress drills and close
| Item | Detail |
|---|---|
| `friday/risk/drills.py` | Seven drills on a temporary store: random storm, everything to zero, every trade loses, rush, broken gate, kill switch across a restart, unsafe settings |
| Fixes | Stakes rounded down to whole cents; gate names must be a real list; `open_checked` decides and opens under one lock; clearing a drawdown halt restarts the peak |
| CLI | `python -m friday.risk drill` (exit 0 only if every drill holds) |
| Tests | `test_risk_drills.py`: 8 tests (646 in the full regression); the suite now runs on the default limits whatever `.env` sets |
| Validation | `scripts/validate_phase_10c.sh`: drills with the server's own limits, proof the real risk store is unchanged, server smoke, regression |

Live results are added after the jarvis020 run.

## What Phase 10 leaves for later phases
- Nothing calls the ledger yet. Phase 12's paper engine is its only caller and must use `open_checked`.
- Exits are not the governor's job. Each strategy's exit rule is defined in Phase 11 and run in Phase 12.
- The gate approves nothing for paper today. Phase 11 builds and tests the twelve book strategies.
