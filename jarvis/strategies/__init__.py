"""
FRIDAY book strategies (Phase 11): exact rules from the trading books, with
fixed settings, registered before any test. Registration approves nothing.
"""

from .registry import (BOOKS, LONG, LOOKBACK, REGISTRY, REGISTRY_VERSION, SHORT, STRATEGIES, Exit, Strategy,
                       books_in_library, check_sources, describe, should_exit, signal, signals)

__all__ = ["BOOKS", "LONG", "LOOKBACK", "REGISTRY", "REGISTRY_VERSION", "SHORT", "STRATEGIES", "Exit", "Strategy",
           "books_in_library", "check_sources", "describe", "should_exit", "signal", "signals"]
