"""
Hermes daily Claude token budget.

Phase 3B:
- A hard daily ceiling on Claude tokens (input + output), per UTC day
- Before each Claude call Hermes requires a per-call reserve to be
  available, so a single call cannot overshoot the ceiling by much
- Usage is persisted, so a restart does not reset the day's spend
- Exhaustion is a non-retryable refusal, logged with its reason

The budget limits cost. It has nothing to do with trading authority.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

from .store import HermesStore

DEFAULT_DAILY_TOKEN_LIMIT = 250_000
# Phase 3D: ~4.7k input + 3,000 output. Phase 4C recall measured 6.2k input
# live; Phase 5C research recall adds up to ~0.9k -> 12k covers a full call.
DEFAULT_RESERVE_PER_CALL = 12_000

# Phase 5C: digests draw on their OWN daily budget (key "research:<day>"),
# so research can never starve live analysis. A digest section is ~12k
# chars of text (~3.5k tokens) + prompt + up to 3k output.
DEFAULT_RESEARCH_DAILY_TOKENS = 200_000
RESEARCH_RESERVE_PER_CALL = 14_000


def research_day() -> str:
    return "research:" + _utc_day()


class BudgetExceededError(RuntimeError):
    """Claude budget for the day is exhausted (never retried)."""


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TokenBudget:
    def __init__(
        self,
        store: HermesStore,
        daily_limit: int = DEFAULT_DAILY_TOKEN_LIMIT,
        reserve_per_call: int = DEFAULT_RESERVE_PER_CALL,
        day: Callable[[], str] = _utc_day,
    ):
        if daily_limit < 0 or reserve_per_call < 1:
            raise ValueError("daily_limit must be >= 0 and reserve_per_call >= 1")
        self.store = store
        self.daily_limit = int(daily_limit)
        self.reserve_per_call = int(reserve_per_call)
        self._day = day
        self._lock = threading.RLock()

    def used_today(self) -> int:
        u = self.store.usage(self._day())
        return u["input_tokens"] + u["output_tokens"]

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used_today())

    def ensure_available(self) -> None:
        with self._lock:
            remaining = self.remaining()
            if remaining < self.reserve_per_call:
                raise BudgetExceededError(
                    f"Daily Claude token budget exhausted: {remaining} of "
                    f"{self.daily_limit} tokens remaining; "
                    f"{self.reserve_per_call} required per call."
                )

    def record(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.store.add_usage(self._day(), input_tokens, output_tokens)

    def status(self) -> dict:
        day = self._day()
        usage = self.store.usage(day)
        used = usage["input_tokens"] + usage["output_tokens"]
        return {
            "day": day,
            "daily_limit": self.daily_limit,
            "used": used,
            "remaining": max(0, self.daily_limit - used),
            "calls": usage["calls"],
            "reserve_per_call": self.reserve_per_call,
        }
