from jarvis.core.state import StateManager


def test_initial_execution_lock():
    state = StateManager("PAPER")
    snapshot = state.snapshot()

    assert snapshot["mode"] == "PAPER"
    assert snapshot["execution_locked"] is True


def test_start_and_stop():
    state = StateManager("PAPER")

    state.start()
    assert state.snapshot()["status"] == "ONLINE"

    state.stop()
    assert state.snapshot()["status"] == "STOPPED"
