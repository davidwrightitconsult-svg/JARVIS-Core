"""
Phase 3D — output headroom, truncation flag, and token accounting on
contract failures (offline, fake Anthropic client).
"""

import json
from types import SimpleNamespace

from friday.intelligence.claude import MAX_OUTPUT_TOKENS, ClaudeIntelligence
from friday.intelligence.context import IntelligenceContext
from friday.intelligence.schemas import IntelligenceAction

VALID = {
    "intent": "x", "assessment": "Insufficient evidence.", "confidence": 0.1,
    "recommendation": "Observe.", "action": "NO_TRADE",
}


def _engine(text, stop_reason):
    engine = ClaudeIntelligence(api_key="", model="claude-test")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(input_tokens=4700, output_tokens=2999),
            stop_reason=stop_reason, _request_id="req_x",
        )

    engine.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return engine, calls


def test_output_headroom_is_3000():
    engine, calls = _engine(json.dumps(VALID), "end_turn")
    engine.analyse(IntelligenceContext(user_command="x"))
    assert MAX_OUTPUT_TOKENS == 3000
    assert calls[0]["max_tokens"] == 3000


def test_normal_reply_records_stop_reason():
    engine, _ = _engine(json.dumps(VALID), "end_turn")
    result = engine.analyse(IntelligenceContext(user_command="x"))
    assert result.stop_reason == "end_turn"
    assert result.truncated is False
    assert result.response.metadata.fallback is False


def test_truncated_reply_is_flagged_safe_and_still_charged():
    cut = json.dumps(VALID)[:40]           # JSON cut off mid-way
    engine, _ = _engine(cut, "max_tokens")
    result = engine.analyse(IntelligenceContext(user_command="x"))

    assert result.truncated is True
    assert result.response.action == IntelligenceAction.NO_TRADE
    assert result.response.metadata.fallback is True
    assert "truncated at 3000" in result.response.assessment
    # Real API call: tokens must be reported so the budget is charged.
    assert (result.input_tokens, result.output_tokens) == (4700, 2999)
    assert result.api_used is True
    assert result.execution_authorized is False


def test_invalid_reply_tokens_are_reported_for_budget():
    engine, _ = _engine("not json at all", "end_turn")
    result = engine.analyse(IntelligenceContext(user_command="x"))
    assert result.response.metadata.fallback is True
    assert result.input_tokens == 4700 and result.output_tokens == 2999
