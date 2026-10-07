"""
Phase 3C — Hermes scheduler, controls, kill switch and health tests.
"""

import threading
import time

import pytest

from friday.core.health import HealthManager
from friday.core.lifecycle import LifecycleManager
from friday.core.state import StateManager
from friday.hermes import (
    MIN_INTERVAL_SECONDS,
    HermesAuthorityError,
    HermesControlError,
    HermesController,
    HermesRejectedError,
    HermesRuntime,
    HermesScheduleError,
    HermesScheduler,
    HermesServices,
    HermesState,
    HermesStore,
    JobStatus,
    JobType,
    TokenBudget,
)
from friday.hermes.scheduler import MAX_SCHEDULES


class NullLogger:
    def emit(self, event):
        return event.normalized()["event_id"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _state():
    return {"status": "ONLINE", "mode": "PAPER", "execution_locked": True, "components": {}}


@pytest.fixture
def store(tmp_path):
    s = HermesStore(tmp_path / "h.db")
    yield s
    s.close()


def _runtime(store, events=None, state=_state):
    return HermesRuntime(
        store, TokenBudget(store),
        HermesServices(state_snapshot=state, health_check=lambda: {"status": "HEALTHY"}),
        event_sink=(events.append if events is not None else None),
    )


def _status_schedule(name="status", interval=30, **extra):
    return {"name": name, "job": {"job_type": "STATUS_REPORT"}, "interval_seconds": interval, **extra}


# ---------------------------------------------------------------- validation

def test_every_job_type_has_a_minimum_interval():
    assert set(MIN_INTERVAL_SECONDS) == set(JobType)
    assert MIN_INTERVAL_SECONDS[JobType.INTELLIGENCE_ANALYSIS] >= 900
    assert MIN_INTERVAL_SECONDS[JobType.MARKET_SCAN] >= 300


@pytest.mark.parametrize("job_type,too_fast", [
    ("STATUS_REPORT", 29), ("MARKET_SCAN", 299), ("INTELLIGENCE_ANALYSIS", 899)])
def test_interval_below_minimum_rejected(store, job_type, too_fast):
    params = {"user_command": "x"} if job_type == "INTELLIGENCE_ANALYSIS" else {}
    sched = HermesScheduler(_runtime(store))
    with pytest.raises(HermesScheduleError):
        sched.add({"name": "s", "job": {"job_type": job_type, "params": params},
                   "interval_seconds": too_fast})


@pytest.mark.parametrize("name", ["PLACE_ORDER", "executeTrade", "unlock-execution"])
def test_forbidden_job_cannot_be_scheduled(store, name):
    sched = HermesScheduler(_runtime(store))
    with pytest.raises(HermesAuthorityError):
        sched.add({"name": "evil", "job": {"job_type": name}, "interval_seconds": 3600})


@pytest.mark.parametrize("bad", [
    {"name": "Bad Name!", "job": {"job_type": "STATUS_REPORT"}, "interval_seconds": 60},
    {"name": "ok", "job": {"job_type": "STATUS_REPORT"}, "interval_seconds": 60, "auto_trade": True},
    {"name": "ok", "job": {"job_type": "MARKET_SCAN", "params": {"limit": 500}}, "interval_seconds": 600},
])
def test_invalid_schedules_rejected(store, bad):
    with pytest.raises(HermesScheduleError):
        HermesScheduler(_runtime(store)).add(bad)


def test_schedule_limit(store):
    sched = HermesScheduler(_runtime(store))
    for i in range(MAX_SCHEDULES):
        sched.add(_status_schedule(name=f"s{i}"))
    with pytest.raises(HermesScheduleError):
        sched.add(_status_schedule(name="one-too-many"))


# ---------------------------------------------------------------- ticking

def test_tick_submits_due_jobs_on_interval(store):
    clock = Clock()
    rt = _runtime(store)
    sched = HermesScheduler(rt, clock=clock)
    sched.add(_status_schedule(interval=30))

    assert len(sched.tick()) == 1          # due immediately
    rt.run_pending()
    assert sched.tick() == []              # not due yet
    clock.t += 30
    assert len(sched.tick()) == 1          # due again
    job = rt.get(sched.list()[0]["last_job_id"])
    assert job.requested_by == "SCHEDULER:status"


def test_run_immediately_false_waits_one_interval(store):
    clock = Clock()
    sched = HermesScheduler(_runtime(store), clock=clock)
    sched.add(_status_schedule(interval=60), run_immediately=False)
    assert sched.tick() == []
    clock.t += 60
    assert len(sched.tick()) == 1


def test_no_pile_up_while_previous_job_pending(store):
    clock = Clock()
    rt = _runtime(store)
    sched = HermesScheduler(rt, clock=clock)
    sched.add(_status_schedule(interval=30))
    sched.tick()
    for _ in range(5):                     # worker never ran
        clock.t += 30
        assert sched.tick() == []
    assert rt.pending_count() == 1
    assert sched.list()[0]["skipped_busy"] == 5


def test_disabled_schedule_never_runs(store):
    sched = HermesScheduler(_runtime(store), clock=Clock())
    sched.add(_status_schedule(enabled=False))
    assert sched.tick() == []


def test_remove_schedule(store):
    sched = HermesScheduler(_runtime(store))
    sched.add(_status_schedule())
    assert sched.remove("status") is True
    assert sched.list() == []


# ---------------------------------------------------------------- controller

def _controller(store, events=None, states=None, tick=0.02):
    rt = _runtime(store, events)
    sched = HermesScheduler(rt, tick_seconds=tick)
    return HermesController(rt, sched, on_state_change=(states.append if states is not None else None))


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_start_runs_schedules_end_to_end(store):
    ctl = _controller(store)
    ctl.add_schedule(_status_schedule())
    ctl.start()
    try:
        assert ctl.state is HermesState.RUNNING
        assert _wait(lambda: any(r["status"] == "SUCCEEDED" for r in store.recent_jobs()))
    finally:
        ctl.stop()
    assert ctl.state is HermesState.STOPPED
    assert not ctl.runtime.running and not ctl.scheduler.running


def test_pause_holds_work_and_resume_releases_it(store):
    ctl = _controller(store)
    ctl.start()
    ctl.pause()
    assert ctl.state is HermesState.PAUSED
    job = ctl.submit({"job_type": "STATUS_REPORT"})
    time.sleep(0.2)
    assert job.status is JobStatus.PENDING     # queued, not executed
    ctl.resume()
    try:
        assert _wait(lambda: job.status is JobStatus.SUCCEEDED)
    finally:
        ctl.stop()


def test_resume_after_pause_during_long_job_keeps_a_worker(store):
    started = threading.Event()
    release = threading.Event()

    def slow_state():
        started.set()
        release.wait(2)
        return _state()

    rt = _runtime(store, state=slow_state)
    ctl = HermesController(rt, HermesScheduler(rt, tick_seconds=0.02))
    ctl.start()
    slow = ctl.submit({"job_type": "STATUS_REPORT"})
    assert started.wait(2)
    ctl.pause()                     # worker is mid-job
    ctl.resume()
    release.set()
    try:
        assert _wait(lambda: slow.status is JobStatus.SUCCEEDED)
        follow_up = ctl.submit({"job_type": "HEALTH_CHECK"})
        assert _wait(lambda: follow_up.status is JobStatus.SUCCEEDED)
        assert ctl.runtime.running
    finally:
        ctl.stop()


def test_illegal_transitions(store):
    ctl = _controller(store)
    with pytest.raises(HermesControlError):
        ctl.pause()
    with pytest.raises(HermesControlError):
        ctl.resume()
    ctl.start()
    ctl.start()  # idempotent
    ctl.pause()
    with pytest.raises(HermesControlError):
        ctl.start()
    ctl.stop()


# ---------------------------------------------------------------- kill switch

def test_kill_cancels_queue_and_refuses_work(store):
    events = []
    ctl = _controller(store, events=events)
    ctl.start()
    ctl.pause()
    queued = [ctl.submit({"job_type": "STATUS_REPORT"}) for _ in range(3)]

    cancelled = ctl.kill("operator test")

    assert cancelled == 3
    assert ctl.state is HermesState.KILLED
    assert all(j.status is JobStatus.CANCELLED for j in queued)
    assert not ctl.runtime.running and not ctl.scheduler.running
    with pytest.raises(HermesRejectedError):
        ctl.submit({"job_type": "STATUS_REPORT"})
    kill_events = [e for e in events if e.kind == "HERMES_CONTROL" and e.payload["state"] == "KILLED"]
    assert kill_events and kill_events[-1].severity == "CRITICAL"
    assert kill_events[-1].payload["execution_authorized"] is False


def test_scheduler_cannot_submit_after_kill(store):
    rt = _runtime(store)
    sched = HermesScheduler(rt, clock=Clock())
    ctl = HermesController(rt, sched)
    sched.add(_status_schedule())
    ctl.kill("test")
    assert sched.tick() == []          # intake closed; rejection audited
    assert rt.pending_count() == 0


def test_killed_requires_human_reset(store):
    ctl = _controller(store)
    ctl.kill("test")
    with pytest.raises(HermesControlError):
        ctl.start()
    with pytest.raises(HermesControlError):
        ctl.reset(confirmed_by="  ")
    ctl.reset(confirmed_by="David")
    assert ctl.state is HermesState.STOPPED
    ctl.start()
    try:
        job = ctl.submit({"job_type": "STATUS_REPORT"})
        assert _wait(lambda: job.status is JobStatus.SUCCEEDED)
    finally:
        ctl.stop()


def test_kill_does_not_touch_execution_lock(store):
    state = StateManager("PAPER")
    rt = _runtime(store, state=state.snapshot)
    ctl = HermesController(rt, HermesScheduler(rt))
    LifecycleManager(state, NullLogger(), hermes=ctl).start()
    ctl.kill("test")
    assert state.is_execution_locked() is True


# ---------------------------------------------------------------- health & lifecycle

def test_lifecycle_starts_and_stops_hermes_and_reports_health(store):
    state = StateManager("PAPER")
    states = []
    rt = _runtime(store, state=state.snapshot)
    ctl = HermesController(rt, HermesScheduler(rt, tick_seconds=0.02),
                           on_state_change=lambda s: (states.append(s),
                                                      state.set_component("hermes", s.value)))
    lifecycle = LifecycleManager(state, NullLogger(), hermes=ctl)
    health = HealthManager(state)

    lifecycle.start()
    assert state.snapshot()["components"]["hermes"] == "RUNNING"
    assert health.check()["status"] == "HEALTHY"

    ctl.pause()
    assert health.check()["status"] == "HEALTHY"      # deliberate pause is healthy

    ctl.kill("test")
    assert health.check()["status"] == "DEGRADED"     # kill is visible
    assert health.check()["execution_locked"] is True

    lifecycle.stop()
    assert state.snapshot()["components"]["hermes"] == "KILLED"
    assert [s.value for s in states] == ["RUNNING", "PAUSED", "KILLED"]


def test_lifecycle_without_hermes_is_unchanged():
    state = StateManager("PAPER")
    LifecycleManager(state, NullLogger()).start()
    assert "hermes" not in state.snapshot()["components"]
    assert HealthManager(state).check()["status"] == "HEALTHY"


def test_hermes_start_failure_does_not_block_friday(store):
    class Broken:
        def start(self):
            raise RuntimeError("boom")

        def stop(self):
            pass

        def component_status(self):
            return "STOPPED"

    state = StateManager("PAPER")
    LifecycleManager(state, NullLogger(), hermes=Broken()).start()
    snap = state.snapshot()
    assert snap["status"] == "ONLINE"
    assert snap["execution_locked"] is True
    assert "Hermes failed to start" in snap["last_error"]
