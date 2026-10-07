"""
FRIDAY Quant Core (Phase 7). Deterministic features, universe rules and
(from 7B) candidate signals computed from FRIDAY's own recorded market data.
No execution authority: nothing here can place, size or approve a position.
"""

from .engine import QuantEngine
from .pipeline import QuantPipeline
from .strategies import LIBRARY, STRATEGIES_VERSION, Strategy, enabled_strategies, evaluate_market
from .features import QUANT_VERSION, MarketFeatures, TokenFeatures, attention_score, market_features
from .universe import UniverseRules, evaluate

__all__ = ["LIBRARY", "QUANT_VERSION", "STRATEGIES_VERSION", "MarketFeatures", "QuantEngine", "QuantPipeline",
           "Strategy", "TokenFeatures", "UniverseRules", "attention_score", "enabled_strategies", "evaluate",
           "evaluate_market", "market_features"]
