"""
FRIDAY Validation Lab (Phase 9). Tests FRIDAY's own models and strategies
against data they have not seen, before anything may depend on them.
Read-only over the recorded data; no network; no execution authority.
"""

from .backtest import BACKTEST_VERSION, BacktestSettings, render_text, run_backtest
from .gate import GATE_VERSION, allowed, build_gate, render_gate
from .inconsistency import INCONSISTENCY_VERSION, render_inconsistencies, scan_inconsistencies
from .robustness import ROBUSTNESS_VERSION, render_robustness, run_robustness

__all__ = ["BACKTEST_VERSION", "GATE_VERSION", "INCONSISTENCY_VERSION", "ROBUSTNESS_VERSION", "BacktestSettings",
           "allowed", "build_gate", "render_gate", "render_inconsistencies", "render_robustness", "render_text",
           "run_backtest", "run_robustness", "scan_inconsistencies"]
