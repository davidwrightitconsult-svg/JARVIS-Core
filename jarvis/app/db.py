import json
import sqlite3
from pathlib import Path
from .config import DB_PATH


def conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                kind TEXT,
                message TEXT,
                source TEXT,
                component TEXT,
                severity TEXT,
                correlation_id TEXT,
                payload TEXT
            );

            CREATE TABLE IF NOT EXISTS books(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                name TEXT,
                pages INTEGER,
                chars INTEGER,
                status TEXT,
                path TEXT
            );

            CREATE TABLE IF NOT EXISTS strategies(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                name TEXT UNIQUE,
                family TEXT,
                confidence REAL,
                status TEXT,
                rules TEXT,
                source TEXT
            );

            CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                market TEXT,
                action TEXT,
                score REAL,
                edge REAL,
                reason TEXT,
                payload TEXT
            );

            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                market TEXT,
                side TEXT,
                entry REAL,
                stop REAL,
                target REAL,
                stake REAL,
                status TEXT,
                pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS commands(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                command TEXT,
                response TEXT
            );
            """
        )

        # Lightweight migration for databases created by Phase 0.
        existing = {
            row["name"]
            for row in c.execute("PRAGMA table_info(events)").fetchall()
        }

        migrations = {
            "event_id": "TEXT",
            "source": "TEXT",
            "component": "TEXT",
            "severity": "TEXT",
            "correlation_id": "TEXT",
        }

        for column, datatype in migrations.items():
            if column not in existing:
                c.execute(
                    f"ALTER TABLE events ADD COLUMN {column} {datatype}"
                )


class EventLogger:
    def emit(self, event):
        payload = event.normalized()

        with conn() as c:
            c.execute(
                """
                INSERT INTO events(
                    event_id, ts, kind, message, source,
                    component, severity, correlation_id, payload
                )
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload["event_id"],
                    payload["ts"],
                    payload["kind"],
                    payload["message"],
                    payload["source"],
                    payload["component"],
                    payload["severity"],
                    payload["correlation_id"],
                    payload["payload"],
                ),
            )

        return payload["event_id"]


def log(kind, message, payload=None):
    from jarvis.core.events import Event

    return EventLogger().emit(
        Event(
            kind=kind,
            message=message,
            payload=payload or {},
        )
    )


def add_command(source, command, response):
    with conn() as c:
        c.execute(
            "INSERT INTO commands(source,command,response) VALUES(?,?,?)",
            (source, command, response),
        )


def stats():
    with conn() as c:
        books = c.execute(
            "SELECT COUNT(*) n FROM books"
        ).fetchone()["n"]

        strategies = c.execute(
            "SELECT COUNT(*) n FROM strategies"
        ).fetchone()["n"]

        decisions = c.execute(
            "SELECT COUNT(*) n FROM decisions"
        ).fetchone()["n"]

        trades = c.execute(
            "SELECT COUNT(*) n FROM trades WHERE status='OPEN'"
        ).fetchone()["n"]

        recent = c.execute(
            """
            SELECT ts,kind,message,source,component,severity
            FROM events
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    return {
        "books": books,
        "strategies": strategies,
        "decisions": decisions,
        "open_trades": trades,
        "events": [dict(x) for x in recent],
    }
