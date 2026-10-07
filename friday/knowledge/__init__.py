"""
FRIDAY Knowledge layer (Phase 4) — Obsidian-compatible Markdown vault.

FRIDAY writes only inside FRIDAY/; human-owned notes are read-only and
always treated as data, never instructions.
"""

from .notes import NoteMeta, NoteType, ParsedNote, parse_note, render_note
from .vault import (
    FRIDAY_FOLDERS,
    FRIDAY_ROOT,
    HUMAN_FOLDERS,
    VAULT_CONTRACT_VERSION,
    KnowledgeVault,
    SecretDetectedError,
    VaultError,
    VaultPermissionError,
    Zone,
    find_secrets,
)

from .recall import KNOWLEDGE_RECALL_VERSION, KnowledgeRecall
from .recorder import KNOWLEDGE_RECORDER_VERSION, KnowledgeRecorder
from .sync import VAULT_SYNC_VERSION, VaultSync, VaultSyncError

__all__ = [
    "KNOWLEDGE_RECALL_VERSION", "KNOWLEDGE_RECORDER_VERSION", "KnowledgeRecall", "KnowledgeRecorder",
    "VAULT_SYNC_VERSION", "VaultSync", "VaultSyncError",
    "FRIDAY_FOLDERS", "FRIDAY_ROOT", "HUMAN_FOLDERS", "VAULT_CONTRACT_VERSION",
    "KnowledgeVault", "NoteMeta", "NoteType", "ParsedNote", "SecretDetectedError",
    "VaultError", "VaultPermissionError", "Zone", "find_secrets", "parse_note", "render_note",
]
