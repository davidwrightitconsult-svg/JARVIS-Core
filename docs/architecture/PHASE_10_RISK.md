# Phase 10 — Risk Governor

Survival first. Every proposed position, from any strategy in any market, must pass the Risk Governor before the paper engine (Phase 12) may open it. The governor can only refuse or reduce. It cannot originate, enlarge or place anything, and it has no network path.

| Sub-phase | Delivers |
|---|---|
| **10A** | The governor core: gate dependency, size caps, loss halts, kill switch, decision log |
| **10B** | The paper ledger the governor reads (equity, peak, day start, positions) and the read-only dashboard |
| **10C** | Stress drills, the weaknesses they exposed (fixed), and the Phase 10 close |

## 10A contract

**Inputs.** A `Proposal` (strategy, market id, stake in USD, mode, venue) and a `Portfolio` (equity, peak equity, day-start equity, open positions). "Stake" is the money at risk: the most that position can lose. A strategy that can lose more than it pays in must pass its maximum loss as the stake.

**Checks, in order.** The first failure rejects the proposal.

| # | Check | Result on failure |
|---|---|---|
| 1 | Mode is not LIVE | `LIVE_TRADING_LOCKED` |
| 2 | Kill switch is not engaged | `KILL_SWITCH` |
| 3 | The strategy is APPROVED_FOR_PAPER or FORWARD_TEST in the 9C gate; an unreadable gate fails closed | `NOT_ALLOWED_BY_GATE` / `GATE_UNAVAILABLE` |
| 4 | Strategy and market named; stake and equity positive and finite | `INVALID_PROPOSAL` |
| 5 | Equity above peak × (1 − max drawdown); a breach is persisted | `DRAWDOWN_HALT` |
| 6 | Equity above day start × (1 − daily loss limit) | `DAILY_LOSS_LIMIT` |
| 7 | Open markets below the maximum (adding to a held market is allowed) | `MAX_OPEN_POSITIONS` |
| 8 | Room under every size cap is at least the minimum stake | `NO_ROOM (<cap>)` |

**Sizing.** The stake is cut to the tightest of four caps, all fractions of current equity: per position, per market, per strategy and total exposure. The verdict is `APPROVED` if nothing was cut and `REDUCED` otherwise. The governor never approves more than was asked.

**Defaults** (`FRIDAY_RISK_*`)

| Setting | Default | Hard ceiling |
|---|---|---|
| Paper bankroll | $1,000 | — |
| Per position | 2% | 10% |
| Per market | 4% | — |
| Per strategy | 15% | — |
| Total exposure | 30% | 60% |
| Open positions | 15 | — |
| Daily loss limit | 3% | 10% |
| Drawdown halt | 10% | 25% |
| Minimum stake | $1 | — |

Limits must nest: position ≤ market ≤ total, and strategy ≤ total. A setting beyond a ceiling is refused; the app falls back to the defaults and reports the error.

**Kill switch and halts.** State lives in its own SQLite file (`FRIDAY_RISK_DB`), so it survives a restart and does not depend on any market's data. Engaging the kill switch needs nothing. Releasing it, or clearing a drawdown halt, needs the literal confirmation `RESUME`.

**Record.** Every decision is stored with each check and its detail. A dry run (`POST /api/risk/evaluate`) records nothing.

**Interfaces.** `GET /api/risk/status`, `POST /api/risk/evaluate`, `GET /api/risk/decisions`, `POST /api/risk/kill`, `POST /api/risk/resume`; `python -m friday.risk status | decisions | kill | resume`.

**Limits**
- The governor trusts the portfolio it is given. The 10B ledger supplies it.
- It does not model correlation between markets. The per-strategy and total caps are the protection against many related bets.
- It is market-agnostic by design, so it knows nothing about order books or fills.

## 10B contract

**Paper ledger** (`friday/risk/ledger.py`, version 10B.1). Paper money only, kept in the risk store so it survives a restart.

| Rule | Detail |
|---|---|
| One way in | A position opens only from a recorded governor decision that is APPROVED or REDUCED, in PAPER mode, for that decision's stake. A dry run, a rejection or LIVE cannot open one. A decision is used once. |
| Units | `units = stake / price`. Value is `units × current price`; with no current price the position is carried at cost. |
| Closing | Realises `units × exit price`. A closed position cannot close again. Cash never goes below zero. |
| Equity | Cash plus the value of open positions. The peak and the day's starting equity (UTC days) are tracked and stored. |
| Bankroll | Set once, when the ledger is first created. Changing `FRIDAY_RISK_BANKROLL` later does not rewrite the books. |

The app values open positions at FRIDAY's own last recorded bid (within the hour). `/api/risk/status` now reports the ledger's portfolio, so the daily loss limit and the drawdown halt act on real paper results.

**Dashboard** (`/`, data from `GET /api/dashboard`). Laid out after the owner's reference image: briefing, portfolio and risk on the left; the validation board, strategy library and books in the centre; market scanner, open trades, activity and profit and loss on the right.

- Read-only. The page makes one GET every 30 seconds. It has no forms or buttons, loads nothing from outside the server, and never contacts the venue: the scanner shows recorded observations.
- Every figure is recorded state or a tested verdict. A strategy shows its gate status and the evidence behind it. There are no confidence scores, sentiment gauges or status lights without a measurement behind them.
- The twelve strategies in the image are listed as "not yet registered" until Phase 11 turns each into an exact rule from the books and tests it.
- A section that fails says so; the others still render.
- The command console moved to `/console`.

**Interfaces.** `GET /api/portfolio`, `GET /api/dashboard`, `GET /api/scan?live=false`.

**Limits**
- The ledger has no caller until the Phase 12 paper engine, so the portfolio stays at the starting bankroll.
- It has no fees or slippage model of its own. The price it is given is the price it books; Phase 12 must pass the real ask on entry and the real bid on exit.
- The dashboard is served on 127.0.0.1 with no login. View it over an SSH tunnel; do not expose the port.

## 10C contract

**Drills** (`friday/risk/drills.py`, `python -m friday.risk drill`). Each runs on a temporary store with paper money and a made-up gate. The real risk store is never opened.

| Drill | What it tries | What must hold |
|---|---|---|
| Random storm | 3,000 random proposals, price moves and closes over many days, with garbage stakes, unapproved strategies and LIVE requests | No approval ever breaks a cap, exceeds what was asked, or gets past a halt |
| Everything to zero | Fill the book, then every open position goes to zero together | The loss is at most the total exposure cap; the halt survives a restart; only RESUME clears it |
| Every trade loses | Total losses, day after day | Each day stops at the daily limit plus one position; the drawdown halt ends it |
| Rush | 80 proposals at the same instant | Market, strategy, exposure and position-count caps hold |
| Broken gate | The gate raises, returns nothing, has the wrong shape, or claims live approval | Every proposal refused; LIVE stays locked |
| Kill switch | Engage, restart, six wrong release words | Refused until RESUME; an open position can still be closed |
| Unsafe settings | Twelve settings beyond the ceilings | None loads |

**What the drills found, and the fixes** (governor and ledger are now 10C.1)

| Weakness | Fix |
|---|---|
| The approved stake was rounded to the nearest cent, so it could exceed the request or a cap by up to half a cent | Stakes are whole cents, rounded down |
| A gate whose approved list arrived as one string would have matched part of a name | Only real lists of names count; any other shape fails closed |
| Deciding and opening were two steps, so simultaneous proposals could both be sized against the same room | `PaperLedger.open_checked` decides and opens under one lock. Phase 12 must use it |
| After RESUME cleared a drawdown halt, the old peak re-engaged the halt on the next proposal | Clearing a drawdown halt restarts the peak at current equity, once. Releasing only the kill switch moves nothing |

**Worst cases on the default limits** (measured by the drills)

| Event | Loss |
|---|---|
| Every open position goes to zero at once | 30% of equity (the exposure cap) |
| A day of nothing but total losses, no positions carried | 4% (limit 3% plus the trade that crosses it) |
| A run of nothing but total losses | Stopped at 11.4% below the peak |

**Limits**
- The daily loss limit and the drawdown halt stop new positions. They do not close open ones, so the exposure cap is the true worst case. An exit rule belongs to each strategy (Phase 11) and the paper engine (Phase 12).
- The entry lock covers one process. FRIDAY runs as one service; a second process writing to the same ledger is not supported.
- The drills use made-up prices. They test the limits, not any strategy.
