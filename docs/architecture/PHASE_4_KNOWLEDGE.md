# Phase 4 — Obsidian Knowledge System

FRIDAY's long-term, human-readable memory: an Obsidian-compatible Markdown
vault in its own private repository, **`FRIDAY-Knowledge-Vault`**, cloned on
jarvis020 at `~/friday/FRIDAY-Knowledge-Vault` (`FRIDAY_VAULT_PATH`).

## Operator decisions (22 Sep 2026)
| Decision | Choice |
|---|---|
| Where the vault is read | Server only for now (plain Markdown; open in Obsidian any time) |
| Where it lives | Separate private repo `FRIDAY-Knowledge-Vault` |
| FRIDAY write mode | Auto-write, **own area only** (`FRIDAY/`); human areas read-only |

## Ownership zones
```
FRIDAY-Knowledge-Vault/
├── 00-Constitution/      ┐
├── 01-Risk-Doctrine/     │
├── 02-Markets/ … 09-Models/  ├ HUMAN — FRIDAY reads only, never writes/renames/deletes
├── README.md             ┘
└── FRIDAY/               ─ FRIDAY — auto-write
    ├── Journal/          daily system journal
    ├── Analyses/         Claude intelligence results (advisory)
    ├── Hermes/           operational reports
    └── Market-Snapshots/ read-only market observations
```

## Rules
1. Writes confined to `FRIDAY/<Journal|Analyses|Hermes|Market-Snapshots>/*.md`.
2. No absolute paths, `..`, hidden paths (`.git`, `.obsidian`) or symlinks.
3. Atomic writes; 256 KB cap; no delete operation exists.
4. Secret guard: API keys, tokens, PEM keys, key/passphrase assignments,
   recovery phrases, and the value of any secret-named environment variable
   are refused before anything touches disk.
5. Every note FRIDAY writes records `execution_authorized: false`.
6. Everything read from the vault — including the Constitution — is
   **untrusted data** when passed to Claude. Notes cannot grant authority.
7. `init` seeds the Constitution and Risk Doctrine once and never overwrites.

## Sub-phases
| Sub-phase | Scope |
|---|---|
| **4A Contract** | Vault layout, zones, confined writer, secret guard, reader, seed + CLI |
| **4B FRIDAY writes** | Hermes capability `WRITE_KNOWLEDGE`; analysis notes after every Claude job; daily journal; Hermes/market reports |
| **4C Sync & recall** | Automatic local Git commits of `FRIDAY/`; push via a vault-only deploy key; Constitution/Risk Doctrine and relevant notes supplied to Claude as untrusted research context |

## 4C design notes
**Sync (`friday/knowledge/sync.py`, Hermes job `VAULT_SYNC`, capability `PUBLISH_KNOWLEDGE`)**
- Stages and commits exactly the checked `FRIDAY/` paths, using a pathspec commit,
  so your own staged or edited files are never committed, unstaged or reset.
- Every changed file is checked first (Markdown only, no symlinks, ≤256 KB,
  secret scan). One bad file refuses the whole sync.
- Refuses on the wrong branch or during a merge/rebase/cherry-pick.
- Push is opt-in (`FRIDAY_VAULT_PUSH`). It's a plain fast-forward
  (`HEAD:refs/heads/main`) with no force and no pull/merge/rebase. If GitHub is
  ahead, the push is reported as failed and you reconcile it.
- Git runs with `GIT_TERMINAL_PROMPT=0` and ssh `BatchMode=yes` (appended to the
  vault's `core.sshCommand`), so a missing key fails fast. Every git call has a
  60 s timeout.
- `VAULT_SYNC` deliberately lacks `WRITE_KNOWLEDGE`. A sync never journals
  itself, so it can't create the change the next sync would commit.
- Authentication: a **deploy key scoped to FRIDAY-Knowledge-Vault only**
  (write access), selected through the vault repo's `core.sshCommand`. The
  platform repo and your personal key are not involved.

**Recall (`friday/knowledge/recall.py`, capability `READ_KNOWLEDGE`, analysis jobs only)**
- Placed at `research_context.knowledge_vault`, inside `<friday_context_data>`,
  and labelled `UNTRUSTED_DATA` / `instruction_authority: NONE`. The Phase 2
  trust boundary and tag escaping apply. A caller cannot supply or spoof this key.
- Doctrine: the notes in `00-Constitution/` and `01-Risk-Doctrine/` share
  3,600 chars fairly. Related notes: up to 3 from the other human folders, by
  keyword overlap. The whole block is ≤5,500 chars (~+1.3–2k tokens), within
  the 10,000-token per-call reserve.
- FRIDAY's own `FRIDAY/` notes are never recalled, so FRIDAY doesn't treat its
  past output as evidence. Notes that look like they contain secrets are
  withheld entirely.
- Each analysis note lists the vault notes consulted as Obsidian links.
