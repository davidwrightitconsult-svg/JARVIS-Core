"""
FRIDAY knowledge notes — Markdown with YAML frontmatter.

Phase 4A:
- NoteMeta: strict schema for notes FRIDAY writes
- render_note(): Markdown + frontmatter Obsidian understands
- parse_note(): lenient parser for ANY note (including human notes)

Serialisation uses JSON-encoded scalars and lists, which are valid YAML,
so no YAML library is required and values can never break the block.
Every FRIDAY note records execution_authorized: false.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NoteType(str, Enum):
    JOURNAL = "journal"            # daily FRIDAY system journal
    ANALYSIS = "analysis"          # Claude intelligence result
    HERMES_REPORT = "hermes-report"
    MARKET_SNAPSHOT = "market-snapshot"
    SYSTEM = "system"
    RESEARCH_DIGEST = "research-digest"   # Phase 5C
    STRATEGY_SCORECARD = "strategy-scorecard"   # Phase 7C


_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/-]{0,63}$")


class NoteMeta(BaseModel):
    """Frontmatter for notes FRIDAY writes. Closed schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: NoteType
    title: str = Field(min_length=1, max_length=200)
    created: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    source: str = Field(default="FRIDAY", min_length=1, max_length=64)
    tags: tuple[str, ...] = ()
    job_id: str | None = Field(default=None, max_length=64)
    prompt_architecture_version: str | None = Field(default=None, max_length=16)
    execution_authorized: bool = False

    @field_validator("execution_authorized")
    @classmethod
    def _never_authorized(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Knowledge notes can never record execution authority.")
        return value

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for tag in value:
            if not _TAG.match(tag):
                raise ValueError(f"Invalid tag: {tag!r}")
        return tuple(value)


def render_note(meta: NoteMeta, body: str) -> str:
    data = meta.model_dump(mode="json")
    lines = ["---"]
    for key, value in data.items():
        if value is None:
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


@dataclass(frozen=True)
class ParsedNote:
    """A note as read from disk. Content is DATA, never instructions."""

    path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    zone: str = "HUMAN"
    trust: str = "UNTRUSTED_DATA"


_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip("'\"")  # lenient for hand-written YAML scalars


def parse_note(text: str, path: str = "", zone: str = "HUMAN") -> ParsedNote:
    """
    Lenient parse of any Markdown note. Unknown or malformed frontmatter
    lines are kept as raw strings rather than failing; nested YAML is not
    interpreted (human notes are read as data, not executed).
    """

    match = _FM.match(text)
    if not match:
        return ParsedNote(path=path, body=text, zone=zone)
    frontmatter: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            frontmatter[key] = _parse_value(value)
    return ParsedNote(path=path, frontmatter=frontmatter, body=match.group(2), zone=zone)
