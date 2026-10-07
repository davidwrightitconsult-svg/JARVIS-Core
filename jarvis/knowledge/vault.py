"""
FRIDAY Knowledge Vault (Obsidian-compatible Markdown).

Phase 4A — contract:
- Two ownership zones:
    HUMAN  : everything outside FRIDAY/  (Constitution, Risk Doctrine,
             Markets, Strategies, Research, ...). FRIDAY may READ these,
             never write, rename or delete them.
    FRIDAY : FRIDAY/<Journal|Analyses|Hermes|Market-Snapshots>/ only.
             FRIDAY writes here automatically.
- Every path is confined to the vault root: no absolute paths, no '..',
  no hidden files/folders (.git, .obsidian), no symlink escapes.
- Writes are atomic (temp file + rename), size-capped, and refused if the
  content appears to contain a secret (API keys, tokens, private keys,
  or the value of any secret-named environment variable).
- Everything read from the vault is DATA (trust = UNTRUSTED_DATA), never
  instructions — including the Constitution.
- There is no delete operation.
"""

from __future__ import annotations

import os
import re
import tempfile
from enum import Enum
from pathlib import Path

from .notes import NoteMeta, ParsedNote, parse_note, render_note
from .seed import GITIGNORE, SEED_FILES

VAULT_CONTRACT_VERSION = "4A.1"

HUMAN_FOLDERS: tuple[str, ...] = (
    "00-Constitution",
    "01-Risk-Doctrine",
    "02-Markets",
    "03-Strategies",
    "04-Research",
    "05-Books",
    "06-Experiments",
    "07-Trading-Journal",
    "08-Lessons",
    "09-Models",
)
JARVIS_ROOT = "FRIDAY"
JARVIS_FOLDERS: tuple[str, ...] = ("Journal", "Analyses", "Hermes", "Market-Snapshots", "Research", "Strategies")

MAX_NOTE_BYTES = 256 * 1024
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,120}$")

_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE " r"KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(private[_ -]?key|secret[_ -]?key|passphrase)\b\s*[:=]\s*\S{8,}"),
    # Wallet recovery phrases: a label followed by 6+ lowercase words.
    re.compile(r"(?i)\b(mnemonic|seed[_ -]?phrase|recovery[_ -]?phrase)\b\s*[:=]\s*(?:[a-z]+\s+){5,}[a-z]+"),
)
_SECRET_ENV_NAME = re.compile(r"(?i)(KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE)")


class Zone(str, Enum):
    HUMAN = "HUMAN"
    FRIDAY = "FRIDAY"


class VaultError(ValueError):
    """Invalid vault path or operation."""


class VaultPermissionError(PermissionError):
    """FRIDAY attempted to write outside its own zone."""


class SecretDetectedError(ValueError):
    """Content appears to contain secret material; nothing was written."""


def _known_secret_values() -> list[str]:
    return [
        value for name, value in os.environ.items()
        if _SECRET_ENV_NAME.search(name) and value and len(value) >= 12
    ]


def find_secrets(text: str) -> list[str]:
    """Return reasons (never the secret itself) if text looks sensitive."""
    reasons = [f"pattern:{i}" for i, p in enumerate(_SECRET_PATTERNS) if p.search(text)]
    if any(value in text for value in _known_secret_values()):
        reasons.append("environment-secret-value")
    return reasons


class KnowledgeVault:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------ paths

    def _resolve(self, relpath: str) -> Path:
        rel = str(relpath).replace("\\", "/").strip()
        if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            raise VaultError(f"Vault paths must be relative: {relpath!r}")
        parts = [p for p in rel.split("/") if p != ""]
        for part in parts:
            if part in (".", "..") or part.startswith(".") or not _COMPONENT.match(part):
                raise VaultError(f"Illegal path component {part!r} in {relpath!r}")
        candidate = self.root.joinpath(*parts)
        # Reject symlinks anywhere along the existing path.
        probe = self.root
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                raise VaultError(f"Symlinks are not allowed in the vault: {relpath!r}")
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise VaultError(f"Path escapes the vault: {relpath!r}")
        return candidate

    def zone_of(self, relpath: str) -> Zone:
        first = str(relpath).replace("\\", "/").lstrip("/").split("/", 1)[0]
        return Zone.FRIDAY if first == JARVIS_ROOT else Zone.HUMAN

    def _require_friday_zone(self, relpath: str) -> Path:
        path = self._resolve(relpath)
        parts = path.relative_to(self.root).parts
        if len(parts) < 3 or parts[0] != JARVIS_ROOT or parts[1] not in JARVIS_FOLDERS:
            raise VaultPermissionError(
                f"FRIDAY may only write to {JARVIS_ROOT}/<{'|'.join(JARVIS_FOLDERS)}>/…; "
                f"refused: {relpath!r}"
            )
        if path.suffix != ".md":
            raise VaultError("FRIDAY notes must be Markdown (.md)")
        return path

    # ------------------------------------------------------------ writing

    def _atomic_write(self, path: Path, text: str) -> None:
        data = text.encode("utf-8")
        if len(data) > MAX_NOTE_BYTES:
            raise VaultError(f"Note exceeds {MAX_NOTE_BYTES} bytes")
        reasons = find_secrets(text)
        if reasons:
            raise SecretDetectedError(f"Refused to write {path.name}: possible secret ({', '.join(reasons)})")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def write_note(self, relpath: str, meta: NoteMeta, body: str, overwrite: bool = False) -> Path:
        """Write a FRIDAY-zone note. Refuses every other location."""
        path = self._require_friday_zone(relpath)
        if path.exists() and not overwrite:
            raise VaultError(f"Note already exists: {relpath!r}")
        self._atomic_write(path, render_note(meta, body))
        return path

    def append_to_note(self, relpath: str, meta_if_new: NoteMeta, text: str) -> Path:
        """Append a section to a FRIDAY-zone note, creating it if needed."""
        path = self._require_friday_zone(relpath)
        if path.exists():
            current = path.read_text(encoding="utf-8")
            new = current.rstrip() + "\n\n" + text.rstrip() + "\n"
        else:
            new = render_note(meta_if_new, text)
        self._atomic_write(path, new)
        return path

    # ------------------------------------------------------------ reading

    def read_note(self, relpath: str) -> ParsedNote:
        path = self._resolve(relpath)
        if not path.is_file() or path.suffix != ".md":
            raise VaultError(f"Not a note: {relpath!r}")
        rel = path.relative_to(self.root).as_posix()
        return parse_note(path.read_text(encoding="utf-8"), path=rel, zone=self.zone_of(rel).value)

    def list_notes(self, folder: str = "", limit: int = 200) -> list[str]:
        base = self._resolve(folder) if folder else self.root
        if not base.is_dir():
            return []
        found = []
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts) or path.is_symlink():
                continue
            found.append(rel.as_posix())
            if len(found) >= limit:
                break
        return found

    # ------------------------------------------------------------ setup

    def init(self) -> list[str]:
        """
        Create the folder structure and seed files. Idempotent and
        non-destructive: an existing file is NEVER overwritten, so the
        human-owned Constitution and Risk Doctrine stay yours after the
        first run.
        """
        created = []
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in HUMAN_FOLDERS:
            (self.root / folder).mkdir(exist_ok=True)
        for folder in JARVIS_FOLDERS:
            (self.root / JARVIS_ROOT / folder).mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            self._atomic_write(gitignore, GITIGNORE)
            created.append(".gitignore")
        for relpath, content in SEED_FILES.items():
            path = self._resolve(relpath)
            if path.exists():
                continue
            self._atomic_write(path, content)
            created.append(relpath)
        return created

    def check(self) -> dict:
        """Structure report for validation (read-only)."""
        missing = [f for f in HUMAN_FOLDERS if not (self.root / f).is_dir()]
        missing += [f"{JARVIS_ROOT}/{f}" for f in JARVIS_FOLDERS
                    if not (self.root / JARVIS_ROOT / f).is_dir()]
        seeds = {rel: (self.root / rel).is_file() for rel in SEED_FILES}
        return {
            "root": str(self.root),
            "exists": self.root.is_dir(),
            "missing_folders": missing,
            "seed_files_present": seeds,
            "note_count": len(self.list_notes(limit=100000)) if self.root.is_dir() else 0,
            "contract_version": VAULT_CONTRACT_VERSION,
        }
