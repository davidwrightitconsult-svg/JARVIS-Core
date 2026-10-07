from jarvis.app.risk import RiskState, size_position


def test_risk_sizing():
    s = RiskState(1, 1, 0, 0)
    stake, why = size_position(s, .5, .45)

    assert stake > 0
    assert why == "ok"


def test_drawdown_block():
    s = RiskState(.8, 1, 0, 0)
    stake, why = size_position(s, .5, .45)

    assert stake == 0
    assert why == "max drawdown breached"
