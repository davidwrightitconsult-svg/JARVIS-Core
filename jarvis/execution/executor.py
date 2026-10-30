"""EXECUTOR — places trades through the Risk Governor.

Every signal from TRIGGER is:
  1. Converted into a Proposal.
  2. Evaluated by RiskGovernor (which now enforces SENTINEL rules).
  3. If approved, sent to the Broker (PaperBroker today, live later).
  4. The result is recorded in ARBITER (win/loss) and the ledger.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from .broker import Broker, OrderRequest, OrderResult


@dataclass
class ExecutionResult:
    approved: bool
    reason: str
    order: OrderResult | None = None


class Executor:
    """Places approved trades. Never bypasses the Risk Governor."""

    def __init__(
        self,
        broker: Broker,
        governor_factory: Callable[[], Any],
        arbiter_factory: Callable[[], Any],
        settings: dict[str, Any],
    ) -> None:
        self._broker = broker
        self._governor_factory = governor_factory
        self._arbiter_factory = arbiter_factory
        self._settings = settings  # e.g. {"default_stake": 20.0, "mode": "PAPER"}

    async def execute_signal(
        self,
        market_id: str,
        token_id: str,
        outcome: str,
        strategy: str,
        side: str,
        price: float,
        stake: float | None = None,
    ) -> ExecutionResult:
        stake = float(stake if stake is not None else self._settings.get("default_stake", 20.0))

        # 1. Build the Proposal (RiskGovernor's input contract)
        try:
            from jarvis.risk import Proposal
            proposal = Proposal(
                strategy=strategy,
                market_id=market_id,
                mode=str(self._settings.get("mode", "PAPER")),
                stake=stake,
            )
        except Exception as e:
            return ExecutionResult(approved=False, reason=f"proposal build failed: {e}")

        # 2. Ask the Risk Governor
        try:
            governor = self._governor_factory()
            verdict = governor.evaluate(proposal, record=True)
        except Exception as e:
            return ExecutionResult(approved=False, reason=f"governor error: {type(e).__name__}: {e}")

        if not verdict.get("approved") and not verdict.get("verdict") in ("REDUCED", "APPROVED"):
            # Includes TERMINATED from SENTINEL/TERMINATOR
            return ExecutionResult(
                approved=False,
                reason=verdict.get("reason", "risk rejected"),
            )

        approved_stake = float(verdict.get("approved_stake") or 0.0)
        if approved_stake <= 0:
            return ExecutionResult(
                approved=False,
                reason=verdict.get("reason", "no approved stake"),
            )

        # 3. Place the order through the broker
        req = OrderRequest(
            market_id=market_id,
            token_id=token_id,
            side=side,
            outcome=outcome,
            limit_price=price,
            stake=approved_stake,
        )
        try:
            order = await self._broker.place_order(req)
        except Exception as e:
            return ExecutionResult(approved=True, reason=f"broker error: {e}", order=None)

        # 4. Record in ARBITER (win tracked when the trade closes, not opened)
        # We don't record here; the SETTLEMENT or paper engine records on close.

        return ExecutionResult(approved=True, reason="executed", order=order)


# ---- Singleton ----

_executor: Executor | None = None


def get_executor() -> Executor | None:
    return _executor


def init_executor(broker: Broker, governor_factory, arbiter_factory, settings) -> Executor:
    global _executor
    _executor = Executor(broker, governor_factory, arbiter_factory, settings)
    return _executor
