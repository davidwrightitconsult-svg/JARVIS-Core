# Phase 3 — Hermes Agent Layer: Report

Status: **3A PASS** · **3B PASS** · **3C PASS** (signed off) · **3D awaiting sign-off**
Hermes contract **3A.1** · runtime **3B.1** · scheduler **3C.1** · integration **3D.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_3_HERMES.md`

## 3A — Hermes contract
| Item | Detail |
|---|---|
| `friday/hermes/authority.py` | Capability allowlist: `READ_STATE`, `READ_HEALTH`, `READ_MARKET_DATA`, `REQUEST_INTELLIGENCE`, `EMIT_EVENT`. Execution/risk/bankroll/wallet/credential actions have no enum member; name guard rejects them in any spelling (`placeOrder`, `place-order`, …). Unknown capabilities are rejected, never granted. |
| `friday/hermes/jobs.py` | Job types `STATUS_REPORT`, `HEALTH_CHECK`, `MARKET_SCAN`, `INTELLIGENCE_ANALYSIS`, each with declared capabilities; registry verified at import. Strict per-type parameter schemas (extra fields rejected, scan limit 1–100, intelligence task must be one of the 6 Phase 2 tasks). Lifecycle state machine with explicit transition table; terminal states final; attempts counted. |
| `friday/hermes/audit.py` | Every job state and every rejected request becomes a control-plane `Event` with `execution_authorized: false`. |
| Tests | `test_hermes_contract.py` — 52 tests (also pass under `python -O`) |
| Runtime behaviour | None yet — 3A is contract only; nothing is wired into the running app. |

Server validation: `bash scripts/validate_phase_3a.sh` on jarvis020 (offline; no API cost).

## 3B — Hermes runtime
| Item | Detail |
|---|---|
| `services.py` | Handlers get a **read-only, capability-gated view bound to one job** — never the raw StateManager / HealthManager / market client / Claude adapter. Each accessor checks the job's declared capabilities; all data returned as deep copies. |
| `handlers.py` | `STATUS_REPORT`, `HEALTH_CHECK` (read state/health), `MARKET_SCAN` (read-only snapshots, no scoring or decision), `INTELLIGENCE_ANALYSIS` (budget check → Phase 2 adapter with `execution_locked`/`execution_allowed:false` context → tokens recorded). |
| `runtime.py` | Queue + single worker; per-job timeout (default 120 s); retries **only** for transient failures (network, timeout), max 2 attempts; budget, authority, missing-service and handler-bug failures are final. Every state change persisted + audited. Background worker `start()/stop()`. |
| `budget.py` | Daily Claude token ceiling per UTC day (default 250,000 ≈ 40 analyses), 8,000-token reserve required before each call; persisted so restarts don't reset spend; exhaustion = logged, non-retryable refusal. |
| `store.py` | SQLite tables `hermes_jobs`, `hermes_token_usage`; oversized results stored as an explicit `truncated` marker, never cut mid-JSON. |
| Tests | `test_hermes_runtime.py` (25 offline) · `test_hermes_live.py` (1 live call through Hermes) |
| Wiring | Not yet connected to the running app (3D). |

Known limitation: Python cannot kill a thread, so a timed-out handler is abandoned (daemon thread) and its late result discarded.

## 3C — Scheduler, controls, kill switch, health
| Item | Detail |
|---|---|
| `scheduler.py` | Recurring schedules wrap ordinary job requests and are validated the same way (forbidden job types cannot be scheduled). Minimum intervals: status/health 30 s, market scan 300 s, Claude analysis 900 s. Max 20 schedules. **No pile-up**: a schedule is skipped while its previous job is still pending/running. |
| `control.py` | `HermesController` state machine: STOPPED → RUNNING ⇄ PAUSED; any → **KILLED**. Pause holds the queue; resume releases it. **Kill switch** closes job intake first (atomic with submit, so an in-flight scheduler tick cannot sneak work in), stops scheduler + worker, cancels every queued job, logs a CRITICAL event. Leaving KILLED requires `reset(confirmed_by=<human>)`, then an explicit start. |
| `runtime.py` | Intake gate (`close_intake` / `open_intake`); `cancel_all_pending`; each worker start gets its own stop signal (fixes a pause-during-long-job → resume race that could leave Hermes RUNNING with no worker). |
| `core/health.py` | `hermes` component: RUNNING and PAUSED are healthy; **STOPPED (while FRIDAY is online) and KILLED report DEGRADED** so they are visible. |
| `core/lifecycle.py` | Optional `hermes=` controller: started after the control plane, stopped before it; a Hermes start failure is recorded but never blocks FRIDAY. |
| Tests | `test_hermes_scheduler.py` (27, stable over 10 repeated runs) · `test_hermes_soak.py` (~70 s real timing run, `FRIDAY_SOAK=1`) |
| Execution lock | Unaffected by every Hermes state, including KILLED (tested). |

## 3D — Integration into the FRIDAY app
| Item | Detail |
|---|---|
| `app/main.py` | Hermes built at start-up and run with FRIDAY's lifecycle. **Free-text commands are routed through Hermes as `INTELLIGENCE_ANALYSIS` jobs** (Phase 2 contract: trust boundary, JSON validation, budget, audit). Deterministic commands (status/scan/help/stop) never call Claude. Replies always end "Execution remains locked." |
| Bypass retired | `app/friday.py` (`FridayBrain`, free-form Claude calls outside the contract) **deleted**. |
| `/api/hermes/*` | `status` (state, schedules, budget, settings) · `jobs` · `jobs/{id}` · `POST jobs` (validated; forbidden types → 400) · `start` · `pause` · `resume` · `kill` · `reset` (requires `confirmed_by`; illegal transitions → 409). |
| `hermes/bootstrap.py` | `HermesSettings` from `.env` (autostart, daily budget, health/scan intervals, scan size, job timeout); default schedules `health` (60 s) and `market-scan` (900 s). Invalid settings or schedules are recorded as errors and **never block FRIDAY start-up**. |
| Claude headroom | Output limit 1,600 → **3,000** tokens; `stop_reason` recorded; a reply cut off at the limit is flagged `truncated` and safely becomes NO_TRADE. Budget reserve raised to 10,000. |
| Budget fix | Claude replies that failed the contract previously dropped their token counts, so the call was not charged to the budget. Tokens are now reported on that path too. |
| Docs | `.env.example` documents all Hermes settings; `friday/README.md` run command corrected (must run from repo root: `friday.app.main:app`). |
| Tests | `test_app_hermes.py` (15, FastAPI TestClient, fakes, temp DB) · `test_claude_truncation.py` (4) · `test_app_live.py` (1 live call) · validation also boots the **real server** on 127.0.0.1:8765 with a temp DB. |

## Phase 3 outcome
Hermes is FRIDAY's operator: it schedules, routes, observes and reports, with the same authority ceiling as Claude. Every path into Claude now goes through the Phase 2 contract, and the operator can pause Hermes or kill it at any time, locally, without affecting the execution lock.

## Findings carried forward
1. **API authentication** — the FastAPI endpoints (including Hermes controls) have no authentication. Safe today because FRIDAY binds to 127.0.0.1 and UFW allows SSH only; authentication is required before any UI or remote access (Phase 15 at the latest).
2. **Polymarket book parsing** (Phase 6) — `polymarket.py` takes `bids[0]`/`asks[0]` as best prices; CLOB book ordering must be verified before scan prices/spreads are trusted.
3. **`/api/scan` scoring** is a Phase 0 heuristic (liquidity/spread/price-range points), not a validated strategy — to be replaced by the Quant Core (Phase 7).
4. **`friday/start.ps1`** (Windows, Phase 0) uses the old run command; harmless on Ubuntu, to be removed or corrected.
