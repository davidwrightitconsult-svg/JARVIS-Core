"""
Hermes Authority Model.

Phase 3A:
- Hermes capabilities are an explicit allowlist (enum)
- Execution, risk, bankroll, wallet and credential actions have NO
  enum member, so they cannot be declared by any job
- A name-based guard additionally rejects forbidden action names
  arriving as untrusted strings

Hermes can schedule, route, observe and report.
Hermes cannot trade.
"""

from __future__ import annotations

import re
from enum import Enum


class HermesAuthorityError(PermissionError):
    """Raised when Hermes is asked to act outside its authority."""


class Capability(str, Enum):
    """
    Everything Hermes is permitted to do. Nothing else exists.
    """

    READ_STATE = "READ_STATE"
    READ_HEALTH = "READ_HEALTH"
    READ_MARKET_DATA = "READ_MARKET_DATA"
    REQUEST_INTELLIGENCE = "REQUEST_INTELLIGENCE"
    EMIT_EVENT = "EMIT_EVENT"
    # Phase 4B: write notes to the FRIDAY/ zone of the knowledge vault only
    # (the vault itself enforces the zone; human notes are never writable).
    WRITE_KNOWLEDGE = "WRITE_KNOWLEDGE"
    # Phase 4C: read vault notes as untrusted reference data for Claude,
    # and commit/push FRIDAY/ notes to the vault's own git repository.
    READ_KNOWLEDGE = "READ_KNOWLEDGE"
    PUBLISH_KNOWLEDGE = "PUBLISH_KNOWLEDGE"
    # Phase 5B: add documents to the private research library — files from
    # the inbox, or ONE operator-supplied https URL via the hardened fetcher.
    INGEST_RESEARCH = "INGEST_RESEARCH"
    # Phase 5C: read library passages as untrusted reference data for Claude
    # (analysis recall and on-request digests).
    READ_RESEARCH = "READ_RESEARCH"


# Hermes' granted authority. Deliberately equal to the full enum today;
# kept separate so a capability can exist without being granted.
HERMES_AUTHORITY: frozenset[Capability] = frozenset(Capability)


# Action names that must never be granted, whatever their spelling.
# These are NOT capabilities; they are listed only so untrusted strings
# naming them are recognised and rejected with an explicit reason.
FORBIDDEN_ACTIONS: frozenset[str] = frozenset({
    "PLACE_ORDER",
    "CANCEL_ORDER",
    "EXECUTE_TRADE",
    "SUBMIT_TRANSACTION",
    "UNLOCK_EXECUTION",
    "CHANGE_EXECUTION_STATE",
    "MODIFY_RISK_LIMITS",
    "MODIFY_BANKROLL",
    "BYPASS_RISK_GOVERNOR",
    "BYPASS_EXECUTION_GATE",
    "ACCESS_WALLET",
    "ACCESS_CREDENTIALS",
    "ENABLE_LIVE_TRADING",
})

# Fragments that mark a name as execution/risk/credential related even if
# it is not an exact match (e.g. "place-order", "unlockExecution").
# Deliberately strict: "TRADE" blocks any trade-named job until a later,
# signed-off phase (e.g. paper trading) introduces it explicitly.
# Plain "ORDER" is NOT a fragment so read-only ORDER_BOOK data stays usable.
_FORBIDDEN_FRAGMENTS = (
    "PLACE_ORDER", "CANCEL_ORDER", "SUBMIT_ORDER", "TRADE", "EXECUT",
    "TRANSACTION", "WALLET", "CREDENTIAL", "PRIVATE_KEY", "SECRET",
    "BANKROLL", "RISK_LIMIT", "LIVE_TRADING", "UNLOCK",
)


def _normalise(name: str) -> str:
    # camelCase -> CAMEL_CASE, separators -> underscore
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def is_forbidden(name: str) -> bool:
    """
    True if a name refers to an action outside Hermes' authority.
    """

    normalised = _normalise(name)
    if normalised in FORBIDDEN_ACTIONS:
        return True
    return any(fragment in normalised for fragment in _FORBIDDEN_FRAGMENTS)


def resolve_capability(name: Capability | str) -> Capability:
    """
    Resolve an untrusted capability name.

    Forbidden actions raise HermesAuthorityError; unknown names raise
    HermesAuthorityError as well — there is no fallback grant.
    """

    if isinstance(name, Capability):
        return name

    if is_forbidden(name):
        raise HermesAuthorityError(
            f"'{name}' is outside Hermes' authority (execution, risk, "
            "bankroll and credential actions are never granted)."
        )

    try:
        return Capability(_normalise(name))
    except ValueError:
        raise HermesAuthorityError(f"Unknown capability: {name!r}") from None


def authorize(*capabilities: Capability | str) -> frozenset[Capability]:
    """
    Verify every requested capability is within Hermes' authority.

    Returns the resolved set, or raises HermesAuthorityError.
    """

    resolved = frozenset(resolve_capability(c) for c in capabilities)
    missing = resolved - HERMES_AUTHORITY
    if missing:
        raise HermesAuthorityError(
            f"Capabilities not granted to Hermes: {sorted(c.value for c in missing)}"
        )
    return resolved


def _verify_authority() -> None:
    for capability in Capability:
        if is_forbidden(capability.value):
            raise RuntimeError(
                f"Capability {capability.value} matches a forbidden action."
            )


_verify_authority()
