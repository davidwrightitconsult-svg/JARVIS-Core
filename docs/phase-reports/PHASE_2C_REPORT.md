# Phase 2C — Prompt Architecture: Completion Report

Status: **2C-1 PASS · 2C-2 PASS · 2C-3 PASS (signed off) · 2C-4 awaiting sign-off**
Prompt architecture version: **2C.3**
Execution: **LOCKED** · Live trading: **DISABLED**

## Principle

Task specialization changes *what* the intelligence engine analyses.
It never changes *what it is authorized to do*.

## 2C-1 — Prompt contract (validated earlier)
- Authority boundary, evidence discipline (FACT / INTERPRETATION / HYPOTHESIS / VALIDATED_STRATEGY)
- External content is DATA, not instructions; NO_TRADE always acceptable
- Confidence is not probability of outcome; JSON-only output
- Live injection test: Claude returned NO_TRADE, `execution_authorized=False`

## 2C-2 — Prompt versioning (validated earlier)
- `PROMPT_ARCHITECTURE_VERSION` defined once in `prompts.py`, imported by `context.py`

## 2C-3 — Task prompt architecture
| Item | Detail |
|---|---|
| `tasks.py` | 6-task taxonomy (`MARKET_ANALYSIS`, `MARKET_RESEARCH`, `STRATEGY_ANALYSIS`, `RISK_ANALYSIS`, `TRADE_REVIEW`, `GENERAL_RESEARCH`); immutable registry; import-time completeness check; `resolve_task()` rejects unknown tasks |
| `prompts.py` | Repaired truncated `build_analysis_prompt`; completed `build_task_prompt`; shared safety blocks used by both builders; `SYSTEM_PROMPT` renders the version from the constant (text otherwise identical to 2C-1) |
| Injection hardening | `escape_context_data()` converts `< > &` in context data to JSON unicode escapes — same JSON meaning, but data can no longer close `<friday_context_data>` or forge prompt tags |
| Claude adapter | **Unchanged** (Phase 2B baseline preserved) |
| Tests | `test_prompt_tasks.py` (35 tests) + version assertion update in `test_intelligence.py` |

### Validation (Claude build environment)
```
54 passed, 1 skipped (live API test — runs on jarvis020 with .env)
ALL CHECKS: PASS
Security scan: PASS
```
Server validation: `bash scripts/validate_phase_2c3.sh` on jarvis020.

## 2C-4 — Claude adapter integration
| Item | Detail |
|---|---|
| `claude.py` | `analyse(context, task=GENERAL_RESEARCH)`; uses `build_task_prompt`; sends `context.prompt_payload()` so version + authority labels reach Claude; unknown task → safe NO_TRADE without any API call; `ClaudeResult` records `task` and `prompt_architecture_version`; `build_request_prompt()` exposes the exact prompt for audit |
| `schemas.py` | `metadata.execution_authorized=true` in a Claude reply is **rejected** (was silently accepted) |
| `validation.py` | Execution-authority invariant is an explicit check, not `assert` (which `python -O` strips) |
| `requirements.txt` | `anthropic==1.6.0` (validated on jarvis020) |
| CI | Installs `requirements-dev.txt` |
| Tests | `test_claude_adapter.py` (19 offline, fake client) · `test_claude_live_tasks.py` (6 live: every task with an injection attempt) |
| Prompt version | Unchanged at **2C.3** — prompt text did not change in 2C-4 |

Server validation: `bash scripts/validate_phase_2c4.sh` on jarvis020.

## Open items carried forward
1. **Dependency pins** — `requirements.txt` predates the Python 3.14 server: `pandas==2.3.2` and `PyMuPDF==1.26.4` have no 3.14 wheels, so jarvis020 must run other versions. The 2C-4 validation prints every drift; pins will be reconciled to the validated server environment.
2. **CI Python** — GitHub Actions stays on 3.12 until pins are reconciled, then moves to 3.14 to match jarvis020.
