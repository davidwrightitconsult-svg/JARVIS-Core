from dataclasses import dataclass
from enum import Enum


class CommandType(str, Enum):
    STATUS = "status"
    SCAN = "scan"
    HELP = "help"
    STOP = "stop"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Command:
    type: CommandType
    raw: str
    args: tuple[str, ...] = ()


class CommandRouter:
    """
    Deterministic command classification.

    The router does not execute trades and never grants execution authority.
    """

    ALIASES = {
        "status": CommandType.STATUS,
        "/status": CommandType.STATUS,
        "scan": CommandType.SCAN,
        "/scan": CommandType.SCAN,
        "help": CommandType.HELP,
        "/help": CommandType.HELP,
        "?": CommandType.HELP,
        "stop": CommandType.STOP,
        "/stop": CommandType.STOP,
    }

    def route(self, text: str) -> Command:
        raw = (text or "").strip()
        if not raw:
            return Command(CommandType.UNKNOWN, raw)

        parts = tuple(raw.split())
        command = parts[0].lower()

        return Command(
            self.ALIASES.get(command, CommandType.UNKNOWN),
            raw,
            parts[1:],
        )
