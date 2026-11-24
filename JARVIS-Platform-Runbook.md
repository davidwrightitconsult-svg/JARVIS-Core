=====================================================================
JARVIS AUTONOMOUS TRADING PLATFORM
OPERATIONS RUNBOOK & ARCHITECTURE DOCUMENTATION
Version: 1.0
Date: 2026-11-24
Owner: King David
Server: jarvis020 (Ubuntu 24.04 LTS, 192.168.100.161)
=====================================================================

TABLE OF CONTENTS
-----------------
1. Executive Summary
2. Architecture Overview
3. Component Reference
4. Repository Structure
5. Prerequisites & Environment
6. Starting the Platform (Step by Step)
7. Starting the Frontend (Windows PowerShell Tunnel)
8. Stopping the Platform
9. Verifying Health After Startup
10. Common Errors & Fixes
11. Database Reference
12. Security & Safety Model
13. Logs & Monitoring
14. Emergency Procedures
15. Appendix: Environment Variables

=====================================================================
1. EXECUTIVE SUMMARY
=====================================================================

JARVIS is an autonomous prediction-market trading platform targeting
Polymarket. It combines:
  - A deterministic quant engine (technical strategies)
  - A Claude-backed AI research pipeline (RESEARCH_FORECAST)
  - A strict risk governor with hard caps
  - A human-readable knowledge vault (Obsidian-compatible)
  - A React HUD (A.L.F.R.E.D.)

The platform is PAPER-ONLY. Live trading is locked behind the
JARVIS_EXECUTE gate and requires manual owner approval.

=====================================================================
2. ARCHITECTURE OVERVIEW
=====================================================================

    [Polymarket API / WebSocket]
                |
                v
    [Market Data Layer]  -->  [SQLite: market_data.db (1.1 GB)]
                |                        |
                v                        v
    [Quant Engine]              [Knowledge Vault]
                |                (Obsidian markdown)
                v
    [Signal Generator]  -->  [SignalTrader]
                |                        |
                v                        v
    [Validation Gate]  <-->  [Risk Governor]
                |                        |
                v                        v
    [Research Pipeline]          [Paper Engine]
    (Claude / Hermes)                    |
                |                        v
                v                [Paper Portfolio]
    [Hermes Scheduler]           (SQLite: jarvis.db)
                |
                v
    [React UI: A.L.F.R.E.D.]  <--  [FastAPI backend :8000]

=====================================================================
3. COMPONENT REFERENCE
=====================================================================

COMPONENT             | PURPOSE                                    | FILE/FOLDER
----------------------|--------------------------------------------|-------------------
Market Data Layer     | Pulls order books from Polymarket          | jarvis/market/
Quant Engine          | Evaluates technical strategies             | jarvis/quant/
Validation Gate       | Approves/rejects strategies for paper      | jarvis/lab/gate.py
Risk Governor         | Enforces drawdown & position limits        | jarvis/risk/
Paper Engine          | Simulates fills & P&L                      | jarvis/paper/engine.py
SignalTrader          | Places trades for approved signals         | jarvis/paper/signals.py
PairTrader            | Arbitrage pairs (Phase 15)                 | jarvis/paper/arbitrage.py
Execution Gate        | Master switch for real trades              | jarvis/paper/executor.py
Hermes Scheduler      | Background job runner (scan, research)     | jarvis/hermes/
Research Pipeline     | Ingests URLs, digests with Claude          | jarvis/research/
Intelligence Layer    | Claude API adapter + prompt safety         | jarvis/intelligence/
Knowledge Vault       | Obsidian markdown sync (human-readable)    | ~/friday/FRIDAY-Knowledge-Vault/
Directives Layer      | Sentinel / Arbiter / Terminator safety     | jarvis/directives/
API Server            | FastAPI + Uvicorn on :8000                 | jarvis/app/main.py
React UI              | A.L.F.R.E.D. dashboard on :5173            | ~/JARVIS-Platform/JARVIS-UI/

=====================================================================
4. REPOSITORY STRUCTURE
=====================================================================

~/JARVIS-Platform/
├── JARVIS-Core/               (Python backend)
│   ├── jarvis/
│   │   ├── app/               (FastAPI routes, main.py)
│   │   ├── core/              (Config, DB path, settings)
│   │   ├── market/            (Polymarket store, snapshots)
│   │   ├── quant/             (Strategies, pipeline, scoring)
│   │   ├── lab/               (Gate, backtest, calibration)
│   │   ├── risk/              (Governor, ledger, portfolio)
│   │   ├── paper/             (Engine, signals, fills, costs)
│   │   ├── hermes/            (Scheduler, jobs)
│   │   ├── research/          (Ingest, digest, library)
│   │   ├── intelligence/      (Claude adapter, prompts)
│   │   ├── directives/        (Sentinel/Arbiter/Terminator)
│   │   ├── trigger/           (Real-time price feeds)
│   │   ├── strategies/        (Registry, indicators)
│   │   └── tests/
│   ├── data/
│   │   └── jarvis.db          (Legacy DB - decisions, hermes_jobs)
│   ├── deploy/
│   │   └── systemd/jarvis.service.example
│   ├── .env                   (Secrets, keys)
│   └── requirements.txt
│
├── JARVIS-UI/                 (React frontend)
│   ├── src/
│   │   ├── api/               (client.ts, ws)
│   │   ├── components/        (HUD, Orb, Panels)
│   │   ├── pages/
│   │   └── App.tsx
│   ├── package.json
│   └── vite.config.ts
│
└── FRIDAY-Knowledge-Vault/    (Obsidian vault)

DATABASES (critical - TWO separate files):
  ~/jarvis/data/market_data.db   (1.1 GB) - market data, signals, outcomes
  ~/JARVIS-Platform/JARVIS-Core/data/jarvis.db  - decisions, hermes jobs

=====================================================================
5. PREREQUISITES & ENVIRONMENT
=====================================================================

OPERATING SYSTEM
  Ubuntu 24.04 LTS (physical server at 192.168.100.161)

RUNTIME
  Python 3.14 (virtual environment at .venv/)
  Node.js 20+ (for React UI)
  SQLite 3.x

SYSTEMD SERVICE
  /etc/systemd/system/jarvis.service
  Enabled on boot (survives reboots and power outages)

ENVIRONMENT FILE
  /home/kingdavid/JARVIS-Platform/JARVIS-Core/.env

=====================================================================
6. STARTING THE PLATFORM (STEP BY STEP)
=====================================================================

There are TWO parts to start: backend (Python) and frontend (React).

STEP 1: SSH INTO THE SERVER
  From any terminal (Windows PowerShell, Mac, Linux):
    ssh kingdavid@192.168.100.161

STEP 2: START THE BACKEND (Python API)
  The backend runs as a systemd service. It auto-starts on boot.
  To check if it's running:
    sudo systemctl status jarvis

  To start it manually:
    sudo systemctl start jarvis

  To restart it (e.g., after editing code or .env):
    sudo systemctl restart jarvis

  EXPECTED OUTPUT (from `status`):
    Active: active (running)
    Main PID: <pid> (python)
    Uvicorn running on http://0.0.0.0:8000

STEP 3: VERIFY BACKEND IS ALIVE
  From the SSH session:
    curl -s http://localhost:8000/api/state | python3 -m json.tool | head -20

  EXPECTED OUTPUT:
    {
        "status": "ONLINE",
        "mode": "PAPER",
        "execution_locked": true,
        "components": {
            "claude": "READY",
            "polymarket": "READY",
            ...
        }
    }

STEP 4: START THE FRONTEND (React UI)
  From a DIFFERENT SSH session (leave the first one alone):
    cd ~/JARVIS-Platform/JARVIS-UI
    npm run dev

  EXPECTED OUTPUT:
    VITE v5.x.x  ready in <ms>
    ➜  Local:   http://localhost:5173/

  LEAVE THIS TERMINAL OPEN. Closing it stops the frontend.

=====================================================================
7. STARTING THE FRONTEND (WINDOWS POWERSHELL TUNNEL)
=====================================================================

The frontend runs on the Linux server but is accessed from your
Windows browser via an SSH port-forward tunnel.

STEP 1: OPEN POWERSHELL ON WINDOWS
  Press Windows key, type "PowerShell", hit Enter.

STEP 2: RUN THE TUNNEL COMMAND
    ssh -L 127.0.0.1:8000:localhost:8000 -L 127.0.0.1:5173:localhost:5173 kingdavid@192.168.100.161

  Enter the password when prompted.

  The window will appear to "freeze" at the Ubuntu prompt.
  THIS IS CORRECT. Do not close this window.

STEP 3: OPEN THE UI IN YOUR BROWSER
  Launch Chrome, Edge, or Firefox and navigate to:
    http://localhost:5173

  The A.L.F.R.E.D. dashboard should load.
  The status indicator should show:
    STREAM ● LIVE     STATUS ● HEALTHY

STEP 4: ACCESS THE API DOCS (SWAGGER)
  In another browser tab, navigate to:
    http://localhost:8000/docs

  The interactive API explorer should load.

IMPORTANT NOTES:
  - If you close PowerShell, the UI becomes unreachable.
  - If the UI shows "Could not reach JARVIS backend," the tunnel
    has dropped. Kill PowerShell, reopen it, rerun the SSH command.
  - If you accidentally opened a PowerShell window that shows a
    Linux prompt but not the tunnel, you must close it and start
    a NEW PowerShell window to run the tunnel command.

=====================================================================
8. STOPPING THE PLATFORM
=====================================================================

STOP BACKEND:
    sudo systemctl stop jarvis

STOP FRONTEND:
    In the terminal running `npm run dev`, press Ctrl+C.

STOP TUNNEL:
    Close the PowerShell window running the ssh -L command.

STOP EVERYTHING AT ONCE:
    sudo systemctl stop jarvis
    Then close the npm terminal and the PowerShell tunnel.

=====================================================================
9. VERIFYING HEALTH AFTER STARTUP
=====================================================================

The following four checks confirm the platform is operational:

CHECK 1 - SERVICE ACTIVE
    sudo systemctl status jarvis | head -5
  EXPECTED: "Active: active (running)"

CHECK 2 - API RESPONSIVE
    curl -s http://localhost:8000/api/state | python3 -m json.tool | head -5
  EXPECTED: {"status": "ONLINE", "mode": "PAPER", ...}

CHECK 3 - SCANNER RUNNING
    sqlite3 ~/jarvis/data/market_data.db "SELECT MAX(observed_at) FROM snapshots;"
  EXPECTED: A timestamp within the last 15 minutes.

CHECK 4 - GATE STATS
    curl -s http://localhost:8000/api/lab/gate | python3 -m json.tool | head -20
  EXPECTED: List of strategies with their approval status.

=====================================================================
10. COMMON ERRORS & FIXES
=====================================================================

ERROR: "Could not reach JARVIS backend" in UI
  CAUSE: The SSH tunnel dropped, or PowerShell was closed.
  FIX:
    1. Close all PowerShell windows.
    2. Open a NEW PowerShell window.
    3. Rerun the ssh -L command.
    4. Refresh the browser tab.

ERROR: "bind: Address already in use" on port 8000 or 5173
  CAUSE: A previous SSH tunnel or a zombie process is still holding
         the port.
  FIX (Windows PowerShell):
    Get-Process ssh | Stop-Process -Force
    net stop winnat
    net start winnat
    (then rerun the ssh -L command)

ERROR: "This site can't be reached" at localhost:8000/docs
  CAUSE: The backend service is not running, or the tunnel is dead.
  FIX:
    1. ssh kingdavid@192.168.100.161
    2. sudo systemctl status jarvis
    3. If not running: sudo systemctl restart jarvis
    4. In PowerShell, rerun the ssh -L command.

ERROR: "TabError: inconsistent use of tabs and spaces"
  CAUSE: A Python file was edited with mixed indentation.
  FIX:
    1. Restore from Git: git checkout -- <file>
    2. Or open in nano -l <file> and manually fix indentation
       (use SPACES ONLY, never Tab).

ERROR: "invalid x-api-key" (Claude 401)
  CAUSE: ANTHROPIC_API_KEY in .env is expired, malformed, or has
         a trailing character (e.g. `>`).
  FIX:
    1. Go to platform.claude.com/settings/keys
    2. Create a new key (must start with "sk-ant-api03-")
    3. Update .env: ANTHROPIC_API_KEY=<new-key>
    4. Restart: sudo systemctl restart jarvis

ERROR: "Workspace not found" (Claude 404)
  CAUSE: A stale ANTHROPIC_WORKSPACE_ID in .env, or a mismatch
         between workspace and key.
  FIX:
    Remove the ANTHROPIC_WORKSPACE_ID line from .env entirely.
    A workspace-scoped key does not need it.
    Restart the service.

ERROR: SignalTrader returns "considered: 0"
  CAUSE: The signal is older than 30 minutes, or already traded,
         or the token_id does not match the outcomes table.
  FIX:
    See Section 11 for DB inspection queries.

ERROR: Database "locked"
  CAUSE: SQLite allows only one writer at a time. The running
         backend has a write lock.
  FIX:
    sudo systemctl stop jarvis
    <do your DB operations>
    sudo systemctl start jarvis

=====================================================================
11. DATABASE REFERENCE
=====================================================================

DATABASE 1: MARKET DATA
  Path: ~/jarvis/data/market_data.db
  Size: ~1.1 GB (grows over time)
  Key tables:
    markets         - All Polymarket markets seen
    outcomes        - Yes/No token mapping per market
    snapshots       - Order book snapshots every scan
    signals         - All generated signals
    signal_outcomes - Scored signal results
    runs            - Scan run metadata
    paper_signal_orders - Traded signal records
    paper_refusals  - Rejected signals (with reason)

DATABASE 2: OPERATIONAL
  Path: ~/JARVIS-Platform/JARVIS-Core/data/jarvis.db
  Key tables:
    decisions       - Risk governor decision log
    trades          - Closed paper trades
    hermes_jobs     - Background job queue
    commands        - API command audit log
    events          - System event log

USEFUL QUERIES:

  Recent scans:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT run_id, observed_at, markets FROM runs ORDER BY run_id DESC LIMIT 5;"

  Open positions:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT position_id, strategy, market_id, stake FROM paper_positions WHERE status='OPEN';"

  RESEARCH_FORECAST signals:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT signal_id, observed_at, direction FROM signals WHERE strategy='RESEARCH_FORECAST';"

  Closed RESEARCH_FORECAST trades:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT position_id, pnl, close_reason FROM paper_positions WHERE strategy='RESEARCH_FORECAST' AND status='CLOSED';"

=====================================================================
12. SECURITY & SAFETY MODEL
=====================================================================

LAYER 1 - LIVE TRADING LOCK
  env: JARVIS_EXECUTE (default: false)
  The platform cannot execute real orders unless this is "true".
  Even then, the API token must be valid AND the Risk Governor
  must approve each trade.

LAYER 2 - RISK GOVERNOR
  Enforces:
    - Max 2% of bankroll per position
    - Max 4% per market
    - Max 15% per strategy
    - Max 30% total exposure
    - Max 15 open positions
    - Daily loss limit: 3%
    - Max drawdown: 10% (halts trading)

LAYER 3 - VALIDATION GATE
  Every strategy must pass a minimum sample size test:
    - 100 closed trades
    - Over at least 7 days
    - With positive avg_net after fees
  Until then, the strategy is NOT_APPROVED and cannot trade.

LAYER 4 - DIRECTIVES (SENTINEL/ARBITER/TERMINATOR)
  - Sentinel: Tracks consecutive losses
  - Arbiter: Decides if a soft pause or full terminate is needed
  - Terminator: Kills the platform if losses exceed thresholds

LAYER 5 - API AUTH
  env: JARVIS_API_TOKEN
  If set, all POST/PUT/DELETE requests require the token.
  If unset, writes are allowed but flagged with X-JARVIS-Auth: disabled.
  There is NO localhost exemption.

LAYER 6 - EXECUTION GATE
  env: JARVIS_EXECUTE_STRATEGIES
  Whitelist of strategies permitted to place trades.
  Empty by default.

=====================================================================
13. LOGS & MONITORING
=====================================================================

LIVE LOGS (systemd)
    sudo journalctl -u jarvis -f

FILTERED LOGS (scan activity only)
    sudo journalctl -u jarvis -f | grep -i "scan"

HERMES ACTIVITY
    sudo journalctl -u jarvis -f | grep -i "hermes"

AI RESEARCH ACTIVITY
    sudo journalctl -u jarvis -f | grep -i "research"

VAULT JOURNAL (human-readable, daily)
    cat ~/friday/FRIDAY-Knowledge-Vault/FRIDAY/Journal/$(date +%Y-%m-%d).md

RESEARCH DIGESTS
    ls -lt ~/friday/FRIDAY-Knowledge-Vault/FRIDAY/Research/

=====================================================================
14. EMERGENCY PROCEDURES
=====================================================================

EMERGENCY 1: "Something is wrong, stop everything NOW"
    sudo systemctl stop jarvis
    pkill -f "npm run dev"   # stops React dev server
    # Close the PowerShell tunnel window.

EMERGENCY 2: "The AI is placing bad trades"
    sudo systemctl stop jarvis
    nano ~/JARVIS-Platform/JARVIS-Core/.env
    # Set: JARVIS_PAPER_SIGNALS=false
    # Set: JARVIS_EXECUTE=false
    sudo systemctl start jarvis

EMERGENCY 3: "Disk is filling up"
    du -sh ~/jarvis/data/market_data.db
    # If large, consider pruning old snapshots:
    sqlite3 ~/jarvis/data/market_data.db \
      "DELETE FROM snapshots WHERE observed_at < date('now', '-30 days');"
    sqlite3 ~/jarvis/data/market_data.db "VACUUM;"

EMERGENCY 4: "Server won't boot"
    # systemd will auto-restart the service.
    # SSH in and check:
    sudo systemctl status jarvis
    sudo journalctl -u jarvis -n 50 --no-pager

EMERGENCY 5: "API key expired"
    # Follow the fix in Section 10 under "invalid x-api-key".

=====================================================================
15. APPENDIX: ENVIRONMENT VARIABLES
=====================================================================

File: /home/kingdavid/JARVIS-Platform/JARVIS-Core/.env

CRITICAL:
  ANTHROPIC_API_KEY=sk-ant-api03-...     # Claude AI (required for research)
  JARVIS_EXECUTE=false                    # Master switch (KEEP FALSE until proven)
  JARVIS_PAPER_SIGNALS=true               # Enables SignalTrader
  JARVIS_PAPER_BOLLINGER=false            # Legacy (keep off)
  JARVIS_PAPER_ARB=false                  # Arbitrage (keep off)

RISK:
  JARVIS_RISK_BANKROLL=1000
  JARVIS_RISK_MAX_POSITION=0.02
  JARVIS_RISK_MAX_DRAWDOWN=0.10
  JARVIS_RISK_DAILY_LOSS=0.03

EXECUTION:
  JARVIS_EXECUTE_STRATEGIES=RESEARCH_FORECAST
  JARVIS_EXECUTE_FORWARD_STAKE=10

PATHS:
  JARVIS_LIBRARY_PATH=/home/kingdavid/friday/library
  JARVIS_VAULT_PATH=/home/kingdavid/friday/FRIDAY-Knowledge-Vault
  DB_PATH=data/jarvis.db
  JARVIS_MARKET_DB=~/jarvis/data/market_data.db

OPTIONAL:
  JARVIS_API_TOKEN=<16+ char secret>      # Enables write auth
  ELEVENLABS_API_KEY=sk_...               # Voice synthesis
  ELEVENLABS_VOICE_ID=...                 # Voice profile

=====================================================================
END OF DOCUMENT
==========================================================================================================================================
JARVIS AUTONOMOUS TRADING PLATFORM
OPERATIONS RUNBOOK & ARCHITECTURE DOCUMENTATION
Version: 1.0
Date: 2026-11-24
Owner: King David
Server: jarvis020 (Ubuntu 24.04 LTS, 192.168.100.161)
=====================================================================

TABLE OF CONTENTS
-----------------
1. Executive Summary
2. Architecture Overview
3. Component Reference
4. Repository Structure
5. Prerequisites & Environment
6. Starting the Platform (Step by Step)
7. Starting the Frontend (Windows PowerShell Tunnel)
8. Stopping the Platform
9. Verifying Health After Startup
10. Common Errors & Fixes
11. Database Reference
12. Security & Safety Model
13. Logs & Monitoring
14. Emergency Procedures
15. Appendix: Environment Variables

=====================================================================
1. EXECUTIVE SUMMARY
=====================================================================

JARVIS is an autonomous prediction-market trading platform targeting
Polymarket. It combines:
  - A deterministic quant engine (technical strategies)
  - A Claude-backed AI research pipeline (RESEARCH_FORECAST)
  - A strict risk governor with hard caps
  - A human-readable knowledge vault (Obsidian-compatible)
  - A React HUD (A.L.F.R.E.D.)

The platform is PAPER-ONLY. Live trading is locked behind the
JARVIS_EXECUTE gate and requires manual owner approval.

=====================================================================
2. ARCHITECTURE OVERVIEW
=====================================================================

    [Polymarket API / WebSocket]
                |
                v
    [Market Data Layer]  -->  [SQLite: market_data.db (1.1 GB)]
                |                        |
                v                        v
    [Quant Engine]              [Knowledge Vault]
                |                (Obsidian markdown)
                v
    [Signal Generator]  -->  [SignalTrader]
                |                        |
                v                        v
    [Validation Gate]  <-->  [Risk Governor]
                |                        |
                v                        v
    [Research Pipeline]          [Paper Engine]
    (Claude / Hermes)                    |
                |                        v
                v                [Paper Portfolio]
    [Hermes Scheduler]           (SQLite: jarvis.db)
                |
                v
    [React UI: A.L.F.R.E.D.]  <--  [FastAPI backend :8000]

=====================================================================
3. COMPONENT REFERENCE
=====================================================================

COMPONENT             | PURPOSE                                    | FILE/FOLDER
----------------------|--------------------------------------------|-------------------
Market Data Layer     | Pulls order books from Polymarket          | jarvis/market/
Quant Engine          | Evaluates technical strategies             | jarvis/quant/
Validation Gate       | Approves/rejects strategies for paper      | jarvis/lab/gate.py
Risk Governor         | Enforces drawdown & position limits        | jarvis/risk/
Paper Engine          | Simulates fills & P&L                      | jarvis/paper/engine.py
SignalTrader          | Places trades for approved signals         | jarvis/paper/signals.py
PairTrader            | Arbitrage pairs (Phase 15)                 | jarvis/paper/arbitrage.py
Execution Gate        | Master switch for real trades              | jarvis/paper/executor.py
Hermes Scheduler      | Background job runner (scan, research)     | jarvis/hermes/
Research Pipeline     | Ingests URLs, digests with Claude          | jarvis/research/
Intelligence Layer    | Claude API adapter + prompt safety         | jarvis/intelligence/
Knowledge Vault       | Obsidian markdown sync (human-readable)    | ~/friday/FRIDAY-Knowledge-Vault/
Directives Layer      | Sentinel / Arbiter / Terminator safety     | jarvis/directives/
API Server            | FastAPI + Uvicorn on :8000                 | jarvis/app/main.py
React UI              | A.L.F.R.E.D. dashboard on :5173            | ~/JARVIS-Platform/JARVIS-UI/

=====================================================================
4. REPOSITORY STRUCTURE
=====================================================================

~/JARVIS-Platform/
├── JARVIS-Core/               (Python backend)
│   ├── jarvis/
│   │   ├── app/               (FastAPI routes, main.py)
│   │   ├── core/              (Config, DB path, settings)
│   │   ├── market/            (Polymarket store, snapshots)
│   │   ├── quant/             (Strategies, pipeline, scoring)
│   │   ├── lab/               (Gate, backtest, calibration)
│   │   ├── risk/              (Governor, ledger, portfolio)
│   │   ├── paper/             (Engine, signals, fills, costs)
│   │   ├── hermes/            (Scheduler, jobs)
│   │   ├── research/          (Ingest, digest, library)
│   │   ├── intelligence/      (Claude adapter, prompts)
│   │   ├── directives/        (Sentinel/Arbiter/Terminator)
│   │   ├── trigger/           (Real-time price feeds)
│   │   ├── strategies/        (Registry, indicators)
│   │   └── tests/
│   ├── data/
│   │   └── jarvis.db          (Legacy DB - decisions, hermes_jobs)
│   ├── deploy/
│   │   └── systemd/jarvis.service.example
│   ├── .env                   (Secrets, keys)
│   └── requirements.txt
│
├── JARVIS-UI/                 (React frontend)
│   ├── src/
│   │   ├── api/               (client.ts, ws)
│   │   ├── components/        (HUD, Orb, Panels)
│   │   ├── pages/
│   │   └── App.tsx
│   ├── package.json
│   └── vite.config.ts
│
└── FRIDAY-Knowledge-Vault/    (Obsidian vault)

DATABASES (critical - TWO separate files):
  ~/jarvis/data/market_data.db   (1.1 GB) - market data, signals, outcomes
  ~/JARVIS-Platform/JARVIS-Core/data/jarvis.db  - decisions, hermes jobs

=====================================================================
5. PREREQUISITES & ENVIRONMENT
=====================================================================

OPERATING SYSTEM
  Ubuntu 24.04 LTS (physical server at 192.168.100.161)

RUNTIME
  Python 3.14 (virtual environment at .venv/)
  Node.js 20+ (for React UI)
  SQLite 3.x

SYSTEMD SERVICE
  /etc/systemd/system/jarvis.service
  Enabled on boot (survives reboots and power outages)

ENVIRONMENT FILE
  /home/kingdavid/JARVIS-Platform/JARVIS-Core/.env

=====================================================================
6. STARTING THE PLATFORM (STEP BY STEP)
=====================================================================

There are TWO parts to start: backend (Python) and frontend (React).

STEP 1: SSH INTO THE SERVER
  From any terminal (Windows PowerShell, Mac, Linux):
    ssh kingdavid@192.168.100.161

STEP 2: START THE BACKEND (Python API)
  The backend runs as a systemd service. It auto-starts on boot.
  To check if it's running:
    sudo systemctl status jarvis

  To start it manually:
    sudo systemctl start jarvis

  To restart it (e.g., after editing code or .env):
    sudo systemctl restart jarvis

  EXPECTED OUTPUT (from `status`):
    Active: active (running)
    Main PID: <pid> (python)
    Uvicorn running on http://0.0.0.0:8000

STEP 3: VERIFY BACKEND IS ALIVE
  From the SSH session:
    curl -s http://localhost:8000/api/state | python3 -m json.tool | head -20

  EXPECTED OUTPUT:
    {
        "status": "ONLINE",
        "mode": "PAPER",
        "execution_locked": true,
        "components": {
            "claude": "READY",
            "polymarket": "READY",
            ...
        }
    }

STEP 4: START THE FRONTEND (React UI)
  From a DIFFERENT SSH session (leave the first one alone):
    cd ~/JARVIS-Platform/JARVIS-UI
    npm run dev

  EXPECTED OUTPUT:
    VITE v5.x.x  ready in <ms>
    ➜  Local:   http://localhost:5173/

  LEAVE THIS TERMINAL OPEN. Closing it stops the frontend.

=====================================================================
7. STARTING THE FRONTEND (WINDOWS POWERSHELL TUNNEL)
=====================================================================

The frontend runs on the Linux server but is accessed from your
Windows browser via an SSH port-forward tunnel.

STEP 1: OPEN POWERSHELL ON WINDOWS
  Press Windows key, type "PowerShell", hit Enter.

STEP 2: RUN THE TUNNEL COMMAND
    ssh -L 127.0.0.1:8000:localhost:8000 -L 127.0.0.1:5173:localhost:5173 kingdavid@192.168.100.161

  Enter the password when prompted.

  The window will appear to "freeze" at the Ubuntu prompt.
  THIS IS CORRECT. Do not close this window.

STEP 3: OPEN THE UI IN YOUR BROWSER
  Launch Chrome, Edge, or Firefox and navigate to:
    http://localhost:5173

  The A.L.F.R.E.D. dashboard should load.
  The status indicator should show:
    STREAM ● LIVE     STATUS ● HEALTHY

STEP 4: ACCESS THE API DOCS (SWAGGER)
  In another browser tab, navigate to:
    http://localhost:8000/docs

  The interactive API explorer should load.

IMPORTANT NOTES:
  - If you close PowerShell, the UI becomes unreachable.
  - If the UI shows "Could not reach JARVIS backend," the tunnel
    has dropped. Kill PowerShell, reopen it, rerun the SSH command.
  - If you accidentally opened a PowerShell window that shows a
    Linux prompt but not the tunnel, you must close it and start
    a NEW PowerShell window to run the tunnel command.

=====================================================================
8. STOPPING THE PLATFORM
=====================================================================

STOP BACKEND:
    sudo systemctl stop jarvis

STOP FRONTEND:
    In the terminal running `npm run dev`, press Ctrl+C.

STOP TUNNEL:
    Close the PowerShell window running the ssh -L command.

STOP EVERYTHING AT ONCE:
    sudo systemctl stop jarvis
    Then close the npm terminal and the PowerShell tunnel.

=====================================================================
9. VERIFYING HEALTH AFTER STARTUP
=====================================================================

The following four checks confirm the platform is operational:

CHECK 1 - SERVICE ACTIVE
    sudo systemctl status jarvis | head -5
  EXPECTED: "Active: active (running)"

CHECK 2 - API RESPONSIVE
    curl -s http://localhost:8000/api/state | python3 -m json.tool | head -5
  EXPECTED: {"status": "ONLINE", "mode": "PAPER", ...}

CHECK 3 - SCANNER RUNNING
    sqlite3 ~/jarvis/data/market_data.db "SELECT MAX(observed_at) FROM snapshots;"
  EXPECTED: A timestamp within the last 15 minutes.

CHECK 4 - GATE STATS
    curl -s http://localhost:8000/api/lab/gate | python3 -m json.tool | head -20
  EXPECTED: List of strategies with their approval status.

=====================================================================
10. COMMON ERRORS & FIXES
=====================================================================

ERROR: "Could not reach JARVIS backend" in UI
  CAUSE: The SSH tunnel dropped, or PowerShell was closed.
  FIX:
    1. Close all PowerShell windows.
    2. Open a NEW PowerShell window.
    3. Rerun the ssh -L command.
    4. Refresh the browser tab.

ERROR: "bind: Address already in use" on port 8000 or 5173
  CAUSE: A previous SSH tunnel or a zombie process is still holding
         the port.
  FIX (Windows PowerShell):
    Get-Process ssh | Stop-Process -Force
    net stop winnat
    net start winnat
    (then rerun the ssh -L command)

ERROR: "This site can't be reached" at localhost:8000/docs
  CAUSE: The backend service is not running, or the tunnel is dead.
  FIX:
    1. ssh kingdavid@192.168.100.161
    2. sudo systemctl status jarvis
    3. If not running: sudo systemctl restart jarvis
    4. In PowerShell, rerun the ssh -L command.

ERROR: "TabError: inconsistent use of tabs and spaces"
  CAUSE: A Python file was edited with mixed indentation.
  FIX:
    1. Restore from Git: git checkout -- <file>
    2. Or open in nano -l <file> and manually fix indentation
       (use SPACES ONLY, never Tab).

ERROR: "invalid x-api-key" (Claude 401)
  CAUSE: ANTHROPIC_API_KEY in .env is expired, malformed, or has
         a trailing character (e.g. `>`).
  FIX:
    1. Go to platform.claude.com/settings/keys
    2. Create a new key (must start with "sk-ant-api03-")
    3. Update .env: ANTHROPIC_API_KEY=<new-key>
    4. Restart: sudo systemctl restart jarvis

ERROR: "Workspace not found" (Claude 404)
  CAUSE: A stale ANTHROPIC_WORKSPACE_ID in .env, or a mismatch
         between workspace and key.
  FIX:
    Remove the ANTHROPIC_WORKSPACE_ID line from .env entirely.
    A workspace-scoped key does not need it.
    Restart the service.

ERROR: SignalTrader returns "considered: 0"
  CAUSE: The signal is older than 30 minutes, or already traded,
         or the token_id does not match the outcomes table.
  FIX:
    See Section 11 for DB inspection queries.

ERROR: Database "locked"
  CAUSE: SQLite allows only one writer at a time. The running
         backend has a write lock.
  FIX:
    sudo systemctl stop jarvis
    <do your DB operations>
    sudo systemctl start jarvis

=====================================================================
11. DATABASE REFERENCE
=====================================================================

DATABASE 1: MARKET DATA
  Path: ~/jarvis/data/market_data.db
  Size: ~1.1 GB (grows over time)
  Key tables:
    markets         - All Polymarket markets seen
    outcomes        - Yes/No token mapping per market
    snapshots       - Order book snapshots every scan
    signals         - All generated signals
    signal_outcomes - Scored signal results
    runs            - Scan run metadata
    paper_signal_orders - Traded signal records
    paper_refusals  - Rejected signals (with reason)

DATABASE 2: OPERATIONAL
  Path: ~/JARVIS-Platform/JARVIS-Core/data/jarvis.db
  Key tables:
    decisions       - Risk governor decision log
    trades          - Closed paper trades
    hermes_jobs     - Background job queue
    commands        - API command audit log
    events          - System event log

USEFUL QUERIES:

  Recent scans:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT run_id, observed_at, markets FROM runs ORDER BY run_id DESC LIMIT 5;"

  Open positions:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT position_id, strategy, market_id, stake FROM paper_positions WHERE status='OPEN';"

  RESEARCH_FORECAST signals:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT signal_id, observed_at, direction FROM signals WHERE strategy='RESEARCH_FORECAST';"

  Closed RESEARCH_FORECAST trades:
    sqlite3 ~/jarvis/data/market_data.db \
      "SELECT position_id, pnl, close_reason FROM paper_positions WHERE strategy='RESEARCH_FORECAST' AND status='CLOSED';"

=====================================================================
12. SECURITY & SAFETY MODEL
=====================================================================

LAYER 1 - LIVE TRADING LOCK
  env: JARVIS_EXECUTE (default: false)
  The platform cannot execute real orders unless this is "true".
  Even then, the API token must be valid AND the Risk Governor
  must approve each trade.

LAYER 2 - RISK GOVERNOR
  Enforces:
    - Max 2% of bankroll per position
    - Max 4% per market
    - Max 15% per strategy
    - Max 30% total exposure
    - Max 15 open positions
    - Daily loss limit: 3%
    - Max drawdown: 10% (halts trading)

LAYER 3 - VALIDATION GATE
  Every strategy must pass a minimum sample size test:
    - 100 closed trades
    - Over at least 7 days
    - With positive avg_net after fees
  Until then, the strategy is NOT_APPROVED and cannot trade.

LAYER 4 - DIRECTIVES (SENTINEL/ARBITER/TERMINATOR)
  - Sentinel: Tracks consecutive losses
  - Arbiter: Decides if a soft pause or full terminate is needed
  - Terminator: Kills the platform if losses exceed thresholds

LAYER 5 - API AUTH
  env: JARVIS_API_TOKEN
  If set, all POST/PUT/DELETE requests require the token.
  If unset, writes are allowed but flagged with X-JARVIS-Auth: disabled.
  There is NO localhost exemption.

LAYER 6 - EXECUTION GATE
  env: JARVIS_EXECUTE_STRATEGIES
  Whitelist of strategies permitted to place trades.
  Empty by default.

=====================================================================
13. LOGS & MONITORING
=====================================================================

LIVE LOGS (systemd)
    sudo journalctl -u jarvis -f

FILTERED LOGS (scan activity only)
    sudo journalctl -u jarvis -f | grep -i "scan"

HERMES ACTIVITY
    sudo journalctl -u jarvis -f | grep -i "hermes"

AI RESEARCH ACTIVITY
    sudo journalctl -u jarvis -f | grep -i "research"

VAULT JOURNAL (human-readable, daily)
    cat ~/friday/FRIDAY-Knowledge-Vault/FRIDAY/Journal/$(date +%Y-%m-%d).md

RESEARCH DIGESTS
    ls -lt ~/friday/FRIDAY-Knowledge-Vault/FRIDAY/Research/

=====================================================================
14. EMERGENCY PROCEDURES
=====================================================================

EMERGENCY 1: "Something is wrong, stop everything NOW"
    sudo systemctl stop jarvis
    pkill -f "npm run dev"   # stops React dev server
    # Close the PowerShell tunnel window.

EMERGENCY 2: "The AI is placing bad trades"
    sudo systemctl stop jarvis
    nano ~/JARVIS-Platform/JARVIS-Core/.env
    # Set: JARVIS_PAPER_SIGNALS=false
    # Set: JARVIS_EXECUTE=false
    sudo systemctl start jarvis

EMERGENCY 3: "Disk is filling up"
    du -sh ~/jarvis/data/market_data.db
    # If large, consider pruning old snapshots:
    sqlite3 ~/jarvis/data/market_data.db \
      "DELETE FROM snapshots WHERE observed_at < date('now', '-30 days');"
    sqlite3 ~/jarvis/data/market_data.db "VACUUM;"

EMERGENCY 4: "Server won't boot"
    # systemd will auto-restart the service.
    # SSH in and check:
    sudo systemctl status jarvis
    sudo journalctl -u jarvis -n 50 --no-pager

EMERGENCY 5: "API key expired"
    # Follow the fix in Section 10 under "invalid x-api-key".

=====================================================================
15. APPENDIX: ENVIRONMENT VARIABLES
=====================================================================

File: /home/kingdavid/JARVIS-Platform/JARVIS-Core/.env

CRITICAL:
  ANTHROPIC_API_KEY=sk-ant-api03-...     # Claude AI (required for research)
  JARVIS_EXECUTE=false                    # Master switch (KEEP FALSE until proven)
  JARVIS_PAPER_SIGNALS=true               # Enables SignalTrader
  JARVIS_PAPER_BOLLINGER=false            # Legacy (keep off)
  JARVIS_PAPER_ARB=false                  # Arbitrage (keep off)

RISK:
  JARVIS_RISK_BANKROLL=1000
  JARVIS_RISK_MAX_POSITION=0.02
  JARVIS_RISK_MAX_DRAWDOWN=0.10
  JARVIS_RISK_DAILY_LOSS=0.03

EXECUTION:
  JARVIS_EXECUTE_STRATEGIES=RESEARCH_FORECAST
  JARVIS_EXECUTE_FORWARD_STAKE=10

PATHS:
  JARVIS_LIBRARY_PATH=/home/kingdavid/friday/library
  JARVIS_VAULT_PATH=/home/kingdavid/friday/FRIDAY-Knowledge-Vault
  DB_PATH=data/jarvis.db
  JARVIS_MARKET_DB=~/jarvis/data/market_data.db

OPTIONAL:
  JARVIS_API_TOKEN=<16+ char secret>      # Enables write auth
  ELEVENLABS_API_KEY=sk_...               # Voice synthesis
  ELEVENLABS_VOICE_ID=...                 # Voice profile

=====================================================================
END OF DOCUMENT
=====================================================================
