"""Directive Command Center — the constitutional layer for JARVIS.

Named agents:
  SENTINEL   — parses directives into hard-coded rules
  ARBITER    — tracks win/loss streaks
  COURIER    — analyzes top traders for copy-trading
  EXECUTOR   — enforces directives before every trade
  TERMINATOR — executes the kill switch protocol
"""

from .sentinel import SentinelDirectives, DirectiveRule, get_sentinel
from .arbiter import Arbiter, get_arbiter
from .courier import Courier, get_courier
from .terminator import Terminator, get_terminator

__all__ = [
    "SentinelDirectives",
    "DirectiveRule",
    "get_sentinel",
    "Arbiter",
    "get_arbiter",
    "Courier",
    "get_courier",
    "Terminator",
    "get_terminator",
]
