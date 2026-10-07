from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid


@dataclass(frozen=True)
class Event:
    kind: str
    message: str
    source: str = "SYSTEM"
    component: str = "CORE"
    severity: str = "INFO"
    correlation_id: str = ""
    payload: dict | None = None

    def normalized(self):
        return {
            "event_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": self.kind,
            "message": self.message,
            "source": self.source,
            "component": self.component,
            "severity": self.severity,
            "correlation_id": self.correlation_id or str(uuid.uuid4()),
            "payload": json.dumps(self.payload or {}),
        }
