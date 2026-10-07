"""
FRIDAY vault sync — commit FRIDAY's own notes and (optionally) push them.

Phase 4C:
- Commits ONLY paths under FRIDAY/. Anything you have staged or changed
  elsewhere in the vault is left exactly as it is (not committed, not
  unstaged, not reset).
- Before committing, every changed FRIDAY/ file is checked: Markdown
  only, no symlinks, size-capped, and secret-scanned. Any violation
  refuses the WHOLE sync — nothing is committed.
- Refuses while a merge/rebase is in progress or when the vault is on a
  different branch than configured.
- Push is opt-in (JARVIS_VAULT_PUSH=true, after the deploy key is set up)
  and is a plain fast-forward push: never --force, never pull/merge/rebase.
  If GitHub has commits FRIDAY does not, the push is reported as failed
  and the local commit is kept for you to reconcile.
- Git never prompts: GIT_TERMINAL_PROMPT=0 and ssh BatchMode=yes, so a
  missing key or passphrase fails fast instead of hanging Hermes.
- Every git call has a timeout. Output shown to you is redacted of
  anything that looks like a token.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .vault import JARVIS_ROOT, MAX_NOTE_BYTES, find_secrets

VAULT_SYNC_VERSION = "4C.1"
DEFAULT_SYNC_INTERVAL = 900
MIN_SYNC_INTERVAL = 300
GIT_TIMEOUT_SECONDS = 60
COMMIT_NAME = "FRIDAY"
COMMIT_EMAIL = "jarvis@jarvis020.local"

_REDACT = re.compile(r"(github_pat_|gh[pousr]_|sk-ant-)[A-Za-z0-9_-]+|https://[^@\s/]+@")


def _redact(text: str) -> str:
    return _REDACT.sub("[REDACTED]", str(text))[:400]


class VaultSyncError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


class VaultSync:
    def __init__(
        self,
        vault_root: str | Path,
        push: bool = False,
        remote: str = "origin",
        branch: str = "main",
        timeout: int = GIT_TIMEOUT_SECONDS,
        runner: Runner = subprocess.run,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.root = Path(vault_root).expanduser().resolve()
        self.push_enabled = bool(push)
        self.remote = remote
        self.branch = branch
        self.timeout = int(timeout)
        self._run = runner
        self._clock = clock
        self._lock = threading.Lock()
        self.runs = 0
        self.commits = 0
        self.pushes = 0
        self.last_result: dict[str, Any] | None = None

    # ------------------------------------------------------------ git

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        env["SSH_ASKPASS"] = "true"
        env["LC_ALL"] = "C"
        configured = self._git("config", "--get", "core.sshCommand", check=False, env=None).stdout.strip()
        env["GIT_SSH_COMMAND"] = f"{configured or 'ssh'} -o BatchMode=yes -o ConnectTimeout=20"
        return env

    def _git(self, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        try:
            proc = self._run(
                ["git", "-C", str(self.root), *args],
                capture_output=True, text=True, timeout=self.timeout,
                env=env if env is not None else {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            raise VaultSyncError(f"git {args[0]} timed out after {self.timeout}s") from None
        if check and proc.returncode != 0:
            raise VaultSyncError(f"git {args[0]} failed: {_redact(proc.stderr.strip() or proc.stdout.strip())}")
        return proc

    def _changed_friday_paths(self) -> list[tuple[str, str]]:
        """[(status, path)] for FRIDAY/ changes, including untracked files."""
        out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all",
                        "--", f"{JARVIS_ROOT}/").stdout
        entries, items = out.split("\0"), []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            if status[0] in "RC" and i < len(entries):   # rename/copy source follows
                items.append(("D ", entries[i]))
                i += 1
            if any(part.startswith(".") for part in path.split("/")):
                continue    # in-flight atomic write (.tmp-*) — never a note
            if status == "??" and not (self.root / path).exists():
                continue    # untracked file already gone (renamed into place)
            items.append((status, path))
        return items

    def _check_file(self, rel: str) -> str | None:
        if not rel.startswith(f"{JARVIS_ROOT}/"):
            return f"{rel}: outside {JARVIS_ROOT}/"
        path = self.root / rel
        if path.is_symlink():
            return f"{rel}: symlink"
        if not path.exists():
            return None                    # deletion inside FRIDAY/
        if path.suffix != ".md":
            return f"{rel}: not a Markdown note"
        if path.stat().st_size > MAX_NOTE_BYTES:
            return f"{rel}: too large"
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"{rel}: not UTF-8 text"
        if find_secrets(text):
            return f"{rel}: looks like it contains a secret"
        return None

    def _stage(self, changes: list[tuple[str, str]]) -> list[str]:
        """
        Stage exactly the checked paths. An untracked note that vanished
        since `git status` (renamed into place by an atomic write) is
        dropped; a tracked deletion is kept. Retries twice on that race.
        """
        for _ in range(3):
            paths = sorted({path for status, path in changes
                            if status != "??" or (self.root / path).exists()})
            if not paths:
                return []
            proc = self._git("add", "-A", "--", *paths, check=False)
            if proc.returncode == 0:
                return paths
            if "did not match" not in proc.stderr:
                raise VaultSyncError(f"git add failed: {_redact(proc.stderr.strip())}")
        raise VaultSyncError("git add failed: notes kept changing during sync")

    # ------------------------------------------------------------ status

    def inspect(self) -> dict[str, Any]:
        """Read-only report used by the CLI and validation. Never commits."""
        report: dict[str, Any] = {"vault": str(self.root), "push_enabled": self.push_enabled,
                                  "branch_expected": self.branch, "sync_version": VAULT_SYNC_VERSION}
        try:
            top = self._git("rev-parse", "--show-toplevel").stdout.strip()
            report["git_repo"] = Path(top).resolve() == self.root
            report["branch"] = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip() or None
            url = self._git("remote", "get-url", self.remote, check=False).stdout.strip()
            report["remote_transport"] = ("ssh" if url.startswith(("git@", "ssh://"))
                                          else "https" if url.startswith("https://")
                                          else "none" if not url else "other")
            report["ssh_command_configured"] = bool(
                self._git("config", "--get", "core.sshCommand", check=False).stdout.strip())
            report["pending_friday_changes"] = len(self._changed_friday_paths())
            ahead = self._git("rev-list", "--count", "@{u}..HEAD", check=False)
            report["unpushed_commits"] = int(ahead.stdout.strip()) if ahead.returncode == 0 else None
        except VaultSyncError as exc:
            report.setdefault("git_repo", False)
            report["error"] = str(exc)
        return report

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "sync_version": VAULT_SYNC_VERSION,
            "vault": str(self.root),
            "push_enabled": self.push_enabled,
            "branch": self.branch,
            "runs": self.runs,
            "commits": self.commits,
            "pushes": self.pushes,
            "last_result": self.last_result,
        }

    # ------------------------------------------------------------ sync

    def sync(self, push: bool | None = None) -> dict[str, Any]:
        """
        Commit pending FRIDAY/ changes and push if enabled.
        Returns a result dict; raises only VaultSyncError for git failures
        before anything was committed.
        """
        with self._lock:
            self.runs += 1
            result = self._sync(self.push_enabled if push is None else bool(push))
            self.last_result = result
            return result

    def _refuse(self, reason: str) -> dict[str, Any]:
        return {"status": "REFUSED", "reason": reason, "files": 0, "commit": None,
                "pushed": False, "execution_authorized": False}

    def _sync(self, push: bool) -> dict[str, Any]:
        top = self._git("rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != self.root:
            return self._refuse("vault is not the root of a git repository")
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir").stdout.strip())
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
            if (git_dir / marker).exists():
                return self._refuse(f"a git operation is in progress ({marker}); resolve it first")
        branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
        if branch != self.branch:
            return self._refuse(f"vault is on branch {branch or '(detached)'}, expected {self.branch}")

        changes = self._changed_friday_paths()
        problems = [p for p in (self._check_file(path) for _, path in changes) if p]
        if problems:
            return self._refuse("; ".join(problems[:5]))

        commit = None
        if changes:
            # Stage exactly the files that were checked — a note written
            # after the check waits for the next sync.
            paths = self._stage(changes)
            if not paths:
                return {"status": "NO_CHANGES", "files": 0, "commit": None, "pushed": False,
                        "reason": None, "execution_authorized": False}
            staged = self._git("diff", "--cached", "--quiet", "--", *paths, check=False)
            if staged.returncode == 0:      # nothing real to commit after all
                return {"status": "NO_CHANGES", "files": 0, "commit": None, "pushed": False,
                        "reason": None, "execution_authorized": False}
            now = self._clock().strftime("%Y-%m-%d %H:%M:%SZ")
            message = (f"FRIDAY: vault sync {now} ({len(changes)} file(s))\n\n"
                       "Automated commit of FRIDAY/ notes only. Advisory; "
                       "execution_authorized: false.")
            # Pathspec => only these FRIDAY/ files are committed; anything
            # else you have staged stays staged and uncommitted.
            self._git("-c", f"user.name={COMMIT_NAME}", "-c", f"user.email={COMMIT_EMAIL}",
                      "-c", "commit.gpgsign=false",
                      "commit", "--quiet", "--no-verify", "-m", message, "--", *paths)
            commit = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            self.commits += 1

        result = {"status": "COMMITTED" if commit else "NO_CHANGES", "files": len(changes),
                  "commit": commit, "pushed": False, "reason": None,
                  "execution_authorized": False}
        if not push:
            return result

        ahead = self._git("rev-list", "--count", f"{self.remote}/{self.branch}..HEAD", check=False)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0" and not commit:
            return result                   # nothing to push
        proc = self._git("push", "--porcelain", self.remote, f"HEAD:refs/heads/{self.branch}",
                         check=False, env=self._env())
        if proc.returncode == 0:
            self.pushes += 1
            result.update(pushed=True, status="PUSHED")
        else:
            detail = proc.stderr.strip() or proc.stdout.strip()
            hint = (" (GitHub has commits FRIDAY does not — pull them into the vault yourself; "
                    "FRIDAY never force-pushes)" if "rejected" in detail or "fetch first" in detail else "")
            result.update(status="PUSH_FAILED", reason=_redact(detail.splitlines()[-1] if detail else "push failed") + hint)
        return result
