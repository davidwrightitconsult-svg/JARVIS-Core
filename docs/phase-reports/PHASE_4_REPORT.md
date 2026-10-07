# Phase 4 — Obsidian Knowledge System: Report

Status: **Phase 4 signed off (4A, 4B, 4C)** — vault sync live on jarvis020 since 2026-09-26
Vault contract **4A.1** · Recorder **4B.1** · Recall **4C.1** · Sync **4C.1**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_4_KNOWLEDGE.md`

## 4A — Vault contract
| Item | Detail |
|---|---|
| `friday/knowledge/vault.py` | `KnowledgeVault`: zones (HUMAN read-only, FRIDAY auto-write), confined atomic writer (`write_note`, `append_to_note`), reader (`read_note`, `list_notes`), `init` (non-destructive), `check`. No delete. |
| `friday/knowledge/notes.py` | `NoteMeta` closed schema (`execution_authorized` can only be false); frontmatter rendered as JSON scalars (valid YAML, cannot be broken by values); lenient parser for hand-written notes. |
| `friday/knowledge/seed.py` | Vault README, `.gitignore`, **Constitution** and **Risk Doctrine** (from the project record), folder READMEs. |
| CLI | `python -m friday.knowledge init|check [--vault PATH]` |
| Secret guard | API keys, GitHub tokens, PEM keys, key/passphrase assignments, recovery phrases, and live values of secret-named env vars are refused; errors never echo the secret. Polymarket condition IDs (0x + 64 hex) are not false positives. |
| Tests | `test_knowledge_vault.py` — 40 tests (also under `python -O`) |
| Runtime | Wired in by 4B (below). |

Server validation: `bash scripts/validate_phase_4a.sh` on jarvis020.

## 4B — FRIDAY writes its own notes
| Item | Detail |
|---|---|
| `friday/knowledge/recorder.py` | `KnowledgeRecorder`: turns finished Hermes jobs and control events into notes. Writes only via `KnowledgeVault`, so the FRIDAY/ zone, secret guard and path confinement of 4A all apply. Never raises: failures are counted (`write_failures`, `last_error`) and FRIDAY carries on. |
| Analysis notes | `FRIDAY/Analyses/YYYY-MM-DD-HHMMSS-<task>-<id8>.md` — task, action, confidence, intent, assessment, recommendation, evidence, hypotheses (marked *not validated*), risks, missing information, advisory callout. Frontmatter carries `job_id`, prompt version and `execution_authorized: false`. Claude text is escaped so it cannot forge frontmatter. |
| Daily journal | `FRIDAY/Journal/YYYY-MM-DD.md` — one line per finished job; kill switch (🛑), control changes and refused jobs; health only when the status changes. Failed/cancelled jobs are journalled only. |
| Market snapshots | `FRIDAY/Market-Snapshots/…-scan-<id8>.md` from scheduled scans, throttled (`FRIDAY_SNAPSHOT_INTERVAL_SECONDS`, default 3600, minimum 300), max 50 rows, table cells escaped. |
| Authority | New Hermes capability `WRITE_KNOWLEDGE`; the runtime only calls the recorder for jobs that declare it. Still no execution, risk, bankroll, wallet or credential capability. |
| App | Recorder built at startup from the environment; component `knowledge` READY/DISABLED; `GET /api/knowledge/status`. A missing/uninitialised vault disables notes — it never stops FRIDAY. |
| Settings | `FRIDAY_KNOWLEDGE_ENABLED` (default true), `FRIDAY_VAULT_PATH`, `FRIDAY_SNAPSHOT_INTERVAL_SECONDS` |
| Tests | `test_knowledge_recorder.py` — 18 tests; the live app test now proves the note is written to a temporary vault. Offline tests never touch the real vault. |
| Not yet | Git commit/push of `FRIDAY/` and reading notes back into Claude context (4C). |

Server validation: `bash scripts/validate_phase_4b.sh` on jarvis020.

## 4C — Vault sync and recall
| Item | Detail |
|---|---|
| `friday/knowledge/sync.py` | `VaultSync`: commits only checked `FRIDAY/` files (pathspec commit; your staged or edited work is untouched); pre-commit checks (Markdown, no symlinks, size, secrets); refuses on the wrong branch or mid-merge; opt-in fast-forward push, never forced; git can't prompt; 60 s timeouts; output redacted. |
| `friday/knowledge/recall.py` | `KnowledgeRecall`: Constitution + Risk Doctrine + up to 3 keyword-related human notes → `research_context.knowledge_vault`, labelled untrusted; ≤5,500 chars; secrets withheld; `FRIDAY/` notes never recalled. |
| Hermes | Capabilities `READ_KNOWLEDGE` (analysis only) and `PUBLISH_KNOWLEDGE`; job `VAULT_SYNC` (minimum 300 s, no `WRITE_KNOWLEDGE`, so no self-journal churn); schedule `vault-sync`, registered only when sync is enabled. |
| App | `/api/knowledge/status` now reports recall + sync; `POST /api/knowledge/sync` runs a sync through Hermes. Analysis notes link the vault notes consulted. |
| CLI | `python -m friday.knowledge sync-status | sync [--push] | recall "question"` |
| Settings | `FRIDAY_KNOWLEDGE_RECALL` (true), `FRIDAY_VAULT_SYNC_INTERVAL_SECONDS` (900; 0 = off), `FRIDAY_VAULT_PUSH` (false until the deploy key works), `FRIDAY_VAULT_BRANCH` (main) |
| Tests | `test_knowledge_sync_recall.py`: 31 tests against real git repos (bare remote, a second clone ahead, staged human work, secrets, symlinks, branch and merge guards, no-prompt env). The live test asserts that doctrine reached Claude and that the call fits the reserve. |

Server validation: `bash scripts/validate_phase_4c.sh` on jarvis020.
