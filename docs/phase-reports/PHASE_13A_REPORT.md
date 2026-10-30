# Phase 13A — Real-time TRIGGER (paper only)

## What it does
Listens to Polymarket's public CLOB market channel and wakes the paper engine the moment a price it
cares about moves, instead of at the next 15-minute scan. Off by default (`JARVIS_TRIGGER=false`).

| Event | Effect |
|---|---|
| Any valid top-of-book quote | Cached in memory; `PaperEngine` uses it **only if fresher** than the recorded quote |
| Held position / resting order token moves ≥ 0.5¢ (2 s cooldown) | `PaperEngine.tick()` runs now: stops, targets, resting fills |
| Watchlist token moves ≥ 2¢ (30 s cooldown) | Counted only (`watch_moves`). No entries are created here |
| `market_resolved` | Recorded as a resolution (`source="ws"`); held positions settle at their payoff |
| Feed silent 60 s / socket drops | Reconnect with jittered backoff; quotes age out; FRIDAY falls back to recorded quotes |

## What it can not do
No sizing, approval or order placement. It never imports the Risk Governor. Entries still come only
from strategy sources through `PaperLedger.open_checked`. LIVE stays locked. Exits never need the
governor, so TRIGGER keeps serving them while the platform is TERMINATED.

## Endpoint
`wss://ws-subscriptions-clob.polymarket.com/ws/market` (public, no key). This is the venue
py-clob-client trades on. **Not** `api.polymarket.us`, a different exchange with a different API.

## Also fixed
`RiskGovernor.evaluate` returned a dict with no `"verdict"` key for TERMINATED / SOFT PAUSE, so
`PaperLedger.open_checked` raised `KeyError: 'verdict'` exactly when the kill switch was engaged.
It now returns a normal REJECTED decision.

## Operate
- `GET /api/trigger/status`: connected, tokens watched, signals, paper ticks caused.
- Env: `JARVIS_TRIGGER*` in `.env.example`.
- Check after enabling: `connected: true`, `watching > 0`, `last_frame_age_s` small.
