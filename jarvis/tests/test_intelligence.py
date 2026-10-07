import json

import pytest

from jarvis.intelligence.context import IntelligenceContext
from jarvis.intelligence.prompts import (
    PROMPT_ARCHITECTURE_VERSION,
    SYSTEM_PROMPT,
)
from jarvis.intelligence.schemas import (
    EvidenceType,
    IntelligenceAction,
)
from jarvis.intelligence.validation import (
    IntelligenceValidationError,
    parse_intelligence_response,
    safe_no_trade,
)


def valid_payload():
    return {
        "intent": "market analysis",
        "assessment": "Insufficient evidence for a trade.",
        "confidence": 0.25,
        "evidence": [
            {
                "type": "FACT",
                "statement": "This is an observed fact.",
                "source": "test",
                "confidence": 0.95,
            }
        ],
        "hypotheses": [],
        "recommendation": "Continue observing.",
        "risks": ["Insufficient validation"],
        "missing_information": ["Historical validation"],
        "action": "NO_TRADE",
        "metadata": {},
    }


def test_valid_intelligence_response():
    result = parse_intelligence_response(valid_payload())

    assert result.action == IntelligenceAction.NO_TRADE
    assert result.execution_authorized is False
    assert result.evidence[0].type == EvidenceType.FACT


def test_json_response_is_supported():
    result = parse_intelligence_response(
        json.dumps(valid_payload())
    )

    assert result.execution_authorized is False


def test_invalid_response_is_rejected():
    payload = valid_payload()
    del payload["assessment"]

    with pytest.raises(IntelligenceValidationError):
        parse_intelligence_response(payload)


def test_safe_no_trade():
    result = safe_no_trade("Claude unavailable.")

    assert result.action == IntelligenceAction.NO_TRADE
    assert result.execution_authorized is False
    assert result.confidence == 0.0


def test_context_contains_no_execution_authority():
    context = IntelligenceContext(
        user_command="Analyse market",
        market_context={"market": "TEST"},
    )

    payload = context.safe_payload()

    assert payload["user_command"] == "Analyse market"
    assert "private_key" not in payload
    assert "execution_authorized" not in payload


def test_context_prompt_payload_has_authoritative_version():
    context = IntelligenceContext(
        user_command="Analyse market",
        market_context={"market": "TEST"},
    )

    payload = context.prompt_payload()

    assert (
        payload["prompt_architecture_version"]
        == PROMPT_ARCHITECTURE_VERSION
    )


def test_context_prompt_payload_is_data_only():
    context = IntelligenceContext(
        user_command="Analyse market",
        market_context={"market": "TEST"},
    )

    payload = context.prompt_payload()

    assert payload["context_type"] == "JARVIS_INTELLIGENCE_DATA"
    assert payload["instruction_authority"] == "NONE"
    assert payload["execution_authority"] is False


def test_prompt_version_exists():
    assert PROMPT_ARCHITECTURE_VERSION
    assert isinstance(PROMPT_ARCHITECTURE_VERSION, str)
    assert PROMPT_ARCHITECTURE_VERSION == "2C.3"


def test_prompt_contains_same_version():
    assert (
        f"Prompt Contract Version: {PROMPT_ARCHITECTURE_VERSION}"
        in SYSTEM_PROMPT
    )


def test_claude_adapter_without_key_is_safe():
    from jarvis.intelligence.claude import ClaudeIntelligence

    engine = ClaudeIntelligence(
        api_key="",
        model="claude-sonnet-5",
    )

    result = engine.analyse(
        IntelligenceContext(
            user_command="Analyse this market."
        )
    )

    assert result.api_used is False
    assert result.response.action == IntelligenceAction.NO_TRADE
    assert result.response.execution_authorized is False
