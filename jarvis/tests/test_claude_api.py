import os
from pathlib import Path

from dotenv import load_dotenv
import pytest

load_dotenv(dotenv_path=Path.cwd() / '.env', override=True)

from jarvis.intelligence.claude import ClaudeIntelligence
from jarvis.intelligence.context import IntelligenceContext
from jarvis.intelligence.schemas import IntelligenceAction


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not configured",
)
def test_claude_live_non_trading_request():
    """
    Real API smoke test.

    This deliberately performs no market lookup, no trading operation,
    and no execution request.
    """

    model = os.getenv(
        "CLAUDE_MODEL",
        "claude-sonnet-5",
    )

    engine = ClaudeIntelligence(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model=model,
    )

    context = IntelligenceContext(
        user_command=(
            "Confirm that you are FRIDAY's intelligence layer. "
            "This is a connectivity test only. "
            "Do not analyse a market and do not propose a trade."
        ),
        system_state={
            "mode": "PAPER",
            "execution_locked": True,
        },
        risk_context={
            "execution_allowed": False,
        },
    )

    result = engine.analyse(context)

    assert result.api_used is True
    assert result.response.execution_authorized is False

    # The test itself does not require a trading action.
    assert result.response.action in IntelligenceAction

    print()
    print("Claude model:", result.model)
    print("Request ID:", result.request_id)
    print("Input tokens:", result.input_tokens)
    print("Output tokens:", result.output_tokens)
    print("Action:", result.response.action.value)
    print("Execution authorized:", result.response.execution_authorized)
