"""
Shared test isolation.

Every test starts with FRIDAY's research library and knowledge vault
pointing at EMPTY temporary locations, so no test can see or touch the
operator's real ~/jarvis/library or ~/jarvis/FRIDAY-Knowledge-Vault
(found in 5B: once the real library existed, app tests picked up its
inbox schedule). Tests that need a library/vault set their own path.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_operator_data(tmp_path_factory, monkeypatch):
    sandbox = tmp_path_factory.mktemp("isolated")
    monkeypatch.setenv("JARVIS_LIBRARY_PATH", str(sandbox / "no-library"))
    monkeypatch.setenv("JARVIS_VAULT_PATH", str(sandbox / "no-vault"))
    monkeypatch.setenv("JARVIS_MARKET_DB", str(sandbox / "market" / "market_data.db"))
    monkeypatch.setenv("JARVIS_RISK_DB", str(sandbox / "risk" / "risk.db"))
    # 10C: tests run on the default limits whatever the server's .env sets (empty = default)
    for name in ("BANKROLL", "MAX_POSITION", "MAX_MARKET", "MAX_STRATEGY", "MAX_EXPOSURE", "MAX_POSITIONS",
                 "DAILY_LOSS", "MAX_DRAWDOWN", "MIN_STAKE"):
        monkeypatch.setenv(f"JARVIS_RISK_{name}", "")
    monkeypatch.delenv("JARVIS_FETCH_CONTACT", raising=False)
    # 8A: no background calibration backfill threads in app tests (tests
    # that exercise the backfill run it explicitly, in the foreground).
    monkeypatch.setenv("JARVIS_CALIBRATION_BACKFILL", "false")
