# Phase 5 — Research & Book Ingestion Engine (v0.5)

## Decisions (operator, 2026-09-25)
- Sources: books you own (PDF/EPUB), research papers (PDF), your notes and
  text files (MD/TXT/HTML), and web articles by URL (5B).
- Claude: everything is indexed locally for free. Claude sees only the few
  relevant passages per question, and writes a full digest only on request,
  under a separate research budget (5C).
- Storage: full text is on the **server only, never in Git**. Only FRIDAY's
  own research notes go to the vault repo (5C).
- FRIDAY runs as a systemd service (5A).

## Sub-phases
| Sub-phase | Scope |
|---|---|
| **5A Library + service** | Private library at `~/friday/library` (0700, refused if inside any Git repo); PDF / EPUB / HTML / MD / TXT extraction; SHA-256 dedupe; paragraph-aware chunks with page/chapter locations; secret-looking passages withheld; SQLite FTS5 search; inbox + CLI; `/api/books` ingests into the library; `/api/research/status|search`; systemd unit + installer |
| **5B Web articles + jobs** | Hardened URL fetcher (HTTPS only, size/time/content-type caps, no private or internal addresses, redirect limits); ingestion as Hermes jobs; research API |
| **5C Claude on research** | Top-k cited passages in Claude's context as untrusted data (like vault recall); on-request `RESEARCH_DIGEST` job with its own token budget; digest notes in `FRIDAY/Research/`, every idea marked *hypothesis, not validated* |

## 5A contract
1. Library content is **UNTRUSTED DATA**, the same as vault notes. Nothing in a
   book can instruct FRIDAY.
2. Nothing in a file is executed. PDF JavaScript, EPUB and HTML scripts, and
   navigation are dropped; only text is read. EPUB paths are confined to the
   archive, with member-count and expanded-size caps against zip bombs.
3. The following are rejected with a reason, and nothing is stored: fake
   extensions, password-protected PDFs, scanned PDFs with no text layer (OCR
   is not supported yet), files over 200 MB, and symlinks.
4. Originals are kept read-only under `originals/<ab>/<sha256>.<ext>`. `remove`
   needs `--confirm`.
5. The library root must be outside any Git working tree. The old Phase 0
   `friday/books/` folder is git-ignored, and uploads no longer go there.
6. Search is local (no network, no Claude).

## 5B contract — web articles and ingestion jobs
1. `SafeFetcher` fetches exactly one operator-supplied URL. It is not a crawler.
   - **https only, port 443 only, no credentials in the URL.**
   - Names like `localhost`, `*.local`, `*.lan`, `*.internal` and `*.home.arpa` are refused.
   - The host is resolved first. If any address is not public (loopback, LAN,
     link-local including cloud metadata `169.254.169.254`, CGNAT, multicast,
     reserved, IPv4-mapped IPv6), the fetch is refused before connecting.
   - The connected peer address is re-checked before the body is read, which
     defends against DNS rebinding. If the peer can't be confirmed, the fetch
     fails closed.
   - Redirects are followed manually, at most 3, and each one is re-validated.
   - **Content-type allowlist:** HTML/XHTML, text, Markdown, PDF, EPUB.
   - **Limits:** 25 MB per download (header and stream), 10 s connect, 20 s read.
   - No proxies or environment settings (`trust_env=False`), no cookies, no auth.
2. For HTML, only `<article>`/`<main>` text is kept when present; nav, scripts
   and footers are dropped. A URL already in the library is a DUPLICATE and is
   not re-fetched, unless `refresh` is set, which replaces the old copy.
3. Hermes capability `INGEST_RESEARCH`, granted only to `RESEARCH_INGEST_URL`
   (at most hourly if scheduled) and `RESEARCH_INGEST_INBOX` (minimum 300 s;
   the `research-inbox` schedule runs every 600 s by default).
   - A refused fetch is a normal REJECTED result, never retried.
   - Both jobs are journalled in the vault; empty inbox runs are silent.
4. `inbox/paper/`, `inbox/note/` and `inbox/article/` set the kind; the top level is books.
5. **API:** `POST /api/research/url`, `POST /api/research/ingest-inbox`,
   `GET /api/research/documents`. Server paths are never exposed, and there is
   no delete over the API, which has no authentication yet.

## 5C contract — Claude on research
1. **Recall.** Every `INTELLIGENCE_ANALYSIS` gets up to 3 cited passages from the
   library (≤2,400 chars in total) at `research_context.research_library`, labelled
   `UNTRUSTED_DATA`. Callers cannot supply or spoof that key.
   - Reference pages rank lower: index, glossary, contents and figure lists
     (headings, page-number density, list-like text).
   - The analysis note lists the passages consulted.
   - Setting: `FRIDAY_RESEARCH_RECALL`.
2. **Digests** run on request only (`RESEARCH_DIGEST`; never scheduled more than hourly).
   - Claude reads the next ≤10 sections (~12k chars each) of one document,
     using the unchanged 2C.3 contract and task `GENERAL_RESEARCH`, with
     NO_TRADE required.
   - The text travels as `UNTRUSTED_DATA`.
   - Progress resumes from the library. A fallback reply stops the digest
     without advancing progress. Each job has a 600 s working limit and a
     900 s job timeout.
3. **A separate research budget** (`FRIDAY_RESEARCH_DAILY_TOKEN_BUDGET`, default
   200k/day, key `research:<day>`, 14k reserve per call). Digests can never use
   up the analysis budget. The analysis per-call reserve rose from 10k to 12k,
   to cover vault recall + library recall + 3k output.
4. **Digest notes** go to `FRIDAY/Research/<title>-<id>.md`, one section per heading:
   - summary, the author's claims (cited), hypotheses marked **not validated**,
     risks, and open questions;
   - they sync to the vault repo like all FRIDAY notes;
   - each digest gets a journal line.
5. **Capability** `READ_RESEARCH` belongs to analysis and digests only. Only
   analysis and digests may ask Claude.
