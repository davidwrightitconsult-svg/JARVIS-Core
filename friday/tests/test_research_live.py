"""
Phase 5C — LIVE: research recall + one real digest section (2 Claude calls).

Runs only when FRIDAY_LIVE_RESEARCH_TEST=1 and ANTHROPIC_API_KEY is set. Uses a
TEMPORARY database, library and vault — nothing real is touched.
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

# Opt-in (costs 2 Claude calls): scripts/validate_phase_5c.sh sets the flag,
# so ordinary regression runs never spend tokens on it.
pytestmark = pytest.mark.skipif(
    not (os.getenv("ANTHROPIC_API_KEY") and os.getenv("FRIDAY_LIVE_RESEARCH_TEST") == "1"),
    reason="live research test is opt-in (FRIDAY_LIVE_RESEARCH_TEST=1 + ANTHROPIC_API_KEY)")

NON_TRADING = {"NO_TRADE", "ANALYSE", "WATCH", "RESEARCH"}

TEXT = """# Longshot Bias Notes

The favourite-longshot bias is the tendency for bettors to overvalue unlikely outcomes and
undervalue favourites. In many betting markets, contracts priced at a few cents win less often
than their price implies, while heavy favourites win slightly more often than implied.

Several explanations have been proposed: risk-loving preferences, probability misperception
(people overweight small probabilities), and the entertainment value of long shots. Market
makers may also widen prices on thin, unlikely outcomes.

A trader might hypothesise that systematically selling long shots and buying favourites earns a
small edge. That idea is untested here: fees, liquidity, capital lock-up and rare large losses
could erase it, and prediction markets may differ from horse racing.
"""


def test_live_research_recall_and_digest(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import friday.app.db as db_module
    import friday.app.main as main
    from friday.core.health import HealthManager
    from friday.core.lifecycle import LifecycleManager
    from friday.core.state import StateManager
    from friday.hermes.budget import DEFAULT_RESERVE_PER_CALL
    from friday.intelligence.claude import MAX_OUTPUT_TOKENS
    from friday.knowledge import KnowledgeVault
    from friday.research import ResearchLibrary

    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "friday_live.db"))
    state = StateManager("PAPER")
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "health_manager", HealthManager(state))
    monkeypatch.setattr(main, "lifecycle", LifecycleManager(state, main.events))
    for name in ("HERMES_HEALTH_INTERVAL_SECONDS", "HERMES_MARKET_SCAN_INTERVAL_SECONDS",
                 "FRIDAY_RESEARCH_INBOX_INTERVAL_SECONDS", "FRIDAY_VAULT_SYNC_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "0")
    vault = KnowledgeVault(tmp_path / "vault")
    vault.init()
    lib = ResearchLibrary(tmp_path / "library").init()
    (tmp_path / "longshot.md").write_text(TEXT)
    doc = lib.add(tmp_path / "longshot.md", kind="paper")
    monkeypatch.setenv("FRIDAY_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("FRIDAY_VAULT_PATH", str(vault.root))
    monkeypatch.setenv("FRIDAY_LIBRARY_PATH", str(lib.root))

    with TestClient(main.app) as client:
        body = client.post("/api/command", json={
            "command": "Does the favourite-longshot bias suggest an edge in prediction markets?",
            "source": "validation"}).json()
        job = client.get(f"/api/hermes/jobs/{body['hermes_job_id']}").json()
        digest = client.post("/api/research/digest", json={"doc_id": doc.doc_id, "sections": 1}).json()
        status = client.get("/api/research/status").json()

    result = job.get("result") or {}
    d = digest.get("result") or {}
    section = (d.get("sections") or [{}])[0]
    response = section.get("response") or {}
    print(f"\n[RESEARCH] analysis job={job['status']} action={result.get('action')} "
          f"tokens={result.get('input_tokens')}/{result.get('output_tokens')} "
          f"library passages={result.get('research_sources')}")
    print(f"[RESEARCH] digest job={digest.get('status')} sections={len(d.get('sections') or [])}/"
          f"{d.get('total_sections')} action={response.get('action')} "
          f"fallback={(response.get('metadata') or {}).get('fallback')} tokens={d.get('tokens_used')} "
          f"hypotheses={len(response.get('hypotheses') or [])} "
          f"research_budget_used={status['research_budget']['used']}")
    print(f"[RESEARCH] vault research notes: {vault.list_notes('FRIDAY/Research')}")

    assert job["status"] == "SUCCEEDED" and result["fallback"] is False
    # Any contract action is non-trading (NO_TRADE / ANALYSE / WATCH / RESEARCH);
    # there is no trade action in the contract at all.
    assert result["action"] in NON_TRADING and result["execution_authorized"] is False
    assert result["research_sources"] and result["research_sources"][0].startswith("Longshot Bias Notes")
    assert result["input_tokens"] + MAX_OUTPUT_TOKENS <= DEFAULT_RESERVE_PER_CALL
    assert digest["status"] == "SUCCEEDED", digest.get("error")
    assert len(d["sections"]) == 1 and response["action"] in NON_TRADING
    assert response["metadata"]["fallback"] is False and d["execution_authorized"] is False
    assert status["research_budget"]["used"] == d["tokens_used"] > 0
    assert len(vault.list_notes("FRIDAY/Research")) == 1
