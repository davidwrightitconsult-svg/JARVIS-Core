"""
FRIDAY Claude Intelligence Contract.

Claude provides analysis only.
This schema does not grant execution authority.

IMPORTANT:
Anthropic Structured Outputs currently does not support JSON Schema
numeric minimum/maximum constraints in the output schema.

Therefore:
- The JSON schema defines the STRUCTURE only.
- Numeric confidence validation remains enforced by Pydantic
  after Claude returns the structured response.
- Claude can NEVER authorize trade execution.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceType(str, Enum):
    FACT = "FACT"
    INTERPRETATION = "INTERPRETATION"
    HYPOTHESIS = "HYPOTHESIS"
    VALIDATED_STRATEGY = "VALIDATED_STRATEGY"


class IntelligenceAction(str, Enum):
    NO_TRADE = "NO_TRADE"
    ANALYSE = "ANALYSE"
    WATCH = "WATCH"
    RESEARCH = "RESEARCH"


class EvidenceItem(BaseModel):
    """
    A single piece of evidence supporting an intelligence assessment.
    """

    model_config = ConfigDict(extra="forbid")

    type: EvidenceType
    statement: str = Field(min_length=1)
    source: str | None = None
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """
        Enforce confidence range AFTER structured output is received.

        This validation is intentionally outside the Anthropic JSON schema
        because Anthropic Structured Outputs does not support minimum/maximum
        constraints for number fields.
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value


class Hypothesis(BaseModel):
    """
    A hypothesis generated from available evidence.

    A hypothesis is NOT considered a validated strategy merely because
    Claude produces it.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """
        Enforce confidence range AFTER structured output is received.
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value


class IntelligenceMetadata(BaseModel):
    """
    Controlled metadata associated with an intelligence response.

    Additional fields are deliberately rejected so Claude cannot
    introduce arbitrary metadata into the FRIDAY control plane.
    """

    model_config = ConfigDict(extra="forbid")

    fallback: bool = False
    execution_authorized: bool = False

    @field_validator("execution_authorized")
    @classmethod
    def reject_execution_authorization(cls, value: bool) -> bool:
        """
        Phase 2C-4: a response that claims execution authorization is
        rejected outright (the adapter then returns safe NO_TRADE).

        Claude cannot grant itself authority by setting this flag, and
        no downstream reader of metadata can be misled by it.
        """
        if value is not False:
            raise ValueError(
                "execution_authorized must be false; Claude has no "
                "execution authority"
            )
        return value


class IntelligenceResponse(BaseModel):
    """
    Structured intelligence response returned by Claude.

    This model is deliberately closed to unexpected fields.

    IMPORTANT:
    This schema does NOT grant Claude execution authority.
    """

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1)
    assessment: str = Field(min_length=1)
    confidence: float = 0.0
    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommendation: str = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    action: IntelligenceAction = IntelligenceAction.NO_TRADE
    metadata: IntelligenceMetadata = Field(
        default_factory=IntelligenceMetadata
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """
        Enforce confidence range AFTER structured output is received.
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value

    @property
    def execution_authorized(self) -> bool:
        """
        Absolute FRIDAY safety invariant.

        Claude can never authorize execution.

        This property intentionally always returns False,
        regardless of the contents of the response.
        """
        return False
