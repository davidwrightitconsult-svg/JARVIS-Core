"""
FRIDAY knowledge recorder — turns Hermes activity into vault notes.

Phase 4B:
- Daily journal      FRIDAY/Journal/YYYY-MM-DD.md       one line per job,
                     kill-switch/pause/resume and refused requests
- Analysis notes     FRIDAY/Analyses/YYYY-MM-DD-HHMMSS-<task>-<id>.md
                     after every successful Claude analysis
- Market snapshots   FRIDAY/Market-Snapshots/YYYY-MM-DD-HHMM-<id>.md
                     at most one per `snapshot_min_interval_seconds`
- Health checks are journalled only when the health status CHANGES
  (a 60-second schedule would otherwise flood the journal)

All writes go through KnowledgeVault (4A): FRIDAY zone only, atomic,
secret-guarded. The recorder NEVER raises into Hermes: a failed write is
counted and reported, and the job outcome is unaffected. All times UTC.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .notes import NoteMeta, NoteType
from .vault import KnowledgeVault, SecretDetectedError

KNOWLEDGE_RECORDER_VERSION = "4B.1"  # + 5B/5C research lines (format unchanged)
DEFAULT_SNAPSHOT_MIN_INTERVAL = 3600
MAX_SNAPSHOT_ROWS = 50
_WITHHELD = "note withheld: content looked like a secret"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _md(value: Any, limit: int = 2000) -> str:
    """Single-line, table-safe text."""
    text = " ".join(str(value if value is not None else "").split())
    text = text.replace("|", "\\|")
    return text[:limit]


def _slug(value: str) -> str:
    cleaned = "".join(c if c.isalnum() else "-" for c in str(value).lower())
    return "-".join(p for p in cleaned.split("-") if p)[:40] or "note"


def _evidence_line(item: dict[str, Any]) -> str:
    line = f"- **{_md(item.get('type'))}** — {_md(item.get('statement'))}"
    source = item.get("source")
    return line + (f" _(source: {_md(source, 200)})_" if source else "")


class KnowledgeRecorder:
    def __init__(
        self,
        vault: KnowledgeVault,
        snapshot_min_interval_seconds: int = DEFAULT_SNAPSHOT_MIN_INTERVAL,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.vault = vault
        self.snapshot_min_interval_seconds = int(snapshot_min_interval_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._last_health: str | None = None
        self._last_snapshot_at: datetime | None = None
        self.notes_written = 0
        self.write_failures = 0
        self.last_error: str | None = None
        self.last_write: str | None = None

    # ------------------------------------------------------------ helpers

    def _write(self, action: Callable[[], Any]) -> bool:
        try:
            action()
        except SecretDetectedError:
            self.write_failures += 1
            self.last_error = _WITHHELD
            return False
        except Exception as exc:  # never propagate into Hermes
            self.write_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return False
        self.notes_written += 1
        self.last_write = self._clock().isoformat(timespec="seconds")
        return True

    def _journal(self, line: str) -> bool:
        now = self._clock()
        day = now.strftime("%Y-%m-%d")
        rel = f"FRIDAY/Journal/{day}.md"
        meta = NoteMeta(type=NoteType.JOURNAL, title=f"FRIDAY journal {day}",
                        tags=("friday/journal",))
        entry = f"- `{now.strftime('%H:%M:%S')}Z` {line}"
        if not (self.vault.root / rel).exists():
            entry = (f"# FRIDAY journal — {day} (UTC)\n\n"
                     "Automated record of Hermes activity. Advisory only; "
                     "`execution_authorized: false`.\n\n" + entry)
        return self._write(lambda: self.vault.append_to_note(rel, meta, entry))

    # ------------------------------------------------------------ jobs

    def record_job(self, job: dict[str, Any]) -> None:
        """
        job: a finished-job dict (see HermesRuntime) with keys
        job_id, job_type, status, requested_by, result, error.

        Never raises; any failure (even while BUILDING a note, before the
        write) is counted and shown in status() — found in 5C, where a
        failure outside _write() was silently swallowed by Hermes.
        """
        try:
            self._record_job(job)
        except Exception as exc:
            self.write_failures += 1
            self.last_error = f"{job.get('job_type', '?')}: {type(exc).__name__}: {exc}"[:300]

    def _record_job(self, job: dict[str, Any]) -> None:
        with self._lock:
            job_type = job.get("job_type", "?")
            status = job.get("status", "?")
            result = job.get("result") or {}
            who = _md(job.get("requested_by", ""), 64)
            short = str(job.get("job_id", ""))[:8]

            if status != "SUCCEEDED":
                self._journal(f"**{job_type}** {status} — {_md(job.get('error'), 300)} "
                              f"(by {who}, job `{short}`)")
                return

            if job_type == "INTELLIGENCE_ANALYSIS":
                link = self._analysis_note(job, result)
                action = _md(result.get("action", "NO_TRADE"), 32)
                task = _md(result.get("task", ""), 40)
                extra = f" → {link}" if link else f" ({_WITHHELD})" if self.last_error == _WITHHELD else ""
                self._journal(f"**ANALYSIS** {task} · action **{action}** · "
                              f"tokens {result.get('input_tokens', 0)}/{result.get('output_tokens', 0)}"
                              f"{' · FALLBACK' if result.get('fallback') else ''}"
                              f" (by {who}){extra}")
            elif job_type == "MARKET_SCAN":
                link = self._maybe_snapshot(job, result)
                self._journal(f"**MARKET_SCAN** {result.get('count', 0)} markets observed"
                              f"{f' → {link}' if link else ''} (by {who})")
            elif job_type == "HEALTH_CHECK":
                health = str(result.get("status"))
                if health != self._last_health:
                    previous = self._last_health or "unknown"
                    self._last_health = health
                    self._journal(f"**HEALTH** {previous} → **{_md(health, 32)}** "
                                  f"(execution_locked={result.get('execution_locked')})")
            elif job_type == "RESEARCH_INGEST_URL":
                status = _md(result.get("status"), 16)
                title = f" «{_md(result.get('title'), 120)}»" if result.get("title") else ""
                detail = (f" — {result.get('chunks', 0)} passages" if status == "ADDED"
                          else f" — {_md(result.get('reason'), 200)}")
                self._journal(f"**RESEARCH** {status}{title} from `{_md(result.get('url'), 300)}`"
                              f"{detail} (by {who})")
            elif job_type == "RESEARCH_DIGEST":
                link = self._digest_note(job, result)
                done = len(result.get("sections") or [])
                stopped_note = f" · stopped: {_md(result['stopped'], 160)}" if result.get("stopped") else ""
                span = (f"sections {result.get('start', 0) + 1}–{result.get('start', 0) + done}"
                        if done else "no sections")
                self._journal(f"**DIGEST** «{_md(result.get('title'), 120)}» {span} of "
                              f"{result.get('total_sections')} · tokens {result.get('tokens_used', 0)}"
                              f"{' · COMPLETE' if result.get('complete') else ''}"
                              f"{stopped_note}"
                              f"{f' → {link}' if link else ''} (by {who})")
            elif job_type == "RESEARCH_INGEST_INBOX":
                if result.get("count"):          # quiet when the inbox was empty
                    names = ", ".join(f"{_md(r.get('status'), 12)} «{_md(r.get('title') or r.get('name'), 80)}»"
                                      for r in (result.get("results") or [])[:10])
                    self._journal(f"**RESEARCH INBOX** {result.get('added', 0)} added, "
                                  f"{result.get('duplicate', 0)} duplicate, "
                                  f"{result.get('rejected', 0)} rejected — {names}")
            else:
                self._journal(f"**{job_type}** SUCCEEDED (by {who})")

    def _analysis_note(self, job: dict[str, Any], result: dict[str, Any]) -> str | None:
        now = self._clock()
        response = result.get("response") or {}
        task = str(result.get("task", "general-research"))
        name = f"{now.strftime('%Y-%m-%d-%H%M%S')}-{_slug(task)}-{str(job.get('job_id', ''))[:8]}"
        rel = f"FRIDAY/Analyses/{name}.md"
        action = str(result.get("action", "NO_TRADE"))
        meta = NoteMeta(
            type=NoteType.ANALYSIS,
            title=f"{task} — {action} ({now.strftime('%Y-%m-%d %H:%M')}Z)",
            source="HERMES",
            tags=("friday/analysis", f"task/{_slug(task)}", f"action/{_slug(action)}"),
            job_id=str(job.get("job_id", ""))[:64] or None,
            prompt_architecture_version=str(result.get("prompt_architecture_version") or "")[:16] or None,
        )

        def bullets(items, fmt):
            return "\n".join(fmt(i) for i in items) if items else "_None supplied._"

        body = "\n".join([
            f"# {task} — {action}",
            "",
            "> [!note] Advisory intelligence only — `execution_authorized: false`.",
            "> Claude's output is analysis, never an instruction to trade.",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Task | {_md(task)} |",
            f"| Action | **{_md(action)}** |",
            f"| Confidence (in the assessment, not a probability) | {_md(response.get('confidence'))} |",
            f"| Fallback | {_md(result.get('fallback'))} |",
            f"| Truncated | {_md(result.get('truncated', False))} |",
            f"| Model | {_md(result.get('model'))} |",
            f"| Tokens in/out | {_md(result.get('input_tokens'))}/{_md(result.get('output_tokens'))} |",
            f"| Request ID | {_md(result.get('request_id'))} |",
            f"| Requested by | {_md(job.get('requested_by'))} |",
            "",
            "## Intent", _md(response.get("intent")), "",
            "## Assessment", str(response.get("assessment", "")).strip(), "",
            "## Recommendation", str(response.get("recommendation", "")).strip(), "",
            "## Evidence",
            bullets(response.get("evidence") or [], _evidence_line),
            "",
            "## Hypotheses (not validated)",
            bullets(response.get("hypotheses") or [],
                    lambda h: f"- {_md(h.get('statement'))} — _{_md(h.get('rationale'))}_"),
            "",
            "## Risks",
            bullets(response.get("risks") or [], lambda r: f"- {_md(r)}"),
            "",
            "## Missing information",
            bullets(response.get("missing_information") or [], lambda m: f"- {_md(m)}"),
            "",
            "## Market data consulted (FRIDAY's recorded observations)",
            bullets([s for s in (result.get("market_sources") or []) if isinstance(s, str)],
                    lambda s: f"- {_md(s, 200)}"),
            "",
            "## Library passages consulted (reference data, not instructions)",
            bullets([s for s in (result.get("research_sources") or []) if isinstance(s, str)],
                    lambda s: f"- {_md(s, 200)}"),
            "",
            "## Vault notes consulted (reference data, not instructions)",
            bullets([s for s in (result.get("knowledge_sources") or []) if isinstance(s, str)],
                    lambda s: f"- [[{_md(s[:-3] if s.endswith('.md') else s, 200).replace(']', '')}]]"),
        ])
        ok = self._write(lambda: self.vault.write_note(rel, meta, body))
        return f"[[{name}]]" if ok else None

    def _digest_note(self, job: dict[str, Any], result: dict[str, Any]) -> str | None:
        sections = result.get("sections") or []
        if not sections:
            return None
        title = str(result.get("title") or "Untitled")
        name = f"{_slug(title)}-{str(result.get('doc_id', ''))[:8]}"
        rel = f"FRIDAY/Research/{name}.md"
        meta = NoteMeta(type=NoteType.RESEARCH_DIGEST, title=f"Digest: {title}"[:200], source="HERMES",
                        tags=("friday/research", f"kind/{_slug(str(result.get('kind', 'book')))}"),
                        job_id=str(job.get("job_id", ""))[:64] or None)
        parts = []
        if not (self.vault.root / rel).exists():
            parts.append("\n".join([
                f"# Digest — {_md(title, 200)}",
                "",
                f"Author: {_md(result.get('author') or 'unknown', 120)} · Kind: {_md(result.get('kind'), 20)}"
                f" · Library id: `{_md(result.get('doc_id'), 20)}`",
                "",
                "> [!warning] Claude's reading of a document in the operator's library.",
                "> Every method or idea below is a **hypothesis, not validated**, until FRIDAY's",
                "> own backtesting (Phase 9) says otherwise. `execution_authorized: false`.",
            ]))

        def bullets(items, fmt):
            return "\n".join(fmt(i) for i in items) if items else "_None._"

        for sec in sections:
            r = sec.get("response") or {}
            parts.append("\n".join([
                f"## {_md(sec.get('locations'), 80)}",
                "",
                "**Summary.** " + " ".join(str(r.get("assessment", "")).split()),
                "",
                "**Key claims (the author's, not FRIDAY's)**",
                bullets(r.get("evidence") or [], _evidence_line),
                "",
                "**Hypotheses — not validated**",
                bullets(r.get("hypotheses") or [],
                        lambda h: f"- {_md(h.get('statement'))} — _{_md(h.get('rationale'))}_"),
                "",
                "**Risks and weaknesses**",
                bullets(r.get("risks") or [], lambda x: f"- {_md(x)}"),
                "",
                "**Open questions**",
                bullets(r.get("missing_information") or [], lambda x: f"- {_md(x)}"),
            ]))
        ok = self._write(lambda: self.vault.append_to_note(rel, meta, "\n\n".join(parts)))
        return f"[[{name}]]" if ok else None

    def _maybe_snapshot(self, job: dict[str, Any], result: dict[str, Any]) -> str | None:
        now = self._clock()
        if (self._last_snapshot_at is not None and
                (now - self._last_snapshot_at).total_seconds() < self.snapshot_min_interval_seconds):
            return None
        markets = list(result.get("markets") or [])[:MAX_SNAPSHOT_ROWS]
        name = f"{now.strftime('%Y-%m-%d-%H%M')}-scan-{str(job.get('job_id', ''))[:8]}"
        rel = f"FRIDAY/Market-Snapshots/{name}.md"
        meta = NoteMeta(type=NoteType.MARKET_SNAPSHOT,
                        title=f"Market snapshot {now.strftime('%Y-%m-%d %H:%M')}Z",
                        source="HERMES", tags=("friday/market-snapshot",),
                        job_id=str(job.get("job_id", ""))[:64] or None)
        rows = [f"| {_md(m.get('title'), 120)} | {_md(m.get('price'))} | {_md(m.get('bid'))} | "
                f"{_md(m.get('ask'))} | {_md(m.get('spread'))} | {_md(m.get('liquidity'))} |"
                for m in markets]
        body = "\n".join([
            f"# Market snapshot — {now.strftime('%Y-%m-%d %H:%M')} UTC",
            "",
            "> [!warning] Observation only — no scoring, no decision, no order.",
            "> Price/spread parsing is the Phase 0 client; order-book ordering is",
            "> verified in Phase 6. Do not treat these numbers as trade signals.",
            "",
            f"{len(markets)} markets (of {result.get('count', len(markets))} observed).",
            "",
            "| Market | Price | Bid | Ask | Spread | Liquidity |",
            "|---|---|---|---|---|---|",
            *rows,
        ])
        if self._write(lambda: self.vault.write_note(rel, meta, body)):
            self._last_snapshot_at = now
            return f"[[{name}]]"
        return None

    # ------------------------------------------------------------ events

    def write_scorecard(self, card: dict[str, Any], day: str) -> str | None:
        """Phase 7C: the daily strategy scorecard, FRIDAY/Strategies/Scorecard-<day>.md
        (rewritten if called again the same day). Never raises."""
        try:
            from friday.quant.scorecard import render_markdown
            name = f"Scorecard-{day}"
            meta = NoteMeta(type=NoteType.STRATEGY_SCORECARD, title=f"Strategy scorecard {day}", source="QUANT",
                            tags=("friday/strategies", "friday/scorecard"))
            with self._lock:
                ok = self._write(lambda: self.vault.write_note(f"FRIDAY/Strategies/{name}.md", meta,
                                                               render_markdown(card, day), overwrite=True))
            return f"[[{name}]]" if ok else None
        except Exception as exc:
            self.write_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return None

    def record_event(self, event: Any) -> None:
        """Journal Hermes control changes and refused requests."""
        kind = getattr(event, "kind", "")
        if kind not in ("HERMES_CONTROL", "HERMES_REJECTED"):
            return
        with self._lock:
            payload = getattr(event, "payload", None) or {}
            if kind == "HERMES_CONTROL":
                state = _md(payload.get("state"), 16)
                reason = payload.get("reason") or payload.get("confirmed_by")
                marker = "🛑 " if state == "KILLED" else ""
                self._journal(f"{marker}**HERMES {state}** — {_md(getattr(event, 'message', ''), 120)}"
                              f"{f' ({_md(reason, 200)})' if reason else ''}")
            else:
                self._journal(f"**REFUSED** job request `{_md(payload.get('requested'), 64)}` "
                              f"from {_md(payload.get('requested_by'), 64)} — {_md(payload.get('reason'), 200)}")

    # ------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "vault": str(self.vault.root),
            "recorder_version": KNOWLEDGE_RECORDER_VERSION,
            "notes_written": self.notes_written,
            "write_failures": self.write_failures,
            "last_write": self.last_write,
            "last_error": self.last_error,
            "snapshot_min_interval_seconds": self.snapshot_min_interval_seconds,
        }


# ------------------------------------------------------------------ bootstrap

DEFAULT_VAULT_PATH = "~/friday/FRIDAY-Knowledge-Vault"


def build_recorder_from_environment() -> tuple["KnowledgeRecorder | None", dict[str, Any]]:
    """
    Read Phase 4B settings and return (recorder or None, status).

    FRIDAY_KNOWLEDGE_ENABLED            true/false (default true)
    FRIDAY_VAULT_PATH                   default ~/friday/FRIDAY-Knowledge-Vault
    FRIDAY_SNAPSHOT_INTERVAL_SECONDS    min seconds between market snapshots
                                        (default 3600, minimum 300)

    Never raises: a missing/uninitialised vault or bad setting leaves the
    recorder disabled with a reason, and FRIDAY runs normally.
    """
    import os

    raw_enabled = os.getenv("FRIDAY_KNOWLEDGE_ENABLED", "true").strip().lower()
    path = os.getenv("FRIDAY_VAULT_PATH", DEFAULT_VAULT_PATH).strip() or DEFAULT_VAULT_PATH
    status: dict[str, Any] = {"enabled": False, "vault": path, "reason": None}

    if raw_enabled in ("0", "false", "no", "off"):
        status["reason"] = "disabled by FRIDAY_KNOWLEDGE_ENABLED"
        return None, status
    if raw_enabled not in ("1", "true", "yes", "on"):
        status["reason"] = f"invalid FRIDAY_KNOWLEDGE_ENABLED={raw_enabled!r}"
        return None, status
    try:
        interval = int(os.getenv("FRIDAY_SNAPSHOT_INTERVAL_SECONDS", str(DEFAULT_SNAPSHOT_MIN_INTERVAL)))
        if interval < 300:
            raise ValueError("must be >= 300")
    except ValueError as exc:
        status["reason"] = f"invalid FRIDAY_SNAPSHOT_INTERVAL_SECONDS: {exc}"
        return None, status

    vault = KnowledgeVault(path)
    report = vault.check()
    # FRIDAY/ is FRIDAY's own zone: sub-folders added by later phases (e.g.
    # FRIDAY/Research in 5C) are created here. A vault missing any of YOUR
    # folders is still treated as uninitialised. (Found in 5C: the new
    # folder made an existing vault look uninitialised and disabled notes.)
    from .vault import FRIDAY_FOLDERS, FRIDAY_ROOT
    friday_missing = [f for f in report["missing_folders"] if f.startswith(FRIDAY_ROOT + "/")]
    if report["exists"] and (vault.root / FRIDAY_ROOT).is_dir() and friday_missing:
        for folder in FRIDAY_FOLDERS:
            (vault.root / FRIDAY_ROOT / folder).mkdir(exist_ok=True)
        report = vault.check()
    if not report["exists"] or report["missing_folders"]:
        status["vault"] = str(vault.root)
        status["reason"] = (f"no initialised vault at {vault.root}; "
                            "run: python -m friday.knowledge init --vault <path>")
        return None, status

    recorder = KnowledgeRecorder(vault, snapshot_min_interval_seconds=interval)
    return recorder, recorder.status()
