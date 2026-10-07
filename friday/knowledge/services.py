"""
FRIDAY knowledge services — everything Phase 4 lends to the running app.

Phase 4C:
    recorder  (4B)  FRIDAY writes notes into FRIDAY/
    recall    (4C)  vault notes supplied to Claude as untrusted reference data
    sync      (4C)  FRIDAY/ committed to the vault's git repo, push opt-in

Settings (environment / .env):
    FRIDAY_KNOWLEDGE_ENABLED              master switch (default true)
    FRIDAY_KNOWLEDGE_RECALL               vault notes -> Claude (default true)
    FRIDAY_VAULT_SYNC_INTERVAL_SECONDS    auto-commit FRIDAY/ (default 900,
                                          minimum 300, 0 disables)
    FRIDAY_VAULT_PUSH                     push after commit (default false;
                                          enable once the deploy key works)
    FRIDAY_VAULT_BRANCH                   default main

Never raises: a bad setting disables only the part it belongs to, with a
reason in the status, and FRIDAY runs normally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .recall import KnowledgeRecall
from .recorder import KnowledgeRecorder, build_recorder_from_environment
from .sync import DEFAULT_SYNC_INTERVAL, MIN_SYNC_INTERVAL, VaultSync

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


@dataclass
class KnowledgeServices:
    recorder: KnowledgeRecorder | None = None
    recall: KnowledgeRecall | None = None
    sync: VaultSync | None = None
    sync_interval_seconds: int = 0
    reasons: dict[str, str] = field(default_factory=dict)
    base_status: dict[str, Any] = field(default_factory=dict)

    def status(self) -> dict[str, Any]:
        status = dict(self.recorder.status() if self.recorder else self.base_status)
        status["recall"] = (self.recall.status() if self.recall
                            else {"enabled": False, "reason": self.reasons.get("recall")})
        if self.sync:
            status["sync"] = {**self.sync.status(), "interval_seconds": self.sync_interval_seconds}
        else:
            status["sync"] = {"enabled": False, "reason": self.reasons.get("sync")}
        return status


def build_knowledge_from_environment() -> KnowledgeServices:
    recorder, base = build_recorder_from_environment()
    services = KnowledgeServices(recorder=recorder, base_status=base)
    if recorder is None:
        reason = base.get("reason") or "knowledge disabled"
        services.reasons = {"recall": reason, "sync": reason}
        return services
    vault = recorder.vault

    try:
        if _flag("FRIDAY_KNOWLEDGE_RECALL", True):
            services.recall = KnowledgeRecall(vault)
        else:
            services.reasons["recall"] = "disabled by FRIDAY_KNOWLEDGE_RECALL"
    except ValueError as exc:
        services.reasons["recall"] = str(exc)

    try:
        raw = os.getenv("FRIDAY_VAULT_SYNC_INTERVAL_SECONDS", "").strip()
        interval = int(raw) if raw else DEFAULT_SYNC_INTERVAL
        if interval != 0 and interval < MIN_SYNC_INTERVAL:
            raise ValueError(f"FRIDAY_VAULT_SYNC_INTERVAL_SECONDS must be 0 or >= {MIN_SYNC_INTERVAL}")
        push = _flag("FRIDAY_VAULT_PUSH", False)
        branch = os.getenv("FRIDAY_VAULT_BRANCH", "main").strip() or "main"
        if interval == 0:
            services.reasons["sync"] = "disabled by FRIDAY_VAULT_SYNC_INTERVAL_SECONDS=0"
        elif not (vault.root / ".git").exists():
            services.reasons["sync"] = f"{vault.root} is not a git repository"
        else:
            services.sync = VaultSync(vault.root, push=push, branch=branch)
            services.sync_interval_seconds = interval
    except ValueError as exc:
        services.reasons["sync"] = str(exc)
    return services
