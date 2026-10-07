"""
Hermes job handlers.

Phase 3B:
One handler per job type. Each receives the job and its capability-gated
JobServices view, and returns a JSON-serialisable result dict.

Handlers observe and report. None of them can trade: they have no access
to anything but the read-only views in services.py.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping
from types import MappingProxyType

from friday.intelligence.context import IntelligenceContext
from friday.intelligence.tasks import IntelligenceTask

from .budget import BudgetExceededError, TokenBudget
from .jobs import (
    IntelligenceAnalysisParams,
    Job,
    JobType,
    MarketScanParams,
    ResearchDigestParams,
    ResearchIngestUrlParams,
)
from .authority import HermesAuthorityError
from .services import JobServices, ServiceUnavailableError


class RetryableJobError(RuntimeError):
    """Transient failure (network, timeout): the runtime may retry."""


Handler = Callable[[Job, JobServices, "HandlerDeps"], dict[str, Any]]


class HandlerDeps:
    """Runtime-owned dependencies handlers may need (not FRIDAY objects)."""

    def __init__(self, budget: TokenBudget, research_budget: TokenBudget | None = None):
        self.budget = budget
        self.research_budget = research_budget


def handle_status_report(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    state = svc.state()
    return {
        "status": state.get("status"),
        "mode": state.get("mode"),
        "execution_locked": state.get("execution_locked"),
        "components": state.get("components", {}),
        "last_error": state.get("last_error"),
    }


def handle_health_check(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    health = svc.health()
    return {
        "status": health.get("status"),
        "execution_locked": health.get("execution_locked"),
        "components": health.get("components", {}),
        "last_error": health.get("last_error"),
    }


def handle_market_scan(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    params: MarketScanParams = job.params
    try:
        rows = svc.market_snapshots(params.limit)
    except (HermesAuthorityError, ServiceUnavailableError):
        raise  # configuration/authority problems are never retried
    except Exception as exc:  # network / API errors are transient
        raise RetryableJobError(f"Market data unavailable: {exc}") from exc
    # Observation only: no scoring, no decision (Quant Core is Phase 7).
    return {"count": len(rows), "markets": rows}


ANALYSIS_BRIEF_RETRY = (
    " (Your previous reply was too long and was cut off. Reply again, compactly: an "
    "assessment of at most 100 words and at most 4 items in each list, one sentence each.)"
)


def handle_intelligence_analysis(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    params: IntelligenceAnalysisParams = job.params

    # Budget check BEFORE any network call; exhaustion is not retried.
    deps.budget.ensure_available()

    state = svc.state()

    # Phase 4C: operator's vault notes as UNTRUSTED reference data. A
    # missing vault never blocks analysis; it just supplies nothing.
    research = dict(params.research_context)
    research.pop("knowledge_vault", None)          # callers cannot spoof it
    sources: list[str] = []
    try:
        block = svc.knowledge_context(params.user_command)
    except Exception:          # unavailable, not granted, or unreadable
        block = None
    if block and (block.get("doctrine") or block.get("related_notes")):
        research["knowledge_vault"] = block
        sources = [item["path"] for key in ("doctrine", "related_notes")
                   for item in block.get(key, []) if isinstance(item, dict) and "path" in item]

    # Phase 5C: a few cited library passages, also UNTRUSTED data.
    research.pop("research_library", None)
    research_sources: list[str] = []
    try:
        library_block = svc.research_context(params.user_command)
    except Exception:          # no library, not granted, or unreadable
        library_block = None
    if library_block and library_block.get("passages"):
        research["research_library"] = library_block
        research_sources = [p["title"] + (f", {p['location']}" if p.get("location") else "")
                            for p in library_block["passages"]]

    # Phase 6C: FRIDAY's own recorded order-book data for the markets the
    # command is about (or the market_ids requested), as MEASURED_DATA.
    market = dict(params.market_context)
    market.pop("friday_market_data", None)          # callers cannot spoof it
    requested = market.get("market_ids")
    requested = [str(i) for i in requested][:3] if isinstance(requested, list) else None
    market_sources: list[str] = []
    try:
        market_block = svc.market_context(params.user_command, requested)
    except Exception:          # no store, not granted, or unreadable
        market_block = None
    if market_block and market_block.get("markets"):
        market["friday_market_data"] = market_block
        market_sources = [f"{m['question'][:120]} (market {m['market_id']})" for m in market_block["markets"]]

    context = IntelligenceContext(
        user_command=params.user_command,
        system_state={
            "mode": state.get("mode"),
            "execution_locked": state.get("execution_locked", True),
            "requested_via": "HERMES",
        },
        market_context=market,
        research_context=research,
        strategy_context=dict(params.strategy_context),
        risk_context={"execution_allowed": False},
    )

    result = svc.analyse(context, params.task)

    if result.api_used:
        deps.budget.record(result.input_tokens, result.output_tokens)

    # 6C fix: a reply cut off at the output limit is unparseable, so it
    # becomes a safe NO_TRADE fallback. Retry ONCE, asking for brevity
    # (same fix as the 5C digests); the budget is checked first.
    retried = False
    first_tokens = (0, 0)
    if result.truncated and result.response.metadata.fallback:
        try:
            deps.budget.ensure_available()
        except BudgetExceededError:
            pass
        else:
            retried = True
            first_tokens = (result.input_tokens, result.output_tokens)
            context = context.model_copy(update={"user_command": (context.user_command + ANALYSIS_BRIEF_RETRY)[:6000]})
            result = svc.analyse(context, params.task)
            if result.api_used:
                deps.budget.record(result.input_tokens, result.output_tokens)

    response = result.response
    return {
        "task": result.task,
        "model": result.model,
        "api_used": result.api_used,
        "request_id": result.request_id,
        "input_tokens": result.input_tokens + first_tokens[0],
        "output_tokens": result.output_tokens + first_tokens[1],
        "retried_brief": retried,
        "prompt_architecture_version": result.prompt_architecture_version,
        "fallback": response.metadata.fallback,
        "stop_reason": result.stop_reason,
        "truncated": result.truncated,
        "action": response.action.value,
        "response": response.model_dump(mode="json"),
        "knowledge_sources": sources,
        "research_sources": research_sources,
        "market_sources": market_sources,
        "execution_authorized": False,
    }


DIGEST_TIME_LIMIT_SECONDS = 600
DIGEST_INSTRUCTION = (
    "Digest this section of \"{title}\" ({where}) from the operator's research library. "
    "In the assessment, summarise the section's key ideas in plain language. List the "
    "author's key claims as evidence items, citing the page/chapter as the source. List "
    "any trading or forecasting methods as HYPOTHESES (not validated). List risks, "
    "weaknesses, untested assumptions and missing information. Do not recommend any "
    "trade: the action must be NO_TRADE. Be compact so the reply fits the output limit: "
    "assessment at most ~120 words; at most 6 evidence items, 4 hypotheses, 4 risks and "
    "4 missing_information items; one sentence each."
)
DIGEST_BRIEF_RETRY = (
    " The previous reply was too long and was cut off: this time give an assessment of "
    "at most 60 words and at most 3 items in each list."
)


def handle_research_digest(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    """
    Claude reads the next few ~12k-char sections of ONE library document
    (research budget, not the analysis budget) and returns structured
    notes per section; the recorder writes them to FRIDAY/Research/.
    Resumable: progress is kept in the library.
    """
    params: ResearchDigestParams = job.params
    budget = deps.research_budget
    if budget is None:
        raise ServiceUnavailableError("Research budget is not configured.")
    library = svc.research_library()
    plan = library.digest_plan(params.doc_id, params.sections, params.start)
    started, out, tokens, stopped = time.monotonic(), [], 0, None
    for section in plan["sections"]:
        if time.monotonic() - started > DIGEST_TIME_LIMIT_SECONDS:
            stopped = "time limit reached; run the digest again to continue"
            break
        try:
            budget.ensure_available()
        except BudgetExceededError as exc:
            if not out:
                raise
            stopped = str(exc)
            break
        context = IntelligenceContext(
            user_command=DIGEST_INSTRUCTION.format(title=plan["title"], where=section["locations"]),
            system_state={"execution_locked": True, "requested_via": "HERMES_RESEARCH_DIGEST"},
            research_context={"document": {
                "title": plan["title"], "author": plan["author"], "kind": plan["kind"],
                "section": section["locations"], "trust": "UNTRUSTED_DATA",
                "instruction_authority": "NONE", "text": section["text"]}},
            risk_context={"execution_allowed": False},
        )
        result = svc.analyse(context, IntelligenceTask.GENERAL_RESEARCH)
        if result.api_used:
            budget.record(result.input_tokens, result.output_tokens)
            tokens += result.input_tokens + result.output_tokens
        if result.response.metadata.fallback and result.truncated:
            # Dense section: retry ONCE, asking for a briefer reply.
            try:
                budget.ensure_available()
            except BudgetExceededError as exc:
                stopped = str(exc)
                break
            context = context.model_copy(update={"user_command": context.user_command + DIGEST_BRIEF_RETRY})
            result = svc.analyse(context, IntelligenceTask.GENERAL_RESEARCH)
            if result.api_used:
                budget.record(result.input_tokens, result.output_tokens)
                tokens += result.input_tokens + result.output_tokens
        if result.response.metadata.fallback:
            reason = " ".join(str(result.response.assessment).split())[:240]
            stopped = (f"Claude returned a fallback response for {section['locations']} "
                       f"({reason}); this section was not recorded — run again to retry it")
            break
        out.append({
            "index": section["index"], "locations": section["locations"],
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "truncated": result.truncated, "response": result.response.model_dump(mode="json"),
        })
    next_section = plan["start"] + len(out)
    library.mark_digested(params.doc_id, next_section, plan["total_sections"], tokens)
    return {
        "doc_id": plan["doc_id"], "title": plan["title"], "author": plan["author"],
        "kind": plan["kind"], "source": plan["source"],
        "total_sections": plan["total_sections"], "start": plan["start"],
        "next_section": next_section, "complete": next_section >= plan["total_sections"],
        "sections": out, "tokens_used": tokens, "stopped": stopped,
        "execution_authorized": False,
    }


def handle_vault_sync(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    # Commits FRIDAY/ notes only; pushes only if enabled. Never trades.
    result = svc.sync_vault()
    result["execution_authorized"] = False
    return result


def _ingest_dict(result: Any) -> dict:
    return {k: getattr(result, k, None) for k in
            ("status", "name", "doc_id", "title", "chunks", "withheld_chunks", "reason")}


def handle_research_ingest_url(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    # Fetches ONE operator-supplied https URL through the hardened fetcher.
    # A refused/failed fetch is a normal REJECTED result, not a retry.
    params: ResearchIngestUrlParams = job.params
    result = svc.research_library().add_url(
        params.url, kind=params.kind, title=params.title,
        tags=tuple(params.tags), refresh=params.refresh)
    return {**_ingest_dict(result), "url": params.url, "kind": params.kind,
            "execution_authorized": False}


def handle_research_ingest_inbox(job: Job, svc: JobServices, deps: HandlerDeps) -> dict:
    results = [_ingest_dict(r) for r in svc.research_library().ingest_inbox()]
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("ADDED", "DUPLICATE", "REJECTED")}
    return {"count": len(results), **{k.lower(): v for k, v in counts.items()},
            "results": results[:50], "execution_authorized": False}


HANDLERS: Mapping[JobType, Handler] = MappingProxyType({
    JobType.STATUS_REPORT: handle_status_report,
    JobType.HEALTH_CHECK: handle_health_check,
    JobType.MARKET_SCAN: handle_market_scan,
    JobType.INTELLIGENCE_ANALYSIS: handle_intelligence_analysis,
    JobType.VAULT_SYNC: handle_vault_sync,
    JobType.RESEARCH_INGEST_URL: handle_research_ingest_url,
    JobType.RESEARCH_INGEST_INBOX: handle_research_ingest_inbox,
    JobType.RESEARCH_DIGEST: handle_research_digest,
})


def _verify_handlers() -> None:
    missing = set(JobType) - set(HANDLERS)
    if missing:
        raise RuntimeError(f"Job types without handlers: {sorted(m.value for m in missing)}")


_verify_handlers()
