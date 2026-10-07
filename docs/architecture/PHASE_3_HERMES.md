# Phase 3 — Hermes Agent Layer

Hermes is FRIDAY's in-house **operator**: it decides *what work runs, when,
and in what order*, and routes that work to the right component
(Claude Intelligence, market data, Quant Core, Risk Governor).

Hermes is **not** a trader. It has the same authority ceiling as Claude:

> Hermes can schedule, route, observe and report.
> Hermes cannot place orders, unlock execution, change risk limits,
> change bankroll controls, or touch credentials or wallets.

## Position in the architecture

```
FRIDAY Control Plane (state, lifecycle, commands, health, events)
  └── Hermes (operator)
        ├── INTELLIGENCE_ANALYSIS → Claude Intelligence Engine (Phase 2 contract)
        ├── MARKET_SCAN           → Market data (read-only)
        ├── STATUS_REPORT / HEALTH_CHECK → control plane (read-only)
        └── (later) Quant Core, Research, Paper Trading — each added as a job type
  Risk Governor + Execution Gate remain OUTSIDE Hermes' authority.
```

## Sub-phases

| Sub-phase | Scope | Gate |
|---|---|---|
| **3A Contract** | Capability allowlist (forbidden actions are unrepresentable), job types with required capabilities, validated job requests, job lifecycle state machine, audit events | Offline tests |
| **3B Runtime** | Job queue + worker, handlers per job type, timeouts, bounded retries, daily Claude token budget, SQLite job persistence | Offline tests + live intelligence job |
| **3C Scheduler** | Recurring jobs (e.g. scan every N minutes), pause/resume, kill switch, lifecycle + health integration (`hermes` component) | Soak run on jarvis020 |
| **3D Integration** | `/api/command` free-form requests routed through Hermes → Phase 2 adapter (retires the Phase 0 `FridayBrain` bypass); `/api/hermes/*` endpoints; raise Claude `max_tokens` headroom | Live end-to-end on jarvis020 |

## Design rules

1. **Allowlist, not blocklist.** Hermes' capabilities are an enum. Order placement,
   execution unlock, risk/bankroll changes and credential access have *no enum member*,
   so no job can even declare them. A name-based guard additionally rejects them.
2. **Every job type declares its required capabilities**; the registry is verified at
   import time to be a subset of Hermes' authority.
3. **Job requests are untrusted input** (they may originate from voice, UI, or Claude
   suggestions): strict schema, unknown job types and extra fields rejected.
4. **Every state change is an audit event** carrying `execution_authorized: false`.
5. **Deterministic** — no LLM decides what Hermes is allowed to do.
