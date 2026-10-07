"""
Validation boundary for Claude intelligence responses.
"""

import json

from pydantic import ValidationError

from .schemas import IntelligenceAction, IntelligenceResponse


class IntelligenceValidationError(ValueError):
    """Raised when Claude output cannot satisfy the contract."""


def parse_intelligence_response(
    raw: str | dict,
) -> IntelligenceResponse:
    """
    Parse and validate Claude output.

    Invalid output is rejected rather than trusted.
    """

    try:
        if isinstance(raw, str):
            payload = json.loads(raw)
        else:
            payload = raw

        result = IntelligenceResponse.model_validate(payload)

    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise IntelligenceValidationError(
            f"Invalid Claude intelligence response: {exc}"
        ) from exc

    # Absolute safety invariant.
    # Explicit check rather than `assert`, which python -O removes.
    if (
        result.execution_authorized is not False
        or result.metadata.execution_authorized is not False
    ):
        raise IntelligenceValidationError(
            "Response violates the execution-authority invariant."
        )

    return result


def safe_no_trade(reason: str) -> IntelligenceResponse:
    """
    Produce a deterministic safe response when intelligence is unavailable
    or invalid.
    """

    return IntelligenceResponse(
        intent="UNAVAILABLE",
        assessment=reason,
        confidence=0.0,
        evidence=[],
        hypotheses=[],
        recommendation="Do not trade until valid intelligence is available.",
        risks=[reason],
        missing_information=["Valid Claude intelligence response"],
        action=IntelligenceAction.NO_TRADE,
        metadata={
            "fallback": True,
            "execution_authorized": False,
        },
    )
