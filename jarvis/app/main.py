"""
FRIDAY application (FastAPI).

Phase 3D:
- Hermes is built at start-up and runs with FRIDAY's lifecycle
- Free-text commands are routed through Hermes as INTELLIGENCE_ANALYSIS
  jobs, i.e. through the Phase 2 Claude contract (trust boundary, JSON
  validation, budget, audit). The Phase 0 FridayBrain free-form path is
  retired.
- /api/hermes/* exposes status, jobs, pause/resume and the kill switch
- Execution remains locked; nothing here can place an order

Phase 4B/4C:
- Knowledge services (recorder, recall, vault sync) built from the
  environment; /api/knowledge/status and POST /api/knowledge/sync

Phase 5A:
- Private research library (outside Git): /api/research/status,
  /api/research/search; /api/books now ingests into the library (size-capped)
  instead of saving uploads inside the repository

Phase 5B:
- POST /api/research/url and /api/research/ingest-inbox run as Hermes jobs
  (hardened fetcher; audited; journalled); GET /api/research/documents;
  scheduled inbox pick-up (JARVIS_RESEARCH_INBOX_INTERVAL_SECONDS)

Phase 5C:
- Research recall: cited library passages in every Claude analysis
  (JARVIS_RESEARCH_RECALL); POST /api/research/digest runs an on-request
  digest under its own daily budget (JARVIS_RESEARCH_DAILY_TOKEN_BUDGET)
"""

import dataclasses
import os
import re
import time
import uuid
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from . import db as db_module
from .config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    MIN_LIQUIDITY,
    MODE,
)
from .db import (
    EventLogger,
    add_command,
    init_db,
    log,
    stats,
)
from .polymarket import PolyMarketClient
from .voice import synthesize
from .routers.version import router as version_router
from .routers.brain_ws import router as brain_ws_router

from jarvis.core.commands import CommandRouter, CommandType
from jarvis.core.events import Event
from jarvis.core.health import HealthManager
from jarvis.core.lifecycle import LifecycleManager
from jarvis.core.state import StateManager
from jarvis.hermes import (
    HermesControlError,
    HermesController,
    HermesRejectedError,
    HermesState,
    JobStatus,
)
from jarvis.hermes.bootstrap import HermesSettings, build_hermes
from jarvis.intelligence.claude import ClaudeIntelligence
from jarvis.knowledge.services import KnowledgeServices, build_knowledge_from_environment
from jarvis.research import DEFAULT_LIBRARY_PATH, KINDS, LibraryError, ResearchLibrary
from jarvis.research.library import MAX_FILE_BYTES, result_dict
from jarvis.research.recall import ResearchRecall
from jarvis.market.context import MarketContext
from jarvis.quant import LIBRARY, QuantEngine, QuantPipeline, UniverseRules, enabled_strategies
from jarvis.quant.calibration import DEFAULT_HORIZON, build_calibration
from jarvis.quant.fair import EdgeSettings, FairValueModel
from jarvis.quant.scorecard import build_scorecard
from jarvis.market.backfill import CalibrationBackfill
from jarvis.quant.scoring import Scorer


BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"

TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

state = StateManager(MODE)
events = EventLogger()
router = CommandRouter()
health_manager = HealthManager(state)
lifecycle = LifecycleManager(state, events)


def _default_intelligence():
    return ClaudeIntelligence(api_key=ANTHROPIC_API_KEY, model=CLAUDE_MODEL)


# Factories are module attributes so tests can substitute offline fakes.
INTELLIGENCE_FACTORY = _default_intelligence
MARKET_FACTORY = PolyMarketClient

intelligence = None
poly = None
hermes: HermesController | None = None
hermes_settings: HermesSettings | None = None
recorder = None
knowledge: KnowledgeServices = KnowledgeServices(base_status={"enabled": False, "reason": "not started"})
research: ResearchLibrary | None = None
research_recall: ResearchRecall | None = None
research_status: dict = {"initialised": False, "reason": "not started"}
market_context: MarketContext | None = None
quant: QuantEngine | None = None
quant_pipeline: QuantPipeline | None = None
calibration_backfill: CalibrationBackfill | None = None
fair_model: FairValueModel | None = None
risk_governor = None            # friday.risk.RiskGovernor (Phase 10A)
paper_ledger = None             # friday.risk.PaperLedger (Phase 10B)
paper_engine = None             # friday.paper.PaperEngine (Phase 12A)
paper_source = None             # friday.paper.BollingerResting (Phase 12B forward test)
risk_status: dict = {"enabled": False, "reason": "not started"}
quant_status: dict = {"enabled": False, "reason": "not started"}


def _research_inbox_interval() -> int:
    raw = os.getenv("JARVIS_RESEARCH_INBOX_INTERVAL_SECONDS", "").strip()
    value = int(raw) if raw else 600
    if value != 0 and value < 300:
        raise ValueError("JARVIS_RESEARCH_INBOX_INTERVAL_SECONDS must be 0 or >= 300")
    return value


def _build_research() -> None:
    """Open the private library if it exists; never create it implicitly."""
    global research, research_status
    path = os.getenv("JARVIS_LIBRARY_PATH", DEFAULT_LIBRARY_PATH).strip() or DEFAULT_LIBRARY_PATH
    try:
        lib = ResearchLibrary(path)
    except LibraryError as exc:
        research, research_status = None, {"initialised": False, "library": path, "reason": str(exc)}
        return
    if not lib.initialised:
        research, research_status = None, {
            "initialised": False, "library": str(lib.root),
            "reason": "no library yet; run: python -m jarvis.research init"}
        return
    research, research_status = lib, lib.status()


def _env_on(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


def _wire_quant_pipeline(store) -> None:
    """7B: signals after every stored snapshot + the sticky watchlist."""
    global quant_pipeline, quant_status, calibration_backfill
    quant_pipeline = None
    calibration_backfill = None
    if _env_on("JARVIS_CALIBRATION_BACKFILL") and hasattr(poly, "closed_markets"):
        calibration_backfill = CalibrationBackfill(
            poly, store, min_volume=float(os.getenv("JARVIS_CALIBRATION_MIN_VOLUME", "").strip() or 10000),
            pace_seconds=float(os.getenv("JARVIS_CALIBRATION_PACE_SECONDS", "").strip() or 0.05),
            exclude_segments=getattr(poly, "exclude_segments", ()))
    if _env_on("JARVIS_QUANT_SIGNALS"):
        try:
            strategies = enabled_strategies()
        except ValueError as exc:
            strategies = ()
            quant_status = {**quant_status, "error": str(exc)}
        quant_pipeline = QuantPipeline(
            quant, strategies,
            resolver=getattr(poly, "market_resolutions", None) if _env_on("JARVIS_QUANT_RESOLUTIONS") else None,
            note_writer=recorder.write_scorecard if recorder is not None else None,
            calibration_updater=(lambda: calibration_backfill.start_background(
                max_markets=int(os.getenv("JARVIS_CALIBRATION_BATCH", "").strip() or 300),
                deadline_seconds=900)) if calibration_backfill is not None else None,
            calibration_interval_hours=float(os.getenv("JARVIS_CALIBRATION_INTERVAL_HOURS", "").strip() or 3),
            fair_model=fair_model)
        if hasattr(poly, "observers"):
            poly.observers.append(quant_pipeline.on_run)
    hours = float(os.getenv("JARVIS_MARKET_WATCHLIST_HOURS", "").strip() or 48)
    if hasattr(poly, "tracked_source"):
        poly.tracked_source = (lambda: store.watchlist_markets(hours)) if hours > 0 else None
    quant_status = {**quant_status, "signals": quant_pipeline is not None, "watchlist_hours": hours}


def _build_risk() -> None:
    """10A: the Risk Governor. A bad setting falls back to the safe defaults and says so; a risk
    store that cannot be opened leaves the governor OFF, and with it every proposal is refused."""
    global risk_governor, risk_status, paper_ledger
    from jarvis.risk import DEFAULT_RISK_DB, PaperLedger, RiskGovernor, RiskSettings, RiskState
    risk_governor = paper_ledger = None
    try:
        settings, error = RiskSettings.from_environment(), None
    except ValueError as exc:
        settings, error = RiskSettings(), str(exc)
    try:
        risk_state = RiskState(os.getenv("JARVIS_RISK_DB", "").strip() or DEFAULT_RISK_DB)
    except Exception as exc:
        risk_status = {"enabled": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
        state.set_component("risk", "DISABLED")
        return
    risk_governor = RiskGovernor(settings, risk_state, gate_provider=lab_gate)
    try:
        paper_ledger = PaperLedger(risk_state.path, settings.bankroll)       # 10B: the accounting it reads
    except Exception as exc:
        error = f"{error + '; ' if error else ''}paper ledger: {type(exc).__name__}: {exc}"[:300]
    risk_status = {"enabled": True, "path": str(risk_state.path), **({"error": error} if error else {})}
    state.set_component("risk", "READY")


def _build_paper() -> None:
    """12A: the paper trading engine. It needs the governor, the ledger and the market store; without
    any of them it stays OFF. It checks its open positions after every stored market scan."""
    global paper_engine, paper_source
    paper_engine = paper_source = None
    _backtest_cache.pop("gate", None)           # a gate built before this start may list other forward tests
    if risk_governor is None or paper_ledger is None or quant is None:
        state.set_component("paper", "DISABLED")
        return
    from jarvis.paper import PaperEngine, PaperSettings

    def f(name, default):
        raw = os.getenv(name, "").strip()
        return float(raw) if raw else default
    d = PaperSettings()
    try:
        settings = PaperSettings(max_quote_age_minutes=f("JARVIS_PAPER_MAX_QUOTE_AGE_MINUTES", d.max_quote_age_minutes),
                                 max_spread=f("JARVIS_PAPER_MAX_SPREAD", d.max_spread))
    except ValueError:
        settings = d
    paper_engine = PaperEngine(risk_governor, paper_ledger, quant.store, settings,
                               resolver=getattr(poly, "market_resolutions", None))
    from jarvis.paper import BollingerResting
    paper_source = BollingerResting(paper_engine, quant.store, enabled=_env_on("JARVIS_PAPER_BOLLINGER"))
    if hasattr(poly, "observers"):
        poly.observers.append(paper_engine.tick)          # fills and exits first,
        poly.observers.append(paper_source.on_run)        # then new resting orders
    state.set_component("paper", "READY")


def _build_hermes():
    global hermes, hermes_settings
    try:
        hermes_settings = HermesSettings.from_environment()
        if knowledge.sync is not None:
            hermes_settings = dataclasses.replace(
                hermes_settings, vault_sync_interval_seconds=knowledge.sync_interval_seconds)
        if research is not None:
            hermes_settings = dataclasses.replace(
                hermes_settings, research_inbox_interval_seconds=_research_inbox_interval())
    except ValueError as exc:
        state.record_error(f"Hermes settings invalid; Hermes not started: {exc}")
        state.set_component("hermes", HermesState.STOPPED.value)
        hermes, hermes_settings = None, None
        return
    hermes = build_hermes(
        settings=hermes_settings,
        db_path=db_module.DB_PATH,
        state_snapshot=state.snapshot,
        health_check=health_manager.check,
        event_sink=events.emit,
        set_component=state.set_component,
        record_error=state.record_error,
        market=poly,
        intelligence=intelligence,
        recorder=recorder,
        knowledge=knowledge.recall,
        vault_sync=knowledge.sync.sync if knowledge.sync else None,
        research=research,
        research_recall=research_recall,
        market_context=market_context,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global intelligence, poly, recorder, knowledge

    init_db()
    intelligence = INTELLIGENCE_FACTORY()
    poly = MARKET_FACTORY()
    knowledge = build_knowledge_from_environment()
    recorder = knowledge.recorder
    state.set_component("knowledge", "READY" if recorder else "DISABLED")
    _build_research()
    state.set_component("research", "READY" if research else "DISABLED")
    global research_recall
    recall_on = os.getenv("JARVIS_RESEARCH_RECALL", "true").strip().lower() not in ("0", "false", "no", "off")
    research_recall = ResearchRecall(research) if (research is not None and recall_on) else None
    global market_context, quant, quant_status
    context_on = os.getenv("JARVIS_MARKET_CONTEXT", "true").strip().lower() not in ("0", "false", "no", "off")
    store = getattr(poly, "store", None)
    if store is not None:
        try:
            rules = UniverseRules.from_environment()
            quant_status = {"enabled": True}
        except ValueError as exc:
            rules = UniverseRules()           # safe defaults, and say so
            quant_status = {"enabled": True, "error": f"invalid JARVIS_QUANT_* setting: {exc}"}
        quant = QuantEngine(store, rules)
        global fair_model
        try:
            edge_settings = EdgeSettings.from_environment()
        except ValueError as exc:
            edge_settings = EdgeSettings()
            quant_status = {**quant_status, "edge_error": str(exc)}
        fair_model = FairValueModel(store, edge_settings)
        _wire_quant_pipeline(store)
    else:
        quant, quant_status = None, {"enabled": False, "reason": "market store disabled"}
    state.set_component("quant", "READY" if quant else "DISABLED")
    _build_risk()
    _build_paper()
    market_context = (MarketContext(store, engine=quant, fair_model=fair_model)
                      if (store is not None and context_on) else None)
    _build_hermes()

    lifecycle.hermes = hermes if (hermes and hermes_settings.autostart) else None
    lifecycle.start()

    state.set_component("claude", "READY" if intelligence.available else "DISABLED")
    state.set_component("polymarket", "READY")
    state.set_component("voice", "READY")

    yield

    lifecycle.stop()
    if hermes is not None:
        hermes.stop()
        hermes.runtime.store.close()


app = FastAPI(
    title="FRIDAY Autonomous Trading Platform",
    version="0.4.0",
    lifespan=lifespan,
)

app.mount(
    "/static",
    __import__("fastapi.staticfiles", fromlist=["StaticFiles"]).StaticFiles(
        directory=str(STATIC_DIR)
    ),
    name="static",
)

app.include_router(version_router)
app.include_router(brain_ws_router)

# ------------------------------------------------------------------ core


@app.get("/health")
def health():
    result = health_manager.check()
    result["live_execution"] = False
    result["claude"] = bool(intelligence and intelligence.available)
    result["hermes"] = hermes.state.value if hermes else "UNAVAILABLE"
    return result


@app.get("/api/state")
def api_state():
    return state.snapshot()


@app.get("/api/stats")
def api_stats():
    return stats()


@app.get("/api/geoblock")
def geoblock():
    return poly.geoblock()


@app.get("/api/scan")
def scan(live: bool = True):
    """
    `live=false` never contacts the venue: it ranks what is already recorded (the dashboard uses it).

    Phase 7A: the Quant Core ranks FRIDAY's latest RECORDED observations
    (the Hermes market scan keeps them fresh), so the UI's 30-second
    refresh no longer hits Polymarket or floods the history store. Only
    when the latest run is stale does it take one live snapshot.

    WATCH = eligible for strategy work (usable, fresh, deep, tight, active).
    `score` is attention (0-100), not value; `edge` is None until an edge
    has been measured (Phases 7C-9). Nothing here can trade.
    """
    if quant is None:
        return _legacy_scan()
    run = quant.store.latest_run()
    stale = run is None or (datetime.now(timezone.utc) - datetime.fromisoformat(run["observed_at"])
                            ).total_seconds() > quant.rules.max_age_seconds
    if stale and live:
        try:
            poly.snapshot(25)
        except Exception as exc:              # keep serving the last known data
            state.record_error(f"scan: live snapshot failed: {type(exc).__name__}: {str(exc)[:160]}")
    rows = []
    open_signals: dict[str, list[str]] = {}
    for sig in quant.store.signals(hours=48, open_only=True, limit=2000):
        open_signals.setdefault(sig["market_id"], []).append(f"{sig['strategy']}:{sig['direction']}")
    markets = quant.features()[:25]
    edges = {e["market_id"]: e for e in fair_model.edges(markets)} if fair_model else {}
    for m in markets:
        o = m.outcomes[0]
        e = edges.get(m.market_id)
        rows.append({
            "market_id": m.market_id, "question": m.question, "outcome": o.outcome, "token_id": o.token_id,
            "title": f"{m.question} — {o.outcome}",
            "price": o.mid, "bid": o.best_bid, "ask": o.best_ask, "spread": o.spread,
            "liquidity": o.depth_usd, "volume_24h": m.volume_24h, "flags": sorted({f for x in m.outcomes for f in x.flags}),
            "usable": all(x.usable for x in m.outcomes), "observed_at": o.observed_at,
            "change_1h": o.change_1h, "change_24h": o.change_24h,
            "signals": open_signals.get(m.market_id, []),
            "score": m.attention, "eligible": m.eligible,
            # 8B: estimated edge of the better side at the live ask (unvalidated)
            # (None until the price band has enough calibration data to back a number)
            "edge": e["edge"] if e and e["status"] in ("EDGE", "NO_EDGE") else None,
            "edge_status": e["status"] if e else None,
            "edge_side": e["best_side"] if e else None, "fair": e["fair"] if e else None,
            "reasons": m.reasons or ["eligible for strategy work (no edge measured yet)"],
            "decision": "WATCH" if m.eligible else "IGNORE",
        })
    return rows


@app.get("/api/quant/universe")
def quant_universe():
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    return {**quant.universe(), "status": quant_status}


@app.get("/api/quant/features")
def quant_features(market_id: str | None = None, eligible_only: bool = False, limit: int = 50):
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    markets = quant.features([market_id[:64]] if market_id else None)
    if eligible_only:
        markets = [m for m in markets if m.eligible]
    return {"markets": [m.as_dict() for m in markets[:max(1, min(int(limit), 500))]],
            "note": "Descriptive features of recorded data. Not signals, not edges."}


@app.get("/api/quant/strategies")
def quant_strategies(hours: float = 24):
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    counts = quant.store.signal_counts(hours=max(0.1, min(float(hours), 24 * 365)))
    active = {s.name for s in quant_pipeline.strategies} if quant_pipeline else set()
    return {"strategies": [{**s.describe(), "enabled": s.name in active,
                            "counts": counts.get(s.name, {"signals": 0, "open": 0, "up": 0, "down": 0})}
                           for s in LIBRARY],
            "pipeline": quant_pipeline.status() if quant_pipeline else {"enabled": False},
            "note": "Every strategy is an unvalidated hypothesis. Signals are not orders and carry no size."}


@app.get("/api/quant/signals")
def quant_signals(hours: float = 24, strategy: str | None = None, market_id: str | None = None,
                  open_only: bool = False, limit: int = 200):
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    return {"signals": quant.store.signals(hours=max(0.1, min(float(hours), 24 * 365)),
                                           strategy=(strategy or "").upper()[:40] or None,
                                           market_id=(market_id or "")[:64] or None,
                                           open_only=open_only, limit=limit),
            "note": "Hypotheses under test (7C scores them). Not orders; nothing is executed."}


@app.get("/api/quant/scorecard")
def quant_scorecard(days: float = 30):
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    return build_scorecard(quant.store, days=max(0.1, min(float(days), 365)))


@app.get("/api/quant/outcomes")
def quant_outcomes(days: float = 7, strategy: str | None = None, limit: int = 200):
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    return {"outcomes": quant.store.outcomes(days=max(0.1, min(float(days), 365)),
                                             strategy=(strategy or "").upper()[:40] or None,
                                             limit=max(1, min(int(limit), 5000)))}


@app.post("/api/quant/score")
def quant_score():
    """Score every due signal now (the pipeline also does this after each scan)."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    return Scorer(quant.store).score_due()


@app.post("/api/quant/scorecard/note")
def quant_scorecard_note():
    """Write today's scorecard note to the vault now."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    if recorder is None:
        raise HTTPException(status_code=503, detail="knowledge vault notes are disabled")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note = recorder.write_scorecard(build_scorecard(quant.store), day)
    if note is None:
        raise HTTPException(status_code=500, detail=f"note not written: {recorder.status().get('last_error')}")
    return {"note": note, "day": day}


@app.get("/api/quant/calibration")
def quant_calibration(horizon: float = DEFAULT_HORIZON, min_volume: float = 0, segment: str | None = None):
    """8A: how often outcomes priced at p actually happened (resolved markets)."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    seg = segment if segment in ("sports", "field", "binary") else None
    cal = build_calibration(quant.store, horizon_hours=float(horizon), min_volume=max(0.0, float(min_volume)),
                            segment=seg)
    cal["backfill"] = calibration_backfill.status() if calibration_backfill else {"enabled": False}
    return cal


@app.post("/api/market/calibration/backfill")
def market_calibration_backfill(markets: int = 300, days: float = 180):
    """Start a background backfill batch now (read-only Polymarket). 8C: `markets` (max 2000) lets
    the dataset catch up faster, e.g. after the closedTime ordering change."""
    if calibration_backfill is None:
        raise HTTPException(status_code=503, detail="calibration backfill disabled (JARVIS_CALIBRATION_BACKFILL)")
    n = max(1, min(int(markets), 20000))
    started = calibration_backfill.start_background(max_markets=n, days=max(1.0, min(float(days), 730.0)),
                                                    reanchor_markets=max(300, n),
                                                    deadline_seconds=900 if n <= 300 else 6 * 3600)
    return {"started": started, "status": calibration_backfill.status()}


@app.post("/api/market/calibration/reanchor")
def market_calibration_reanchor(markets: int = 300):
    """9B: build end-anchored calibration points (prices before the SCHEDULED end) for markets that
    lack them. Background, read-only Polymarket. `markets` max 5000."""
    if calibration_backfill is None:
        raise HTTPException(status_code=503, detail="calibration backfill disabled (JARVIS_CALIBRATION_BACKFILL)")
    n = max(1, min(int(markets), 5000))
    started = calibration_backfill.start_reanchor_background(n, 900 if n <= 300 else 7200)
    return {"started": started, "status": calibration_backfill.status()}


@app.get("/api/quant/edges")
def quant_edges(market_id: str | None = None, edges_only: bool = False, limit: int = 50):
    """8B: calibrated fair value and edge vs the live ask (estimated, unvalidated)."""
    if quant is None or fair_model is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    rows = fair_model.edges(quant.features([market_id[:64]] if market_id else None))
    if edges_only:
        rows = [r for r in rows if r["status"] == "EDGE"]
    return {"edges": rows[:max(1, min(int(limit), 500))], "model": fair_model.status(),
            "note": "Estimated from calibration of resolved markets; unvalidated until Phases 8C-9. EDGE rows "
                    "are recorded as CALIBRATION_EDGE signals and scored held to resolution (see "
                    "/api/quant/scorecard). Stake fractions are arithmetic only — FRIDAY has no execution authority."}


_backtest_cache: dict = {}
_gate_lock = threading.Lock()

@app.get("/api/lab/backtest")
def lab_backtest(horizon: float = 24, half_spread: float = 0.01, folds: int = 5, anchor: str = "end"):
    """9A: walk-forward (out-of-sample) backtest of the calibration edge. Read-only, cached 10 minutes."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    from jarvis.lab import BacktestSettings, run_backtest
    try:
        settings = BacktestSettings(horizon_hours=float(horizon), half_spread=float(half_spread), folds=int(folds),
                                    anchor=str(anchor),
                                    min_price=quant.rules.min_price, max_price=quant.rules.max_price,
                                    edge=fair_model.settings if fair_model else EdgeSettings())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    key = (settings.horizon_hours, settings.half_spread, settings.folds, settings.anchor)
    hit = _backtest_cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    result = run_backtest(quant.store, settings, frozenset(quant.rules.excluded_segments))
    _backtest_cache[key] = (time.monotonic(), result)
    return result


@app.get("/api/lab/gate")
def lab_gate():
    """9C: the validation gate - which strategies FRIDAY may PAPER-trade. Never approves live. Cached 10 minutes."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
        hit = _backtest_cache.get("gate")
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    # Only one thread runs the backtest. Concurrent callers wait and
    # then get the cached result.
    with _gate_lock:
        # Re-check inside the lock — another thread may have just finished
        hit = _backtest_cache.get("gate")
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        from jarvis.lab import BacktestSettings, build_gate
        settings = BacktestSettings(min_price=quant.rules.min_price, max_price=quant.rules.max_price,
                                    edge=fair_model.settings if fair_model else EdgeSettings())
        forward = [e for e in [paper_source.gate_entry() if paper_source is not None else None] if e]
        result = build_gate(quant.store, settings, frozenset(quant.rules.excluded_segments), forward_tests=forward)
        _backtest_cache["gate"] = (time.monotonic(), result)
        return result  

# ------------------------------------------------------------------ Phase 10A: Risk Governor

def _risk():
    if risk_governor is None:
        raise HTTPException(status_code=503, detail=risk_status.get("reason") or "risk governor disabled")
    return risk_governor


def _paper_prices() -> dict[int, float]:
    """Current value of open paper positions: FRIDAY's own last recorded BID for the instrument
    (what it could be sold for), within the last hour. No quote -> carried at cost."""
    if paper_engine is not None:
        return paper_engine.prices()
    prices: dict[int, float] = {}
    if paper_ledger is None or quant is None:
        return prices
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in paper_ledger.positions("OPEN"):
        if p["instrument"]:
            q = quant.store.quote_near(p["instrument"], now, 1.0)
            if q and q["best_bid"] is not None:
                prices[p["position_id"]] = q["best_bid"]
    return prices


def _portfolio():
    return paper_ledger.portfolio(_paper_prices()) if paper_ledger is not None else None


@app.get("/api/risk/status")
def risk_status_view():
    return {**_risk().status(_portfolio()), "store": risk_status}


@app.get("/api/portfolio")
def portfolio_view(closed: int = 20):
    """10B: the PAPER portfolio - balance, P&L, drawdown, open positions, recent closed trades."""
    if paper_ledger is None:
        raise HTTPException(status_code=503, detail=risk_status.get("reason") or "paper ledger unavailable")
    prices = _paper_prices()
    open_rows = [{**p, "price": prices.get(p["position_id"]),
                  "value": round(p["units"] * prices[p["position_id"]], 2) if p["position_id"] in prices else p["stake"]}
                 for p in paper_ledger.positions("OPEN")]
    return {**paper_ledger.summary(prices), "positions": open_rows,
            "closed": paper_ledger.positions("CLOSED", max(1, min(int(closed), 200))),
            "live_trading": "LOCKED", "execution_locked": True}


@app.get("/api/paper")
def paper_view(trades: int = 50):
    """12A: the paper trading record - by strategy, with the spreads really paid - and what is open.
    Read-only: positions are opened and closed by the engine, never through the API."""
    if paper_engine is None:
        raise HTTPException(status_code=503, detail="paper engine unavailable")
    plans = paper_engine._plans()
    prices = paper_engine.prices()
    open_rows = [{**p, "bid": prices.get(p["position_id"]), "stop_price": plans.get(p["position_id"], {}).get("stop_price"),
                  "target_price": plans.get(p["position_id"], {}).get("target_price"),
                  "expires_at": plans.get(p["position_id"], {}).get("expires_at")} for p in paper_ledger.positions("OPEN")]
    return {**paper_engine.report(), "positions": open_rows, "trades": paper_engine.trades(max(1, min(int(trades), 500))),
            "resting": paper_engine.resting("PENDING", 100),
            "forward_tests": [{**paper_source.standing(), "last_run": paper_source.last}] if paper_source is not None else []}


@app.get("/api/strategies")
def strategies_view():
    """11A: the registry of book strategies - each rule, its source and its fixed settings - and which
    of the six books FRIDAY's library holds. Registered is not tested: nothing here is approved."""
    from jarvis.strategies import books_in_library, describe
    docs = research.documents() if research is not None else []
    from jarvis.strategies import check_sources
    return {**describe(), "books": books_in_library(docs), "library_available": research is not None,
            "sources": check_sources(research)["strategies"],
            "live_trading": "LOCKED", "execution_locked": True}


@app.get("/api/dashboard")
def dashboard_data():
    """10B: everything the read-only dashboard shows, in one call. Each section stands alone: one
    that fails reports its error and the rest still render. Nothing here can change anything."""
    def part(fn):
        try:
            return fn()
        except HTTPException as exc:
            return {"error": str(exc.detail)[:200]}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def scanner():
        rows = scan(live=False)               # recorded observations only: the dashboard never calls the venue
        return {"venue": "Polymarket", "markets": len(rows),
                "observed_at": max((r["observed_at"] for r in rows), default=None),
                "rows": [{k: r[k] for k in ("market_id", "question", "outcome", "price", "change_24h", "volume_24h",
                                            "decision", "signals", "edge_status")} for r in rows[:12]]}

    def activity():
        items = []
        if hermes is not None:
            for j in hermes.runtime.store.recent_jobs(12):
                items.append({"at": j.get("finished_at") or j.get("created_at"), "type": "Job",
                              "what": str(j.get("job_type", "")).replace("_", " ").title(),
                              "result": str(j.get("status", ""))})
        if risk_governor is not None:
            for d in risk_governor.state.decisions(12):
                items.append({"at": d["at"], "type": "Risk check", "what": f"{d['strategy']} · {d['market_id']}",
                              "result": d["verdict"]})
        return sorted((i for i in items if i["at"]), key=lambda i: str(i["at"]), reverse=True)[:12]

    def library():
        docs = research_documents()
        return {"documents": len(docs), "books": [
            {"title": d["title"], "author": d["author"], "kind": d["kind"],
             "read": d["digest"]["next_section"], "sections": d["digest"]["total_sections"]} for d in docs[:12]]}

    def book_strategies():
        from jarvis.strategies import STRATEGIES, describe
        d = describe()
        from jarvis.strategies import check_sources
        checked = {r["key"]: r["status"] for r in check_sources(research)["strategies"]}
        from jarvis.lab.bookrun import stored_results
        res = stored_results(quant.store) if quant is not None else None
        return {"registered": d["registered"], "tested": len(res["strategies"]) if res else 0,
                "passed": res["passed"] if res else [], "tested_at": res["at"] if res else None, "names": [x.name for x in STRATEGIES],
                "items": [{"name": x.name, "key": x.key, "kind": x.kind, "source": x.source,
                           "source_status": checked.get(x.key)} for x in STRATEGIES],
                "note": "Registered with fixed settings and tested once on hourly history; the verdicts are "
                        "in the validation gate." if res else "Registered with fixed settings. Not yet tested."}

    health_view = part(health)
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "system": health_view, "scanner": part(scanner), "gate": part(lab_gate),
        "risk": part(risk_status_view), "portfolio": part(portfolio_view), "activity": part(activity),
        "paper": part(lambda: {k: v for k, v in paper_view(8).items() if k in ("strategies", "open", "closed", "last_tick", "refused", "resting", "forward_tests")}),
        "library": part(library),
        "book_strategies": part(book_strategies),
        "live_trading": "LOCKED", "execution_locked": True,
    }


@app.post("/api/risk/evaluate")
def risk_evaluate(body: dict):
    """Dry run: would the governor allow this proposal against a FRESH paper portfolio (or the one
    given)? Nothing is recorded and nothing is placed."""
    from jarvis.risk import Portfolio, Position, Proposal
    try:
        proposal = Proposal(strategy=str(body.get("strategy") or "")[:80], market_id=str(body.get("market_id") or "")[:80],
                            stake=float(body.get("stake") or 0), mode=str(body.get("mode") or "PAPER")[:10],
                            venue=str(body.get("venue") or "")[:40])
        pf = body.get("portfolio")
        portfolio = None if not pf else Portfolio(
            equity=float(pf["equity"]), peak_equity=float(pf.get("peak_equity", pf["equity"])),
            day_start_equity=float(pf.get("day_start_equity", pf["equity"])),
            positions=tuple(Position(str(p["market_id"]), str(p["strategy"]), float(p["stake"]))
                            for p in pf.get("positions", [])))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"bad proposal: {exc}")
    return _risk().evaluate(proposal, portfolio, record=False)


@app.get("/api/risk/decisions")
def risk_decisions(limit: int = 100, verdict: str | None = None):
    return {"decisions": _risk().state.decisions(limit, (verdict or "").upper() or None)}


@app.post("/api/risk/kill")
def risk_kill(body: dict | None = None):
    """Engage the kill switch: the governor refuses every new position until it is released."""
    return {"kill_switch": _risk().state.engage_kill_switch(str((body or {}).get("reason") or "")[:300], "api")}


@app.post("/api/risk/resume")
def risk_resume(body: dict | None = None):
    """Release the kill switch and clear a drawdown halt. Needs {"confirm": "RESUME"}."""
    try:
        return _risk().state.resume(str((body or {}).get("confirm") or ""), "api")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@app.get("/api/lab/bars")
def lab_bars():
    """11B: what hourly price history FRIDAY holds for testing the book strategies (read-only)."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    from jarvis.lab.bookbt import HOLDOUT_FRACTION, split
    stats = quant.store.bar_stats()
    ok, frozen = quant.store.bar_markets("OK"), quant.store.meta_get("book_holdout_start")
    dev, hold = ([m for m in ok if str(m["reference_time"]) < frozen], [m for m in ok if str(m["reference_time"]) >= frozen]
                 ) if frozen else split(ok)
    return {**stats, "development_markets": len(dev), "holdout_markets": len(hold), "holdout_frozen": bool(frozen),
            "holdout_fraction": HOLDOUT_FRACTION,
            "holdout_starts": frozen or (hold[0]["reference_time"] if hold else None)}


@app.get("/api/lab/book")
def lab_book():
    """11C: the stored result of the book-strategy test (read-only; the test itself is run from the CLI)."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    from jarvis.lab.bookrun import fingerprint, stored_results
    result = stored_results(quant.store)
    if not result:
        return {"tested": False, "note": "The book strategies have not been tested yet.", "execution_locked": True}
    return {"tested": True, "settings_current": result["fingerprint"] == fingerprint(), **result}


@app.get("/api/lab/inconsistencies")
def lab_inconsistencies(days: float = 30):
    """9C: recorded moments where both outcomes of a market could be bought for under $1."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    from jarvis.lab import scan_inconsistencies
    return scan_inconsistencies(quant.store, days=max(0.1, min(float(days), 365)),
                                fee=fair_model.settings.fee if fair_model else 0.0,
                                excluded=frozenset(quant.rules.excluded_segments))


@app.get("/api/lab/robustness")
def lab_robustness(half_spread: float = 0.01):
    """9B: the edge across horizons, anchors, thresholds and FRIDAY's own recorded asks. Cached 10 minutes."""
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    from jarvis.lab import BacktestSettings, run_robustness
    try:
        settings = BacktestSettings(half_spread=float(half_spread), min_price=quant.rules.min_price,
                                    max_price=quant.rules.max_price,
                                    edge=fair_model.settings if fair_model else EdgeSettings())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    key = ("robustness", settings.half_spread)
    hit = _backtest_cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    result = run_robustness(quant.store, settings, frozenset(quant.rules.excluded_segments))
    _backtest_cache[key] = (time.monotonic(), result)
    return result


@app.get("/api/quant/watchlist")
def quant_watchlist():
    if quant is None:
        raise HTTPException(status_code=503, detail=quant_status.get("reason") or "quant core disabled")
    hours = quant_status.get("watchlist_hours", 48)
    return {"hours": hours, "markets": quant.store.watchlist(hours) if hours else []}


def _legacy_scan():
    """Phase 0/6A heuristic, used only when the market store is disabled."""
    rows = []

    for s in poly.snapshots(30):
        score = 0
        reasons = []
        usable = s.get("usable", True)
        price = s.get("price")
        spread = s.get("spread")

        if usable and (s.get("liquidity") or 0) >= MIN_LIQUIDITY:
            score += 25
            reasons.append("depth")

        if usable and spread is not None and spread <= 0.02:
            score += 25
            reasons.append("tight spread")

        if usable and price is not None and 0.05 <= price <= 0.95:
            score += 10
            reasons.append("tradable probability range")

        if not usable:
            reasons.append("data quality: " + ", ".join(s.get("flags") or []))

        rows.append(
            {
                **s,
                "score": score,
                "edge": 0.0,
                "reasons": reasons,
                "decision": "WATCH" if (usable and score >= 40) else "IGNORE",
            }
        )

    rows.sort(key=lambda x: x["score"], reverse=True)

    return rows[:25]


def _market_store():
    store = getattr(poly, "store", None)
    if store is None:
        status = getattr(poly, "store_status", {}) or {}
        raise HTTPException(status_code=503, detail=status.get("reason") or "market store disabled")
    return store


@app.get("/api/market/stats")
def market_stats(days: float = 7):
    result = _market_store().stats(days=max(0.01, min(float(days), 3650)))
    result["filter"] = getattr(poly, "filter_status", None)
    result["context"] = market_context.status() if market_context else {"enabled": False}
    return result


@app.get("/api/market/markets")
def market_markets(limit: int = 200):
    return _market_store().markets(limit=limit)


@app.get("/api/market/history")
def market_history(token_id: str, hours: float = 24, limit: int = 2000):
    return {"token_id": token_id[:100],
            "rows": _market_store().history(token_id[:100], hours=max(0.1, min(float(hours), 24 * 3650)),
                                            limit=limit)}


@app.get("/api/market/levels")
def market_levels(token_id: str, observed_at: str | None = None):
    return _market_store().levels(token_id[:100], observed_at)


@app.get("/api/market/export")
def market_export(token_id: str | None = None, market_id: str | None = None, hours: float = 168,
                  format: str = "csv"):
    """Snapshot history as CSV (default) or JSON: one token, one market, or all."""
    rows = _market_store().export_rows(token_id=(token_id or "")[:100] or None,
                                       market_id=(market_id or "")[:64] or None,
                                       hours=max(0.1, min(float(hours), 24 * 3650)))
    if format.lower() == "json":
        return {"rows": rows, "count": len(rows)}
    from fastapi.responses import PlainTextResponse
    name = f"jarvis-market-{(market_id or token_id or 'all')[:24]}.csv"
    return PlainTextResponse(_market_store().to_csv(rows), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/market/context")
def market_context_preview(q: str = "", market_id: str | None = None):
    """What Claude would be given for this command / market (no Claude call)."""
    _market_store()
    if market_context is None:
        raise HTTPException(status_code=503, detail="market context disabled (JARVIS_MARKET_CONTEXT)")
    block = market_context.context_for(q[:4000], [market_id] if market_id else None)
    return block or {"markets": [], "note": "no tracked market matches"}


MARKET_ANALYSIS_BRIEF = (
    "Be compact so the reply fits the output limit: assessment at most ~150 words; at most "
    "6 evidence items, 4 hypotheses, 4 risks and 4 missing_information items; one sentence each."
)


@app.post("/api/market/analyse")
def market_analyse(body: dict):
    """
    Ask Claude (through Hermes, analysis budget) to analyse ONE tracked
    market using FRIDAY's recorded data. Advisory only; returns the job id.
    """
    h = _require_hermes()
    market_id = str(body.get("market_id") or "").strip()[:64]
    summary = _market_store().market_summary(market_id) if market_id else None
    if summary is None:
        raise HTTPException(status_code=404, detail="market not tracked in the last 24h")
    question = str(body.get("question") or "").strip()[:1000]
    command = (f"Analyse this Polymarket market using FRIDAY's recorded order-book data: "
               f"{summary['question']}" + (f" — operator's question: {question}" if question else "")
               + ". " + MARKET_ANALYSIS_BRIEF)
    try:
        job = h.submit({"job_type": "INTELLIGENCE_ANALYSIS", "requested_by": "MARKET_API",
                        "params": {"task": "MARKET_ANALYSIS", "user_command": command[:4000],
                                   "market_context": {"market_ids": [market_id]}}})
    except HermesRejectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job_id": job.id, "status": job.status.value, "market_id": market_id,
            "question": summary["question"], "execution_locked": True}


@app.get("/api/market/report")
def market_report():
    """Last market-data snapshot report: counts, batch endpoint, errors."""
    report = getattr(poly, "last_report", None)
    return report.as_dict() if report is not None else {"observed_at": None}


# ------------------------------------------------------------------ commands


def _requested_by(source: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(source or "")).upper()[:32]
    return cleaned or "UI"


def _ask_hermes(text: str, source: str) -> tuple[str, str | None]:
    """
    Route a free-text command through Hermes -> Phase 2 Claude contract.
    Returns (reply, job_id).
    """

    if hermes is None:
        return ("Hermes is unavailable, so free-text analysis is offline. "
                "Execution remains locked."), None
    if hermes.state is not HermesState.RUNNING:
        return (f"Hermes is {hermes.state.value}; free-text analysis is unavailable "
                "until it is running. Execution remains locked."), None

    try:
        job = hermes.submit({
            "job_type": "INTELLIGENCE_ANALYSIS",
            "requested_by": _requested_by(source),
            "params": {"task": "GENERAL_RESEARCH", "user_command": text[:4000]},
        })
    except HermesRejectedError as exc:
        return f"Hermes refused the request: {exc}. Execution remains locked.", None

    wait = (hermes_settings.job_timeout_seconds if hermes_settings else 120) + 5
    deadline = time.monotonic() + wait
    while job.status not in TERMINAL and time.monotonic() < deadline:
        time.sleep(0.1)

    if job.status not in TERMINAL:
        return (f"Analysis is still running (job {job.id}). "
                "Execution remains locked."), job.id
    if job.status is not JobStatus.SUCCEEDED:
        return (f"Analysis did not complete: {job.error}. No trade is implied. "
                "Execution remains locked."), job.id

    result = job.result or {}
    response = result.get("response", {})
    reply = (
        f"{response.get('assessment', '').strip()} "
        f"Recommendation: {response.get('recommendation', '').strip()} "
        f"Action: {result.get('action', 'NO_TRADE')}. Execution remains locked."
    ).strip()
    return reply, job.id


@app.post("/api/command")
def command(body: dict):
    text = str(body.get("command", ""))
    source = str(body.get("source", "typed"))

    state.record_command(text, source)

    parsed = router.route(text)
    job_id = None

    if parsed.type == CommandType.STATUS:
        hermes_state = hermes.state.value if hermes else "UNAVAILABLE"
        reply = (
            f"FRIDAY is {state.snapshot()['status']}. "
            f"Mode is {MODE}. Hermes is {hermes_state}. "
            "Execution remains locked."
        )

    elif parsed.type == CommandType.SCAN:
        reply = (
            "Market scan requested. Candidates may be observed and analysed, "
            "but no order can be placed. Execution remains locked."
        )

    elif parsed.type == CommandType.HELP:
        reply = (
            "Available commands: status, scan, help, stop. Anything else is "
            "sent to FRIDAY's intelligence engine for analysis. Trading "
            "commands are analysis-only until the execution gates are "
            "formally enabled in a later phase."
        )

    elif parsed.type == CommandType.STOP:
        lifecycle.stop()
        reply = "FRIDAY control plane stopped. Execution remains locked."

    else:
        reply, job_id = _ask_hermes(text, source)

    events.emit(
        Event(
            kind="COMMAND",
            message=text or "empty command",
            source=source.upper()[:32],
            component="COMMAND_ROUTER",
            payload={
                "command_type": parsed.type.value,
                "hermes_job_id": job_id,
                "execution_locked": True,
            },
        )
    )

    add_command(source, text, reply)

    return {
        "reply": reply,
        "command_type": parsed.type.value,
        "hermes_job_id": job_id,
        "execution_locked": True,
    }


# ------------------------------------------------------------------ hermes


def _require_hermes() -> HermesController:
    if hermes is None:
        raise HTTPException(status_code=503, detail="Hermes is unavailable")
    return hermes


def _control(action):
    try:
        action()
    except HermesControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _require_hermes().status()


@app.get("/api/knowledge/status")
def api_knowledge_status():
    return knowledge.status()


@app.post("/api/knowledge/replay")
def api_knowledge_replay(body: dict):
    """
    Re-record the notes of a finished job from its saved result (no Claude
    call, no new tokens) — e.g. after note-writing was unavailable.
    Only jobs that are allowed to write knowledge can be replayed.
    """
    from jarvis.hermes.authority import Capability
    from jarvis.hermes.jobs import JOB_CAPABILITIES, resolve_job_type
    if recorder is None:
        raise HTTPException(status_code=409, detail="note-writing is disabled")
    row = _require_hermes().runtime.store.get_job(str(body.get("job_id", ""))[:64])
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if Capability.WRITE_KNOWLEDGE not in JOB_CAPABILITIES[resolve_job_type(row["job_type"])]:
        raise HTTPException(status_code=400, detail=f"{row['job_type']} jobs do not write notes")
    before = recorder.notes_written, recorder.write_failures
    recorder.record_job({"job_id": row["id"], "job_type": row["job_type"], "status": row["status"],
                         "requested_by": f"REPLAY:{row.get('requested_by') or ''}"[:64],
                         "result": row.get("result"), "error": row.get("error")})
    return {"job_id": row["id"], "job_type": row["job_type"],
            "notes_written": recorder.notes_written - before[0],
            "write_failures": recorder.write_failures - before[1],
            "last_error": recorder.last_error}


@app.post("/api/knowledge/sync")
def api_knowledge_sync():
    """Run a vault sync now, through Hermes (same checks as the schedule)."""
    if knowledge.sync is None:
        raise HTTPException(status_code=409, detail=knowledge.status()["sync"].get("reason") or "sync disabled")
    controller = _require_hermes()
    try:
        job = controller.submit({"job_type": "VAULT_SYNC", "requested_by": "API"})
    except HermesRejectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    deadline = time.monotonic() + 90
    while job.status not in TERMINAL and time.monotonic() < deadline:
        time.sleep(0.1)
    return {**job.summary(), "result": job.result}


@app.get("/api/hermes/status")
def hermes_status():
    status = _require_hermes().status()
    status["settings"] = hermes_settings.__dict__ if hermes_settings else None
    return status


@app.get("/api/hermes/jobs")
def hermes_jobs(limit: int = 20):
    limit = max(1, min(int(limit), 100))
    return _require_hermes().runtime.store.recent_jobs(limit)


@app.get("/api/hermes/jobs/{job_id}")
def hermes_job(job_id: str):
    row = _require_hermes().runtime.store.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


@app.post("/api/hermes/jobs")
def hermes_submit(body: dict):
    controller = _require_hermes()
    try:
        job = controller.submit({
            "job_type": str(body.get("job_type", "")),
            "params": body.get("params") or {},
            "requested_by": "API",
        })
    except HermesRejectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return job.summary()


@app.post("/api/hermes/start")
def hermes_start():
    return _control(_require_hermes().start)


@app.post("/api/hermes/pause")
def hermes_pause():
    return _control(_require_hermes().pause)


@app.post("/api/hermes/resume")
def hermes_resume():
    return _control(_require_hermes().resume)


@app.post("/api/hermes/kill")
def hermes_kill(body: dict | None = None):
    reason = str((body or {}).get("reason") or "Kill switch activated via API")[:500]
    controller = _require_hermes()
    cancelled = controller.kill(reason)
    status = controller.status()
    status["cancelled_jobs"] = cancelled
    return status


@app.post("/api/hermes/reset")
def hermes_reset(body: dict | None = None):
    confirmed_by = str((body or {}).get("confirmed_by") or "").strip()[:64]
    controller = _require_hermes()
    return _control(lambda: controller.reset(confirmed_by=confirmed_by))


# ------------------------------------------------------------------ misc


@app.post("/api/speak")
def speak(body: dict):
    return synthesize(body.get("text", "")) or {"enabled": False}


@app.get("/api/research/status")
def research_status_api():
    status = dict(research.status() if research else research_status)
    status["recall"] = research_recall.status() if research_recall else {"enabled": False}
    if hermes is not None and hermes.runtime.research_budget is not None:
        status["research_budget"] = hermes.runtime.research_budget.status()
    return status


@app.post("/api/research/digest")
def research_digest(body: dict):
    """On-request Claude digest of the next sections of one document."""
    if research is None:
        raise HTTPException(status_code=503, detail=research_status.get("reason"))
    params = {"doc_id": str(body.get("doc_id", ""))[:32], "sections": int(body.get("sections") or 6)}
    if body.get("start") is not None:
        params["start"] = int(body["start"])
    wait = max(930, (hermes_settings.job_timeout_seconds if hermes_settings else 120) + 30)
    return _run_job({"job_type": "RESEARCH_DIGEST", "requested_by": "API", "params": params}, wait)


@app.get("/api/research/search")
def research_search(q: str, limit: int = 5, kind: str | None = None):
    """Local full-text search of the private library (no network, no Claude)."""
    if research is None:
        raise HTTPException(status_code=503, detail=research_status.get("reason"))
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(KINDS)}")
    return {"query": q[:500], "hits": research.search(q[:500], limit=limit, kind=kind),
            "trust": "UNTRUSTED_DATA"}


def _run_job(request: dict, wait_seconds: float) -> dict:
    controller = _require_hermes()
    try:
        job = controller.submit(request)
    except HermesRejectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    deadline = time.monotonic() + wait_seconds
    while job.status not in TERMINAL and time.monotonic() < deadline:
        time.sleep(0.1)
    return {**job.summary(), "result": job.result}


@app.post("/api/research/url")
def research_url(body: dict):
    """Fetch ONE https URL (article or paper) into the library via Hermes."""
    if research is None:
        raise HTTPException(status_code=503, detail=research_status.get("reason"))
    params = {"url": str(body.get("url", ""))[:2000], "kind": str(body.get("kind") or "article")}
    if body.get("title"):
        params["title"] = str(body["title"])[:200]
    if body.get("refresh"):
        params["refresh"] = True
    return _run_job({"job_type": "RESEARCH_INGEST_URL", "requested_by": "API", "params": params}, 90)


@app.post("/api/research/ingest-inbox")
def research_ingest_inbox():
    if research is None:
        raise HTTPException(status_code=503, detail=research_status.get("reason"))
    return _run_job({"job_type": "RESEARCH_INGEST_INBOX", "requested_by": "API"}, 300)


@app.get("/api/research/documents")
def research_documents():
    if research is None:
        raise HTTPException(status_code=503, detail=research_status.get("reason"))
    docs = []
    for d in research.documents():
        row = {k: d[k] for k in ("doc_id", "title", "author", "kind", "format", "source", "pages",
                                 "chunks", "withheld_chunks", "tags", "added_at")}
        progress = research.digest_progress(d["doc_id"])
        row["digest"] = {k: progress[k] for k in ("next_section", "total_sections", "tokens_used")}
        docs.append(row)
    return docs


@app.post("/api/books")
def books(file: UploadFile = File(...), kind: str = "book"):
    """Ingest an uploaded book/paper/note into the private library (not Git)."""
    if research is None:
        raise HTTPException(status_code=503, detail=research_status.get("reason"))
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(KINDS)}")
    name = Path(file.filename or "upload").name
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")[:120] or "upload"
    staging = research.inbox / f".upload-{uuid.uuid4().hex}"
    target = staging / safe
    staging.mkdir(parents=True)
    try:
        size = 0
        with target.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="file too large")
                out.write(chunk)
        result = research.add(target, kind=kind)
    finally:
        for leftover in staging.glob("*"):
            leftover.unlink()
        staging.rmdir()
    log("BOOK", f"{result.status} {safe}", {"doc_id": result.doc_id, "reason": result.reason})
    return result_dict(result)


@app.get("/", response_class=HTMLResponse)
def home():
    """10B: the read-only dashboard. It shows recorded state and tested verdicts; it changes nothing."""
    return (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


@app.get("/console", response_class=HTMLResponse)
def console():
    """The Phase 0 command console (typed commands to FRIDAY)."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
