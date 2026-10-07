"""
FRIDAY research library — private, server-only document store + search.

Phase 5A:
- Layout under the library root (default ~/friday/library):
      originals/<ab>/<sha256>.<ext>   read-only copy of every ingested file
      library.db                      SQLite: documents + FTS5 chunk index
      inbox/                          drop files here, then `ingest-inbox`
      rejected/                       files that could not be ingested (+ .reason.txt)
- The root must NOT be inside a Git working tree, so book text can never be
  committed by accident (full text stays on jarvis020, by decision).
- Deduplicated by SHA-256; re-adding the same file is a no-op.
- Text is split into ~1,200-character chunks on paragraph/sentence
  boundaries, each keeping its location (page / chapter).
- Chunks that look like they contain a secret are withheld from the index.
- Search is local SQLite FTS5 (BM25): no network, no Claude, no cost.
- Everything in the library is UNTRUSTED DATA when later shown to Claude.

Phase 5B:
- add_url(): fetch ONE https page/PDF through SafeFetcher, then ingest it;
  a URL already in the library is a DUPLICATE unless refresh=True
- inbox/<kind>/ subfolders (paper/, note/, article/) set the kind;
  files at the top of inbox/ are books
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from friday.knowledge.vault import find_secrets

from .extract import SUPPORTED, ExtractionError, Extracted, extract
from .fetch import FetchError, SafeFetcher

RESEARCH_LIBRARY_VERSION = "5C.1"
DEFAULT_LIBRARY_PATH = "~/friday/library"
MAX_FILE_BYTES = 200 * 1024 * 1024
CHUNK_CHARS = 1200
KINDS = ("book", "paper", "note", "article")

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS documents(
    doc_id TEXT PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    kind TEXT NOT NULL,
    format TEXT NOT NULL,
    source TEXT NOT NULL,
    original TEXT NOT NULL,
    pages INTEGER,
    chars INTEGER NOT NULL,
    chunks INTEGER NOT NULL,
    withheld_chunks INTEGER NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_progress(
    doc_id TEXT PRIMARY KEY,
    next_section INTEGER NOT NULL DEFAULT 0,
    total_sections INTEGER NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
    text, doc_id UNINDEXED, location UNINDEXED, seq UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


class LibraryError(ValueError):
    pass


@dataclass
class IngestResult:
    status: str                  # ADDED | DUPLICATE | REJECTED
    name: str
    doc_id: str | None = None
    title: str | None = None
    chunks: int = 0
    withheld_chunks: int = 0
    reason: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inside_git_worktree(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Paragraph-aware chunks of roughly `size` characters."""
    chunks, current = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        pieces = [para]
        if len(para) > size:        # split long paragraphs on sentence ends
            pieces, buf = [], ""
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                while len(sentence) > size:
                    pieces.append(sentence[:size])
                    sentence = sentence[size:]
                if len(buf) + len(sentence) + 1 > size and buf:
                    pieces.append(buf)
                    buf = ""
                buf = f"{buf} {sentence}".strip()
            if buf:
                pieces.append(buf)
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > size:
                chunks.append(current)
                current = ""
            current = f"{current}\n\n{piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def _fts_query(query: str, any_term: bool = False) -> str | None:
    terms = [t for t in re.findall(r"[\w'-]{2,}", query.lower()) if t not in ("and", "or", "not", "near")]
    if not terms:
        return None
    quoted = ['"' + t.replace('"', "") + '"' for t in terms[:20]]
    return (" OR " if any_term else " ").join(quoted)


class ResearchLibrary:
    def __init__(self, root: str | Path = DEFAULT_LIBRARY_PATH, allow_inside_git: bool = False):
        self.root = Path(root).expanduser().resolve()
        repo = _inside_git_worktree(self.root)
        if repo is not None and not allow_inside_git:
            raise LibraryError(
                f"library path {self.root} is inside the Git repository {repo}; "
                "book text must never be committed — choose a path outside any repo")
        self.originals = self.root / "originals"
        self.inbox = self.root / "inbox"
        self.rejected = self.root / "rejected"
        self.db_path = self.root / "library.db"
        self._lock = threading.RLock()

    # ------------------------------------------------------------ setup

    def init(self) -> "ResearchLibrary":
        for folder in (self.root, self.originals, self.inbox, self.rejected,
                       *(self.inbox / k for k in KINDS if k != "book")):
            folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)            # private to the FRIDAY user
        with self._db() as db:
            db.executescript(_SCHEMA)
        return self

    @property
    def initialised(self) -> bool:
        return self.db_path.is_file()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ ingest

    def add(self, path: str | Path, *, kind: str = "book", title: str | None = None,
            source: str | None = None, tags: tuple[str, ...] = ()) -> IngestResult:
        path = Path(path).expanduser()
        name = path.name
        if kind not in KINDS:
            return IngestResult("REJECTED", name, reason=f"kind must be one of {', '.join(KINDS)}")
        try:
            if path.is_symlink() or not path.is_file():
                raise ExtractionError("not a regular file")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ExtractionError(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self._lock, self._db() as db:
                row = db.execute("SELECT doc_id, title FROM documents WHERE sha256=?", (digest,)).fetchone()
            if row:
                return IngestResult("DUPLICATE", name, doc_id=row["doc_id"], title=row["title"],
                                    reason="already in the library")
            extracted = extract(path)
        except ExtractionError as exc:
            return IngestResult("REJECTED", name, reason=str(exc))
        return self._store(path, digest, extracted, kind=kind, title=title,
                           source=source or name, tags=tags)

    def _store(self, path: Path, digest: str, ex: Extracted, *, kind: str, title: str | None,
               source: str, tags: tuple[str, ...]) -> IngestResult:
        doc_id = digest[:16]
        final_title = (title or ex.title or path.stem.replace("_", " ")).strip()[:200]
        rows, withheld = [], 0
        for section in ex.sections:
            for chunk in chunk_text(section.text):
                if find_secrets(chunk):
                    withheld += 1
                    continue
                rows.append((chunk, doc_id, section.location, len(rows)))
        if not rows:
            return IngestResult("REJECTED", path.name, reason="every passage was withheld as sensitive")

        suffix = path.suffix.lower() if path.suffix.lower() in SUPPORTED else ".bin"
        original = self.originals / digest[:2] / f"{digest}{suffix}"
        original.parent.mkdir(parents=True, exist_ok=True)
        if not original.exists():
            shutil.copyfile(path, original)
            os.chmod(original, stat.S_IRUSR)
        clean_tags = sorted({t.strip().lower()[:40] for t in tags if t and t.strip()})
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO documents(doc_id, sha256, title, author, kind, format, source, original,"
                " pages, chars, chunks, withheld_chunks, tags, added_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, digest, final_title, ex.author, kind, ex.format, source[:500],
                 str(original.relative_to(self.root)), ex.pages, ex.chars, len(rows), withheld,
                 json.dumps(clean_tags), _now()))
            db.executemany("INSERT INTO passages(text, doc_id, location, seq) VALUES(?,?,?,?)", rows)
        return IngestResult("ADDED", path.name, doc_id=doc_id, title=final_title,
                            chunks=len(rows), withheld_chunks=withheld)

    def find_by_source(self, source: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT doc_id, title FROM documents WHERE source=?", (source[:500],)).fetchone()
        return dict(row) if row else None

    def add_url(self, url: str, *, kind: str = "article", title: str | None = None,
                tags: tuple[str, ...] = (), refresh: bool = False,
                fetcher: SafeFetcher | None = None) -> IngestResult:
        """Fetch one https URL safely and ingest it (source = the URL)."""
        from .fetch import check_url
        try:
            url, _ = check_url(url)
        except FetchError as exc:
            return IngestResult("REJECTED", url[:200], reason=str(exc))
        existing = self.find_by_source(url)
        if existing and not refresh:
            return IngestResult("DUPLICATE", url, doc_id=existing["doc_id"], title=existing["title"],
                                reason="this URL is already in the library")
        staging = self.inbox / f".fetch-{uuid.uuid4().hex}"
        try:
            try:
                fetched = (fetcher or SafeFetcher()).fetch(url, staging)
            except FetchError as exc:
                return IngestResult("REJECTED", url, reason=str(exc))
            result = self.add(fetched.path, kind=kind, title=title, source=fetched.url, tags=tags)
            result.name = url
            if result.status == "ADDED" and existing and refresh:
                self.remove(existing["doc_id"])           # replaced by the refreshed copy
            return result
        finally:
            if staging.exists():
                for leftover in staging.iterdir():
                    leftover.unlink()
                staging.rmdir()

    def _inbox_files(self, default_kind: str) -> list[tuple[Path, str]]:
        files = []
        for path in sorted(self.inbox.iterdir()):
            if path.name.startswith("."):
                continue
            if path.is_dir() and path.name in KINDS:
                files += [(p, path.name) for p in sorted(path.iterdir())
                          if p.is_file() and not p.name.startswith(".")]
            elif path.is_file():
                files.append((path, default_kind))
        return files

    def ingest_inbox(self, kind: str = "book") -> list[IngestResult]:
        """Ingest every file in inbox/ (and inbox/<kind>/ subfolders):
        added/duplicates leave the inbox, rejects move to rejected/ with a
        .reason.txt."""
        results = []
        for path, file_kind in self._inbox_files(kind):
            result = self.add(path, kind=file_kind)
            results.append(result)
            if result.status in ("ADDED", "DUPLICATE"):
                path.unlink()
            else:
                target = self.rejected / path.name
                shutil.move(str(path), target)
                (self.rejected / f"{path.name}.reason.txt").write_text(result.reason or "", encoding="utf-8")
        return results

    # ------------------------------------------------------------ read

    def search(self, query: str, limit: int = 5, kind: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        hits: list[dict[str, Any]] = []
        for any_term in (False, True):          # all words first, then any word
            fts = _fts_query(query, any_term)
            if not fts:
                return []
            sql = ("SELECT p.doc_id, p.location, p.seq, p.text, bm25(passages) AS score,"
                   " snippet(passages, 0, '[', ']', '…', 24) AS snippet, d.title, d.kind, d.author"
                   " FROM passages p JOIN documents d ON d.doc_id = p.doc_id"
                   " WHERE passages MATCH ?" + (" AND d.kind = ?" if kind else "") +
                   " ORDER BY score LIMIT ?")
            params = [fts] + ([kind] if kind else []) + [limit]
            with self._db() as db:
                hits = [dict(r) for r in db.execute(sql, params)]
            if hits:
                break
        for hit in hits:
            hit["score"] = round(-hit["score"], 3)       # higher is better
        return hits

    def locate(self, doc_id: str, query: str, limit: int = 200) -> list[dict[str, Any]]:
        """11B: where in ONE document all the words of `query` appear together. Returns locations and
        passage numbers only - never text - so a caller can cite a source without copying it."""
        fts = _fts_query(query, any_term=False)
        if not fts:
            return []
        with self._db() as db:
            rows = db.execute("SELECT location, seq FROM passages WHERE passages MATCH ? AND doc_id = ?"
                              " ORDER BY CAST(seq AS INTEGER) LIMIT ?", (fts, doc_id, max(1, min(int(limit), 1000)))).fetchall()
        return [{"location": r["location"], "seq": r["seq"]} for r in rows]

    # ------------------------------------------------------------ digests (5C)

    def sections(self, doc_id: str, size: int = 12_000) -> list[dict[str, Any]]:
        """Consecutive passages grouped into ~`size`-char digest sections."""
        with self._db() as db:
            rows = db.execute("SELECT text, location, seq FROM passages WHERE doc_id=? "
                              "ORDER BY CAST(seq AS INTEGER)", (doc_id,)).fetchall()
        sections, current = [], {"texts": [], "locations": []}
        for row in rows:
            if current["texts"] and sum(len(t) for t in current["texts"]) + len(row["text"]) > size:
                sections.append(current)
                current = {"texts": [], "locations": []}
            current["texts"].append(row["text"])
            if row["location"] and row["location"] not in current["locations"]:
                current["locations"].append(row["location"])
        if current["texts"]:
            sections.append(current)
        out = []
        for index, sec in enumerate(sections):
            locs = sec["locations"]
            span = (locs[0] if len(locs) == 1 else f"{locs[0]} – {locs[-1]}") if locs else f"section {index + 1}"
            out.append({"index": index, "locations": span, "text": "\n\n".join(sec["texts"])})
        return out

    def digest_progress(self, doc_id: str) -> dict[str, Any]:
        with self._db() as db:
            db.executescript(_SCHEMA.split("CREATE VIRTUAL TABLE")[0])   # older libraries
            row = db.execute("SELECT * FROM digest_progress WHERE doc_id=?", (doc_id,)).fetchone()
        return dict(row) if row else {"doc_id": doc_id, "next_section": 0, "total_sections": None,
                                      "tokens_used": 0, "updated_at": None}

    def digest_plan(self, doc_id: str, max_sections: int, start: int | None = None) -> dict[str, Any]:
        doc = self.get(doc_id)
        if doc is None:
            raise LibraryError(f"no document {doc_id!r}")
        sections = self.sections(doc_id)
        begin = self.digest_progress(doc_id)["next_section"] if start is None else max(0, int(start))
        chosen = sections[begin: begin + max(1, int(max_sections))]
        return {"doc_id": doc_id, "title": doc["title"], "kind": doc["kind"], "author": doc["author"],
                "source": doc["source"], "total_sections": len(sections), "start": begin,
                "sections": chosen}

    def mark_digested(self, doc_id: str, next_section: int, total: int, tokens: int) -> None:
        with self._lock, self._db() as db:
            db.executescript(_SCHEMA.split("CREATE VIRTUAL TABLE")[0])
            db.execute(
                "INSERT INTO digest_progress(doc_id, next_section, total_sections, tokens_used, updated_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET"
                " next_section=MAX(next_section, excluded.next_section),"
                " total_sections=excluded.total_sections,"
                " tokens_used=tokens_used + excluded.tokens_used, updated_at=excluded.updated_at",
                (doc_id, int(next_section), int(total), int(tokens), _now()))

    def documents(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM documents ORDER BY added_at DESC").fetchall()
        return [{**dict(r), "tags": json.loads(r["tags"])} for r in rows]

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        return {**dict(row), "tags": json.loads(row["tags"])} if row else None

    def remove(self, doc_id: str) -> bool:
        with self._lock, self._db() as db:
            row = db.execute("SELECT original FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM passages WHERE doc_id=?", (doc_id,))
            db.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        original = self.root / row["original"]
        if original.exists():
            os.chmod(original, stat.S_IRUSR | stat.S_IWUSR)
            original.unlink()
        return True

    def status(self) -> dict[str, Any]:
        base = {"library": str(self.root), "version": RESEARCH_LIBRARY_VERSION,
                "initialised": self.initialised}
        if not self.initialised:
            return {**base, "documents": 0, "chunks": 0}
        with self._db() as db:
            by_kind = {r["kind"]: r["n"] for r in db.execute(
                "SELECT kind, COUNT(*) n FROM documents GROUP BY kind")}
            chunks = db.execute("SELECT COALESCE(SUM(chunks),0) n FROM documents").fetchone()["n"]
        inbox = self._inbox_files("book") if self.inbox.is_dir() else []
        return {**base, "documents": sum(by_kind.values()), "by_kind": by_kind,
                "chunks": chunks, "inbox_waiting": len(inbox)}


def result_dict(result: IngestResult) -> dict[str, Any]:
    return asdict(result)
