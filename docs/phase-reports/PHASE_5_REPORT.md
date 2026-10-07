# Phase 5 — Research & Book Ingestion Engine: Report

Status: **5A, 5B signed off** · **5C awaiting sign-off**
Library **5B.1** · Fetcher **5B.2**
Execution: **LOCKED** · Live trading: **DISABLED**

Design: `docs/architecture/PHASE_5_RESEARCH.md`

## 5A — Private research library + FRIDAY service
| Item | Detail |
|---|---|
| `friday/research/extract.py` | PDF (PyMuPDF, per page), EPUB (stdlib, spine order, per chapter, zip-bomb/traversal safe), HTML (scripts/nav dropped), Markdown, text. Rejects fake/locked/scanned files with reasons. |
| `friday/research/library.py` | `ResearchLibrary`: 0700 root outside Git; read-only originals; SHA-256 dedupe; ~1,200-char paragraph-aware chunks keeping page/chapter; secret-looking passages withheld; SQLite FTS5 (BM25) search, all words and then any word, injection-safe; inbox and rejected folders; remove with confirmation. |
| CLI | `python -m friday.research init / add / ingest-inbox / search / list / status / remove` |
| App | Component `research`; `GET /api/research/status`, `GET /api/research/search` (results labelled `UNTRUSTED_DATA`); `POST /api/books` now streams into the library (200 MB cap, sanitised name, staging cleaned up) instead of saving inside the repo. |
| Service | `deploy/systemd/friday.service.template`, `scripts/install_friday_service.sh`: runs as you (not root), 127.0.0.1:8000, restart on failure, hardened (NoNewPrivileges, PrivateTmp, ProtectSystem=full, UMask 0077). |
| Tests | `test_research_library.py`: 22 tests |
| Settings | `FRIDAY_LIBRARY_PATH` (default `~/friday/library`) |

Server validation: `bash scripts/validate_phase_5a.sh` on jarvis020.

## 5B — Web articles by URL + ingestion as Hermes jobs
| Item | Detail |
|---|---|
| `friday/research/fetch.py` | `SafeFetcher`: https:443 only; public addresses only (pre-resolve **and** post-connect peer check, fails closed); ≤3 re-validated redirects; content-type allowlist; 25 MB per download; timeouts; no proxy, env settings, cookies or auth. |
| Library | `add_url()` (URL is the source; duplicate by URL; `refresh` replaces the old copy); HTML keeps `<article>`/`<main>` text; `inbox/<kind>/` subfolders. |
| Hermes | Capability `INGEST_RESEARCH`; jobs `RESEARCH_INGEST_URL` and `RESEARCH_INGEST_INBOX`; `research-inbox` schedule (600 s); journal lines `**RESEARCH**` / `**RESEARCH INBOX**`. |
| App | `POST /api/research/url`, `POST /api/research/ingest-inbox`, `GET /api/research/documents` |
| CLI | `python -m friday.research add-url URL [--kind article|paper] [--refresh]` |
| Tests | `test_research_ingest.py`: 58 tests, using a fake network |
| Validation | Real fetch of a public article, plus real-DNS refusals: 127.0.0.1, localhost, 169.254.169.254, `localtest.me` (public name resolving to 127.0.0.1), 192.168.1.1, http://, port 8443 |

### 5B fixes (found by server validation)
- **Wikipedia HTTP 403:** the User-Agent now carries a contact URL, as Wikimedia's policy requires (`FRIDAY_FETCH_CONTACT`, default: operator's public GitHub profile URL). A 401/403/429 now says the site refused automated access. Validation also fetches an IETF RFC, so one site's policy can't mask the fetcher's health.
- **Test isolation:** after the real `~/friday/library` existed, app tests saw its inbox schedule. `friday/tests/conftest.py` now points every test at an empty temporary library and vault unless the test sets its own.

## 5C — Claude on research
| Item | Detail |
|---|---|
| `friday/research/recall.py` | `ResearchRecall` (5C.1): FTS candidates, re-ranked to push down index/glossary/contents pages; ≤3 passages from different pages, ≤2,400 chars, `UNTRUSTED_DATA`, cited by title and page. |
| Digests | Job `RESEARCH_DIGEST` (on request; ≤10 sections; resumable; stops on a fallback reply or budget; 900 s timeout), unchanged 2C.3 contract, NO_TRADE required. Notes go to `FRIDAY/Research/` with hypotheses marked *not validated*. |
| Budgets | A separate research budget of 200k/day (key `research:<day>`); the analysis reserve went from 10k to 12k. |
| Hermes | Capability `READ_RESEARCH` (analysis and digests only); only analysis and digests may ask Claude. |
| App / CLI | `POST /api/research/digest`; documents list shows digest progress; `python -m friday.research digest DOC_ID` (through the running service) and `digest-status`. |
| Tests | `test_research_digest.py`: 17 tests; opt-in live test `test_research_live.py` (2 Claude calls) |

### 5C fix (found on the first real digest)
The first Brooks section came back as a fallback after 11.8k tokens: Claude's reply for a dense 12k-char section hit the 3,000-token output ceiling. Digests now ask for a compact reply (assessment ≈120 words; ≤6 claims, 4 hypotheses, 4 risks, 4 open questions), retry a truncated section once in a briefer form, and report the actual fallback reason. Progress never advances past a failed section.

### 5C fix 2 (found on jarvis020)
- **Notes switched off after the 5C restart:** adding the `FRIDAY/Research` folder made the existing vault look uninitialised, so the recorder (and vault sync) disabled themselves. FRIDAY now creates missing folders inside its own `FRIDAY/` zone; a vault missing any of *your* folders is still refused.
- **The recorder never loses a failure:** errors while building a note are now counted and shown in `last_error`.
- **Replay:** `POST /api/knowledge/replay` and `python -m friday.knowledge replay` rebuild a finished job's notes from its saved result, with no Claude call and no tokens.
- The live test accepts any non-trading contract action (RESEARCH/ANALYSE/WATCH/NO_TRADE).
- The 5C validation now reports whether the running service has notes enabled.
