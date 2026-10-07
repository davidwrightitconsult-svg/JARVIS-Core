"""
Hermes read-only service gateway.

Phase 3B:
Handlers never receive raw FRIDAY objects (StateManager, HealthManager,
market client, Claude adapter). They receive a JobServices view bound to
ONE job, and every accessor checks that job's declared capabilities
before it answers. A MARKET_SCAN handler that tries to call Claude is
refused, even if the handler code is wrong.

All accessors return plain data (dict / list / result objects), never
objects with write methods.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from friday.intelligence.claude import ClaudeResult
from friday.intelligence.context import IntelligenceContext
from friday.intelligence.tasks import IntelligenceTask

from .authority import Capability, HermesAuthorityError
from .jobs import Job


class MarketDataSource(Protocol):
    def snapshots(self, limit: int = 25) -> list[dict[str, Any]]: ...


class KnowledgeSource(Protocol):
    def context_for(self, query: str) -> dict[str, Any]: ...


class MarketContextSource(Protocol):
    def context_for(self, query: str, market_ids: list[str] | None = ...) -> dict[str, Any] | None: ...


class ResearchIngestor(Protocol):
    def add_url(self, url: str, *, kind: str = ..., title: str | None = ...,
                tags: tuple[str, ...] = ..., refresh: bool = ...) -> Any: ...
    def ingest_inbox(self, kind: str = ...) -> list[Any]: ...


class IntelligenceEngine(Protocol):
    def analyse(
        self, context: IntelligenceContext, task: IntelligenceTask | str = ...
    ) -> ClaudeResult: ...


@dataclass(frozen=True)
class HermesServices:
    """
    What FRIDAY lends to Hermes. Each entry is optional; a missing
    service makes the dependent job fail with a clear reason.

    state_snapshot / health_check are zero-argument callables returning
    dicts (e.g. StateManager.snapshot, HealthManager.check) so Hermes
    never holds a reference that could mutate state.
    """

    state_snapshot: Callable[[], dict[str, Any]] | None = None
    health_check: Callable[[], dict[str, Any]] | None = None
    market: MarketDataSource | None = None
    intelligence: IntelligenceEngine | None = None
    knowledge: KnowledgeSource | None = None          # Phase 4C recall
    vault_sync: Callable[[], dict[str, Any]] | None = None  # Phase 4C sync
    research: ResearchIngestor | None = None                # Phase 5B library
    research_recall: KnowledgeSource | None = None          # Phase 5C passages
    market_context: MarketContextSource | None = None       # Phase 6C recorded market data


class ServiceUnavailableError(RuntimeError):
    """A job needs a service FRIDAY has not provided to Hermes."""


class JobServices:
    """
    Capability-gated, read-only services for a single job.
    """

    def __init__(self, job: Job, services: HermesServices):
        self._job = job
        self._services = services

    def _require(self, capability: Capability) -> None:
        if capability not in self._job.capabilities:
            raise HermesAuthorityError(
                f"Job {self._job.type.value} did not declare {capability.value}."
            )

    @staticmethod
    def _need(service, name: str):
        if service is None:
            raise ServiceUnavailableError(f"{name} is not available to Hermes.")
        return service

    def state(self) -> dict[str, Any]:
        self._require(Capability.READ_STATE)
        snapshot = self._need(self._services.state_snapshot, "State")()
        return copy.deepcopy(dict(snapshot))

    def health(self) -> dict[str, Any]:
        self._require(Capability.READ_HEALTH)
        result = self._need(self._services.health_check, "Health")()
        return copy.deepcopy(dict(result))

    def market_snapshots(self, limit: int) -> list[dict[str, Any]]:
        self._require(Capability.READ_MARKET_DATA)
        market = self._need(self._services.market, "Market data")
        rows = market.snapshots(limit)
        return [copy.deepcopy(dict(r)) for r in list(rows)[:limit]]

    def market_context(self, query: str, market_ids: list[str] | None = None) -> dict[str, Any] | None:
        self._require(Capability.READ_MARKET_DATA)
        source = self._need(self._services.market_context, "Market context")
        block = source.context_for(query, market_ids)
        return copy.deepcopy(dict(block)) if block else None

    def knowledge_context(self, query: str) -> dict[str, Any]:
        self._require(Capability.READ_KNOWLEDGE)
        source = self._need(self._services.knowledge, "Knowledge vault")
        return copy.deepcopy(dict(source.context_for(query)))

    def sync_vault(self) -> dict[str, Any]:
        self._require(Capability.PUBLISH_KNOWLEDGE)
        sync = self._need(self._services.vault_sync, "Vault sync")
        return copy.deepcopy(dict(sync()))

    def research_context(self, query: str) -> dict[str, Any] | None:
        self._require(Capability.READ_RESEARCH)
        source = self._need(self._services.research_recall, "Research recall")
        block = source.context_for(query)
        return copy.deepcopy(dict(block)) if block else None

    def research_library(self) -> ResearchIngestor:
        self._require(Capability.INGEST_RESEARCH)
        return self._need(self._services.research, "Research library")

    def analyse(
        self, context: IntelligenceContext, task: IntelligenceTask
    ) -> ClaudeResult:
        self._require(Capability.REQUEST_INTELLIGENCE)
        engine = self._need(self._services.intelligence, "Claude intelligence")
        return engine.analyse(context, task=task)
