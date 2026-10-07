"""
Hermes bootstrap — builds the controller FRIDAY runs.

Phase 3D:
- HermesSettings read from the environment at FRIDAY start-up
  (defaults apply when unset; invalid values fail loudly)
- build_hermes(): store, budget, runtime, scheduler, controller and the
  default schedules, wired to FRIDAY's read-only state/health views,
  event log, market client and the Phase 2 Claude adapter
- A schedule that cannot be registered is recorded as a FRIDAY error;
  it never prevents FRIDAY from starting
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from friday.core.events import Event

from .budget import (
    DEFAULT_DAILY_TOKEN_LIMIT,
    DEFAULT_RESEARCH_DAILY_TOKENS,
    RESEARCH_RESERVE_PER_CALL,
    TokenBudget,
    research_day,
)
from .control import HermesController, HermesState
from .runtime import DEFAULT_TIMEOUT_SECONDS, HermesRuntime
from .scheduler import HermesScheduler
from .services import HermesServices, IntelligenceEngine, MarketDataSource
from .store import HermesStore


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class HermesSettings:
    autostart: bool = True
    daily_token_budget: int = DEFAULT_DAILY_TOKEN_LIMIT
    health_interval_seconds: int = 60       # 0 disables the schedule
    market_scan_interval_seconds: int = 900  # 0 disables the schedule
    market_scan_limit: int = 100          # rows (~50 binary markets); 6B history
    job_timeout_seconds: int = int(DEFAULT_TIMEOUT_SECONDS)
    vault_sync_interval_seconds: int = 0     # set by FRIDAY when sync is enabled
    research_inbox_interval_seconds: int = 0  # set by FRIDAY when the library exists
    research_daily_token_budget: int = DEFAULT_RESEARCH_DAILY_TOKENS  # digests only (5C)

    @classmethod
    def from_environment(cls) -> "HermesSettings":
        return cls(
            autostart=_env_bool("HERMES_AUTOSTART", True),
            daily_token_budget=_env_int("HERMES_DAILY_TOKEN_BUDGET", DEFAULT_DAILY_TOKEN_LIMIT),
            health_interval_seconds=_env_int("HERMES_HEALTH_INTERVAL_SECONDS", 60),
            market_scan_interval_seconds=_env_int("HERMES_MARKET_SCAN_INTERVAL_SECONDS", 900),
            market_scan_limit=_env_int("HERMES_MARKET_SCAN_LIMIT", 100, minimum=1),
            job_timeout_seconds=_env_int("HERMES_JOB_TIMEOUT_SECONDS",
                                         int(DEFAULT_TIMEOUT_SECONDS), minimum=10),
            research_daily_token_budget=_env_int("FRIDAY_RESEARCH_DAILY_TOKEN_BUDGET",
                                                 DEFAULT_RESEARCH_DAILY_TOKENS),
        )

    def default_schedules(self) -> list[dict[str, Any]]:
        schedules = []
        if self.health_interval_seconds:
            schedules.append({
                "name": "health",
                "job": {"job_type": "HEALTH_CHECK"},
                "interval_seconds": self.health_interval_seconds,
            })
        if self.market_scan_interval_seconds:
            schedules.append({
                "name": "market-scan",
                "job": {"job_type": "MARKET_SCAN", "params": {"limit": self.market_scan_limit}},
                "interval_seconds": self.market_scan_interval_seconds,
            })
        if self.research_inbox_interval_seconds:
            schedules.append({
                "name": "research-inbox",
                "job": {"job_type": "RESEARCH_INGEST_INBOX"},
                "interval_seconds": self.research_inbox_interval_seconds,
            })
        if self.vault_sync_interval_seconds:
            schedules.append({
                "name": "vault-sync",
                "job": {"job_type": "VAULT_SYNC"},
                "interval_seconds": self.vault_sync_interval_seconds,
            })
        return schedules


def build_hermes(
    *,
    settings: HermesSettings,
    db_path: str | Path,
    state_snapshot: Callable[[], dict[str, Any]],
    health_check: Callable[[], dict[str, Any]],
    event_sink: Callable[[Event], Any],
    set_component: Callable[[str, str], Any],
    record_error: Callable[[str], Any],
    market: MarketDataSource | None = None,
    intelligence: IntelligenceEngine | None = None,
    recorder: Any = None,
    knowledge: Any = None,
    vault_sync: Callable[[], dict[str, Any]] | None = None,
    research: Any = None,
    research_recall: Any = None,
    market_context: Any = None,
) -> HermesController:
    """
    recorder: optional KnowledgeRecorder (Phase 4B). Receives finished jobs
    (WRITE_KNOWLEDGE only) and Hermes control/refusal events.
    """
    store = HermesStore(db_path)

    def sink(event: Event) -> None:
        event_sink(event)
        if recorder is not None:
            try:
                recorder.record_event(event)
            except Exception:
                pass

    runtime = HermesRuntime(
        store,
        TokenBudget(store, daily_limit=settings.daily_token_budget),
        HermesServices(
            state_snapshot=state_snapshot,
            health_check=health_check,
            market=market,
            intelligence=intelligence,
            knowledge=knowledge,
            vault_sync=vault_sync,
            research=research,
            research_recall=research_recall,
            market_context=market_context,
        ),
        event_sink=sink,
        timeout_seconds=settings.job_timeout_seconds,
        on_job_finished=recorder.record_job if recorder is not None else None,
        research_budget=TokenBudget(store, daily_limit=settings.research_daily_token_budget,
                                    reserve_per_call=RESEARCH_RESERVE_PER_CALL, day=research_day),
    )
    controller = HermesController(
        runtime,
        HermesScheduler(runtime),
        on_state_change=lambda s: set_component("hermes", s.value),
    )
    for schedule in settings.default_schedules():
        if schedule["job"]["job_type"] == "VAULT_SYNC" and vault_sync is None:
            continue
        if schedule["job"]["job_type"] == "RESEARCH_INGEST_INBOX" and research is None:
            continue
        try:
            controller.add_schedule(schedule)
        except Exception as exc:
            record_error(f"Hermes schedule '{schedule['name']}' not registered: {exc}")
    set_component("hermes", HermesState.STOPPED.value)
    return controller
