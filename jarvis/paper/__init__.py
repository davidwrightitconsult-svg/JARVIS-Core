"""
FRIDAY paper trading (Phase 12). PAPER money only: positions are opened and
closed in the paper ledger at prices FRIDAY itself recorded, under the Risk
Governor. Nothing here can place an order anywhere.
"""

from .capacity import CAPACITY_VERSION
from .costs import COSTS_VERSION, CostSettings
from .engine import PAPER_VERSION, Order, PaperEngine, PaperSettings
from .signals import SIGNALS_VERSION, SignalTrader
from .sources import PROMOTE_WORD, SOURCES_VERSION, BollingerResting

__all__ = ["PAPER_VERSION", "COSTS_VERSION", "CAPACITY_VERSION", "CostSettings", "PROMOTE_WORD", "SOURCES_VERSION", "BollingerResting", "SIGNALS_VERSION", "SignalTrader", "Order", "PaperEngine", "PaperSettings"]
