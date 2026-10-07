"""
Hermes persistence (SQLite).

Phase 3B:
- hermes_jobs: one row per job, upserted on every state change
- hermes_token_usage: Claude tokens per UTC day (feeds the budget)

The store takes an explicit database path so it can be tested in
isolation; FRIDAY wires in its own DB_PATH in Phase 3D.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .jobs import Job

MAX_RESULT_BYTES = 64_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_jobs(
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT,
    correlation_id TEXT,
    params TEXT,
    result TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hermes_jobs_created ON hermes_jobs(created_at);

CREATE TABLE IF NOT EXISTS hermes_token_usage(
    day TEXT PRIMARY KEY,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    calls INTEGER DEFAULT 0
);
"""


def bounded_json(value: Any, limit: int = MAX_RESULT_BYTES) -> str:
    """
    Serialize for storage; oversized payloads are replaced by a marker
    rather than silently truncated mid-JSON.
    """

    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text.encode("utf-8")) <= limit:
        return text
    return json.dumps({"truncated": True, "original_bytes": len(text.encode("utf-8"))})


class HermesStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------- jobs

    def save_job(self, job: Job) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO hermes_jobs(id, job_type, status, requested_by,
                    correlation_id, params, result, error, attempts,
                    created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    result=excluded.result,
                    error=excluded.error,
                    attempts=excluded.attempts,
                    updated_at=excluded.updated_at
                """,
                (
                    job.id,
                    job.type.value,
                    job.status.value,
                    job.requested_by,
                    job.correlation_id,
                    bounded_json(job.params.model_dump(mode="json")),
                    None if job.result is None else bounded_json(job.result),
                    job.error,
                    job.attempts,
                    job.created_at,
                    job.updated_at,
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hermes_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return None if row is None else self._row(row)

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hermes_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("params", "result"):
            if data.get(key):
                data[key] = json.loads(data[key])
        data["execution_authorized"] = False
        return data

    # -------------------------------------------------------- token usage

    def add_usage(self, day: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO hermes_token_usage(day, input_tokens, output_tokens, calls)
                VALUES(?,?,?,1)
                ON CONFLICT(day) DO UPDATE SET
                    input_tokens=input_tokens+excluded.input_tokens,
                    output_tokens=output_tokens+excluded.output_tokens,
                    calls=calls+1
                """,
                (day, int(input_tokens), int(output_tokens)),
            )

    def usage(self, day: str) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT input_tokens, output_tokens, calls FROM hermes_token_usage WHERE day=?",
                (day,),
            ).fetchone()
        if row is None:
            return {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        return dict(row)
