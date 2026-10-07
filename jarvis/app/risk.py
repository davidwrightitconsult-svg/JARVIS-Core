from dataclasses import dataclass
from .config import RISK_PER_TRADE, MAX_DRAWDOWN, MAX_OPEN_EXPOSURE
@dataclass
class RiskState:
    bankroll:float
    peak:float
    open_exposure:float=0.0
    daily_pnl:float=0.0
    def drawdown(self): return max(0,(self.peak-self.bankroll)/max(self.peak,1e-9))
def size_position(state, entry, stop):
    if entry<=0 or stop<=0 or entry==stop: return 0,'invalid levels'
    if state.drawdown()>=MAX_DRAWDOWN: return 0,'max drawdown breached'
    if state.open_exposure>=MAX_OPEN_EXPOSURE*state.bankroll: return 0,'exposure cap reached'
    risk=state.bankroll*RISK_PER_TRADE
    stake=min(risk/abs(entry-stop),MAX_OPEN_EXPOSURE*state.bankroll-state.open_exposure)
    return max(0,stake),'ok'
