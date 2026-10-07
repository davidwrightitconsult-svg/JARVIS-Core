"""
FRIDAY paper trading (Phase 12). PAPER money only: positions are opened and
closed in the paper ledger at prices FRIDAY itself recorded, under the Risk
Governor. Nothing here can place an order anywhere.
"""

from .engine import PAPER_VERSION, Order, PaperEngine, PaperSettings
from .sources import PROMOTE_WORD, SOURCES_VERSION, BollingerResting

__all__ = ["PAPER_VERSION", "PROMOTE_WORD", "SOURCES_VERSION", "BollingerResting", "Order", "PaperEngine", "PaperSettings"]
