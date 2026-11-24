import sqlite3
import json
from datetime import datetime, timedelta, timezone

DB = "/home/kingdavid/JARVIS-Platform/JARVIS-Core/data/jarvis.db"
print(f"Using database: {DB}")

conn = sqlite3.connect(DB)
now = datetime.now(timezone.utc)
expires = (now + timedelta(hours=24)).isoformat(timespec="seconds")

row = (
    now.isoformat(timespec="seconds"),
    "RESEARCH_FORECAST",
    "1.0",
    "6919847",
    "0",
    "Yes",
    "DOWN",
    0.85,
    24.0,
    expires,
    0.087, 0.086, 0.088, 0.001,
    json.dumps({"source": "AI research", "doc_id": "0b77bb23e484b5de", "reason": "Board is actively searching for a permanent CEO"})
)

cur = conn.cursor()
cur.execute("""
    INSERT INTO signals(observed_at, strategy, strategy_version, market_id, token_id, outcome, direction, strength, horizon_hours, expires_at, mid, best_bid, best_ask, half_spread, inputs)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", row)
conn.commit()
print(f"Inserted signal ID: {cur.lastrowid}")
conn.close()
