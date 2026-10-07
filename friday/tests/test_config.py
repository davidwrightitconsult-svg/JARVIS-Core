from friday.core.config import Settings


def test_default_settings(monkeypatch):
    for name in [
        "MODE",
        "STARTING_BANKROLL",
        "TARGET_BANKROLL",
        "RISK_PER_TRADE",
        "MAX_DAILY_LOSS",
        "MAX_DRAWDOWN",
        "MAX_OPEN_EXPOSURE",
    ]:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment()

    assert settings.mode == "PAPER"
    assert settings.starting_bankroll == 1.0
    assert settings.target_bankroll == 3300000.0
