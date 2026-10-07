"""
Phase 3C — Hermes soak run (~70 s, no Claude calls).

Runs only with FRIDAY_SOAK=1 (set by scripts/validate_phase_3c.sh).
Uses FRIDAY's real StateManager, HealthManager and LifecycleManager, and
the real read-only Polymarket client for one market scan. Exercises
real scheduler timing, pause/resume, the kill switch and health.
Hermes state goes to a temporary database, not FRIDAY's live DB.
"""

import os
import time

import pytest

from friday.core.health import HealthManager
from friday.core.lifecycle import LifecycleManager
from friday.core.state import StateManager
from friday.hermes import (
    HermesController, HermesRuntime, HermesScheduler, HermesServices,
    HermesState, HermesStore, TokenBudget,
)

pytestmark = pytest.mark.skipif(os.getenv("FRIDAY_SOAK") != "1", reason="set FRIDAY_SOAK=1")


class ListLogger:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)
        return event.normalized()["event_id"]


def test_hermes_soak(tmp_path):
    from friday.app.polymarket import PolyMarketClient

    state = StateManager("PAPER")
    health = HealthManager(state)
    log = ListLogger()
    store = HermesStore(tmp_path / "soak.db")
    runtime = HermesRuntime(
        store, TokenBudget(store, daily_limit=0),  # zero budget: Claude impossible
        HermesServices(state_snapshot=state.snapshot, health_check=health.check,
                       market=PolyMarketClient()),
        event_sink=log.emit, timeout_seconds=60,
    )
    controller = HermesController(
        runtime, HermesScheduler(runtime, tick_seconds=0.5),
        on_state_change=lambda s: state.set_component("hermes", s.value),
    )
    controller.add_schedule({"name": "status", "job": {"job_type": "STATUS_REPORT"}, "interval_seconds": 30})
    controller.add_schedule({"name": "health", "job": {"job_type": "HEALTH_CHECK"}, "interval_seconds": 30})
    controller.add_schedule({"name": "scan", "job": {"job_type": "MARKET_SCAN", "params": {"limit": 5}},
                             "interval_seconds": 300})

    lifecycle = LifecycleManager(state, log, hermes=controller)
    lifecycle.start()
    assert health.check()["status"] == "HEALTHY"

    time.sleep(62)                                   # t=0, 30, 60 for 30 s schedules

    controller.pause()
    before = len(store.recent_jobs(100))
    time.sleep(3)
    held = len(store.recent_jobs(100)) == before
    controller.resume()
    time.sleep(1)
    cancelled = controller.kill("Phase 3C soak complete")
    health_after_kill = health.check()
    lifecycle.stop()

    jobs = store.recent_jobs(100)
    by_type = {}
    for j in jobs:
        by_type.setdefault(j["job_type"], []).append(j["status"])
    print("\n[SOAK] jobs:", {k: f"{v.count('SUCCEEDED')}/{len(v)} ok" for k, v in by_type.items()})
    print(f"[SOAK] pause held={held} killed_cancelled={cancelled} "
          f"health_after_kill={health_after_kill['status']} "
          f"exec_locked={health_after_kill['execution_locked']}")
    scan = next((j for j in jobs if j["job_type"] == "MARKET_SCAN"), None)
    if scan:
        print(f"[SOAK] market scan: {scan['status']} "
              f"markets={(scan.get('result') or {}).get('count')} error={scan.get('error')}")

    assert by_type["STATUS_REPORT"].count("SUCCEEDED") >= 3
    assert by_type["HEALTH_CHECK"].count("SUCCEEDED") >= 3
    assert scan is not None and scan["status"] == "SUCCEEDED", scan
    assert held
    assert controller.state is HermesState.KILLED
    assert health_after_kill["status"] == "DEGRADED"
    assert health_after_kill["execution_locked"] is True
    assert state.is_execution_locked() is True
    assert all(j["execution_authorized"] is False for j in jobs)
    assert runtime.budget.status()["calls"] == 0     # no Claude calls
    store.close()
