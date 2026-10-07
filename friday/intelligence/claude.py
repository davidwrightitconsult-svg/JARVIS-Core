"""
FRIDAY Claude Intelligence Adapter.

Claude provides intelligence only.
This adapter has no trading execution authority.

All Claude responses are forced through the FRIDAY intelligence
contract before they can be consumed by the rest of the platform.

Phase 2C-4:
- Every request is task-scoped (IntelligenceTask; default GENERAL_RESEARCH)
- Uses build_task_prompt, so the task metadata, shared safety blocks
  and context-data escaping from 2C-3 reach Claude
- Sends context.prompt_payload(), so the prompt architecture version
  and the NONE/False authority labels reach Claude
- Unknown tasks never reach the API: safe NO_TRADE instead
- ClaudeResult records task and prompt version for audit

Phase 3D:
- Output headroom raised to 3,000 tokens (2C-4 live replies reached
  1,438 of 1,600)
- ClaudeResult records stop_reason; a reply cut off at the limit is
  flagged (truncated=True) instead of failing silently
"""

import json
import os
from dataclasses import dataclass

from .context import IntelligenceContext
from .prompts import (
    PROMPT_ARCHITECTURE_VERSION,
    SYSTEM_PROMPT,
    build_task_prompt,
)
from .schemas import IntelligenceResponse
from .tasks import DEFAULT_TASK, IntelligenceTask, resolve_task
from .validation import (
    IntelligenceValidationError,
    parse_intelligence_response,
    safe_no_trade,
)


MAX_OUTPUT_TOKENS = 3000


@dataclass
class ClaudeResult:
    response: IntelligenceResponse
    model: str
    api_used: bool
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    task: str = DEFAULT_TASK.value
    prompt_architecture_version: str = PROMPT_ARCHITECTURE_VERSION
    stop_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """True when Claude hit the output limit (reply may be incomplete)."""
        return self.stop_reason == "max_tokens"

    @property
    def execution_authorized(self) -> bool:
        """
        Absolute FRIDAY safety invariant, mirrored at result level.
        """
        return False


class ClaudeIntelligence:
    """
    Controlled Claude interface.

    Claude can analyse and reason.

    Claude cannot:
    - place orders
    - unlock execution
    - alter risk limits
    - alter bankroll controls
    - bypass the Risk Governor
    """

    def __init__(
        self,
        api_key: str,
        model: str,
    ):
        self.api_key = api_key
        self.model = model
        self.client = None

        if api_key:
            from anthropic import Anthropic

            workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()

            client_kwargs = {
                "api_key": api_key,
            }

            if workspace_id:
                client_kwargs["default_headers"] = {
                    "anthropic-workspace-id": workspace_id,
                }

            self.client = Anthropic(**client_kwargs)

    @property
    def available(self) -> bool:
        return self.client is not None

    @staticmethod
    def build_request_prompt(
        context: IntelligenceContext,
        task: IntelligenceTask,
    ) -> str:
        """
        Build the exact user-turn prompt sent to Claude.

        Exposed separately so tests and audit tooling can inspect the
        prompt without calling the API.
        """

        context_json = json.dumps(
            context.prompt_payload(),
            ensure_ascii=False,
            default=str,
        )

        return build_task_prompt(task, context_json)

    def analyse(
        self,
        context: IntelligenceContext,
        task: IntelligenceTask | str = DEFAULT_TASK,
    ) -> ClaudeResult:

        try:
            resolved = resolve_task(task)
        except ValueError as exc:
            return ClaudeResult(
                response=safe_no_trade(str(exc)),
                model=self.model,
                api_used=False,
                task=str(task),
            )

        if not self.available:
            return ClaudeResult(
                response=safe_no_trade(
                    "Claude API is not configured."
                ),
                model=self.model,
                api_used=False,
                task=resolved.value,
            )

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": self.build_request_prompt(
                            context,
                            resolved,
                        ),
                    }
                ],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": IntelligenceResponse.model_json_schema(),
                    }
                },
            )

            text_parts = []

            for block in message.content:
                text = getattr(block, "text", None)

                if text:
                    text_parts.append(text)

            raw = "".join(text_parts).strip()
            stop_reason = getattr(message, "stop_reason", None)

            try:
                result = parse_intelligence_response(raw)
            except IntelligenceValidationError as exc:
                reason = str(exc)
                if stop_reason == "max_tokens":
                    reason = (
                        f"Claude reply truncated at {MAX_OUTPUT_TOKENS} output "
                        f"tokens; {reason}"
                    )
                return ClaudeResult(
                    response=safe_no_trade(reason),
                    model=self.model,
                    api_used=True,
                    request_id=getattr(message, "_request_id", None),
                    input_tokens=int(getattr(getattr(message, "usage", None), "input_tokens", 0) or 0),
                    output_tokens=int(getattr(getattr(message, "usage", None), "output_tokens", 0) or 0),
                    task=resolved.value,
                    stop_reason=stop_reason,
                )

            usage = getattr(message, "usage", None)

            input_tokens = int(
                getattr(usage, "input_tokens", 0) or 0
            )

            output_tokens = int(
                getattr(usage, "output_tokens", 0) or 0
            )

            request_id = getattr(
                message,
                "_request_id",
                None,
            )

            return ClaudeResult(
                response=result,
                model=self.model,
                api_used=True,
                request_id=request_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                task=resolved.value,
                stop_reason=stop_reason,
            )

        except IntelligenceValidationError as exc:
            return ClaudeResult(
                response=safe_no_trade(
                    str(exc)
                ),
                model=self.model,
                api_used=True,
                task=resolved.value,
            )

        except Exception as exc:
            return ClaudeResult(
                response=safe_no_trade(
                    f"Claude intelligence unavailable: {exc}"
                ),
                model=self.model,
                api_used=True,
                task=resolved.value,
            )
