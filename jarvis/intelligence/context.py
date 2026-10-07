"""
FRIDAY Intelligence Context.

Phase 2C-2:
- Structured intelligence context
- Centralized prompt architecture version
- Explicit trust boundaries
- No execution authority
- No credential material

The prompt architecture version is imported from prompts.py so there
is one authoritative version identifier.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jarvis.intelligence.prompts import PROMPT_ARCHITECTURE_VERSION


class IntelligenceContext(BaseModel):
    """
    Structured context supplied to the intelligence engine.

    The model contains observations and constraints for analysis.

    It does NOT contain:
    - API credentials
    - wallet private keys
    - signing keys
    - execution commands
    - authorization tokens
    """

    model_config = ConfigDict(extra="forbid")

    user_command: str = ""

    system_state: dict[str, Any] = Field(
        default_factory=dict
    )

    market_context: dict[str, Any] = Field(
        default_factory=dict
    )

    research_context: dict[str, Any] = Field(
        default_factory=dict
    )

    portfolio_context: dict[str, Any] = Field(
        default_factory=dict
    )

    strategy_context: dict[str, Any] = Field(
        default_factory=dict
    )

    risk_context: dict[str, Any] = Field(
        default_factory=dict
    )

    def safe_payload(self) -> dict[str, Any]:
        """
        Return the context intended for Claude.

        This is DATA supplied to the intelligence engine.

        The returned structure deliberately contains no API keys,
        wallet credentials, private signing material, or execution
        authorization mechanisms.
        """

        return {
            "user_command": self.user_command,
            "system_state": self.system_state,
            "market_context": self.market_context,
            "research_context": self.research_context,
            "portfolio_context": self.portfolio_context,
            "strategy_context": self.strategy_context,
            "risk_context": self.risk_context,
        }

    def prompt_payload(self) -> dict[str, Any]:
        """
        Return a clearly labelled payload for prompt construction.

        The prompt architecture version comes from the authoritative
        PROMPT_ARCHITECTURE_VERSION constant in prompts.py.

        The trust label is descriptive metadata only. It does NOT
        grant authority to the contained data.
        """

        return {
            "context_type": "JARVIS_INTELLIGENCE_DATA",
            "prompt_architecture_version": PROMPT_ARCHITECTURE_VERSION,
            "instruction_authority": "NONE",
            "execution_authority": False,
            "data": self.safe_payload(),
        }
