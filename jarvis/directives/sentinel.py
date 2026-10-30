"""SENTINEL — the Directive Engine.

Parses natural language directives into hard-coded operational rules.
Persists rules to disk so they survive restarts.

Example directives:
  "Do not lose more than 2 trades in a row"
  "If you lose more than 3 trades consecutively, close down"
  "Be a truly excellent trading platform"
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DIRECTIVES_PATH = Path.home() / "jarvis" / "data" / "directives.json"

DEFAULT_RULES: dict[str, Any] = {
    "max_consecutive_losses": 3,
    "survival_mode": False,
    "soft_pause_after_losses": 2,  # reduce position size
    "soft_pause_factor": 0.25,      # reduce to 25% of normal size
    "soft_pause_hours": 24,
    "copy_trader_enabled": False,
    "copy_trader_target": None,
    "copy_trader_max_roi_lookback_days": 7,
    "platform_directive": None,
    "last_updated": None,
    "updated_by": None,
}


@dataclass
class DirectiveRule:
    name: str
    description: str
    value: Any
    source: str = "user"


class SentinelDirectives:
    """The single source of truth for all operational directives."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rules: dict[str, Any] = dict(DEFAULT_RULES)
        self._load()

    def _load(self) -> None:
        if DIRECTIVES_PATH.exists():
            try:
                with open(DIRECTIVES_PATH) as f:
                    loaded = json.load(f)
                    self._rules.update(loaded)
            except Exception:
                pass

    def _save(self) -> None:
        DIRECTIVES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DIRECTIVES_PATH, "w") as f:
            json.dump(self._rules, f, indent=2)

    # ---- Read access ----

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._rules)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._rules.get(key, default)

    def is_survival_mode(self) -> bool:
        return bool(self.get("survival_mode", False))

    def max_consecutive_losses(self) -> int:
        return int(self.get("max_consecutive_losses", 3))

    # ---- Write access ----

    def set(self, key: str, value: Any, by: str = "user") -> None:
        with self._lock:
            self._rules[key] = value
            import datetime
            self._rules["last_updated"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self._rules["updated_by"] = by
            self._save()

    def enable_survival_mode(self) -> None:
        self.set("survival_mode", True)

    def disable_survival_mode(self) -> None:
        self.set("survival_mode", False)

    # ---- Parsing natural language ----

    def parse_and_apply(self, directive_text: str) -> dict[str, Any]:
        """Parse a natural-language directive and apply what we can detect.

        Returns a dict with `applied` (list of rules changed) and
        `reply` (Alfred's verbal response).
        """
        text = directive_text.lower()
        applied: list[str] = []

        # Detect "close down after N losses"
        import re
        m = re.search(r"(?:lose|lost|losing)\s+(?:more\s+than\s+)?(\d+)\s+(?:trades?|times?)", text)
        if m:
            n = int(m.group(1))
            self.set("max_consecutive_losses", n)
            self.set("survival_mode", True)
            applied.append(f"max_consecutive_losses={n}")
            applied.append("survival_mode=True")

        # Detect "switch to next most profitable trader"
        if "copy" in text or ("next" in text and "trader" in text):
            self.set("copy_trader_enabled", True)
            applied.append("copy_trader_enabled=True")

        # Detect "pause for N hours"
        m = re.search(r"pause.{0,20}(\d+)\s+hours?", text)
        if m:
            hours = int(m.group(1))
            self.set("soft_pause_hours", hours)
            applied.append(f"soft_pause_hours={hours}")

        # Detect "excellent trading platform"
        if "excellent" in text or "best" in text:
            self.set("platform_directive", "excellence")
            applied.append("platform_directive=excellence")

        # Compose reply
        if applied:
            reply = (
                "Very good, sir. I have updated my directives: "
                + ", ".join(applied)
                + ". I shall hold myself to that standard without exception."
            )
        else:
            reply = (
                "I am afraid I could not parse that as a directive, sir. "
                "Try something like 'do not lose more than three trades in a row'."
            )

        return {"applied": applied, "reply": reply}


# ---- Singleton ----

_sentinel: SentinelDirectives | None = None


def get_sentinel() -> SentinelDirectives:
    global _sentinel
    if _sentinel is None:
        _sentinel = SentinelDirectives()
    return _sentinel
