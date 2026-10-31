"""
FRIDAY market data store (Phase 6B) — time-series history on jarvis020.

- SQLite file, default ~/jarvis/data/market_data.db, separate from FRIDAY's
  operational database; refused inside any Git working tree
- Every snapshot run is recorded by the market-data engine itself, so the
  existing Hermes MARKET_SCAN schedule builds history with no new authority
- Tables: runs, markets, outcomes, snapshots (one row per outcome token per
  run), book_levels (top-N levels per side, per snapshot)
- Retention: snapshots/runs 365 days, book levels 30 days (configurable);
  pruning runs at most hourly, automatically
- Read-only market facts only: no orders, no positions, no credentials
- 6C: runs record how many settled/past-end markets were excluded; CSV/JSON
  export; keyword lookup of tracked markets and a compact per-market
  summary (latest book, 24h change/range) used as Claude's market context
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import statistics
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Market, SnapshotRow

MARKET_STORE_VERSION = "8A.1"
DEFAULT_MARKET_DB = "~/jarvis/data/market_data.db"
DEFAULT_RETENTION_DAYS = 365
DEFAULT_BOOK_RETENTION_DAYS = 30

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs(
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    markets INTEGER, tokens INTEGER, books_ok INTEGER, usable_rows INTEGER,
    batch_endpoint INTEGER, errors TEXT, duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_time ON runs(observed_at);
CREATE TABLE IF NOT EXISTS markets(
    market_id TEXT PRIMARY KEY,
    question TEXT NOT NULL, condition_id TEXT, slug TEXT, end_date TEXT, neg_risk INTEGER,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes(
    token_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    best_bid REAL, best_ask REAL, mid REAL, spread REAL, last_trade REAL,
    depth_usd REAL, gamma_liquidity REAL, volume_24h REAL,
    flags TEXT NOT NULL, usable INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_token_time ON snapshots(token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(observed_at);
CREATE TABLE IF NOT EXISTS book_levels(
    snapshot_id INTEGER NOT NULL,
    side TEXT NOT NULL,          -- 'bid' | 'ask'
    level INTEGER NOT NULL,      -- 0 = best
    price REAL NOT NULL, size REAL NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_levels_snap ON book_levels(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_levels_time ON book_levels(observed_at);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS watchlist(
    market_id TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    last_eligible_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals(
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    observed_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    direction TEXT NOT NULL,          -- 'UP' | 'DOWN': expected move of this token's price
    strength REAL NOT NULL,           -- 0..1, strategy-specific
    horizon_hours REAL NOT NULL,
    expires_at TEXT NOT NULL,
    mid REAL, best_bid REAL, best_ask REAL, half_spread REAL,
    inputs TEXT NOT NULL              -- JSON: every feature the rule used
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(observed_at);
CREATE INDEX IF NOT EXISTS idx_signals_key ON signals(strategy, token_id, expires_at);
CREATE TABLE IF NOT EXISTS signal_outcomes(
    signal_id INTEGER PRIMARY KEY,
    scored_at TEXT NOT NULL,
    status TEXT NOT NULL,             -- SCORED | NO_EXIT_DATA | RESOLVED_BEFORE_EXIT
    exit_observed_at TEXT, exit_mid REAL, exit_bid REAL, exit_ask REAL,
    mid_move REAL,                    -- signed: + means the price moved the predicted way
    net REAL,                         -- executable: crossed the spread both ways
    cost REAL,                        -- capital per $1 share committed at entry
    return_on_cost REAL,
    resolution_value REAL,            -- 1/0 payoff of the token once the market resolved
    resolution_net REAL,              -- executable result had it been held to resolution
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS calib_markets(
    market_id TEXT PRIMARY KEY,
    question TEXT, slug TEXT, end_date TEXT, closed_time TEXT,
    reference_time TEXT NOT NULL,     -- when the outcome became known (earliest of end/close)
    volume REAL, neg_risk INTEGER,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bar_markets(      -- 11B: one hourly price series per resolved market
    bar_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT UNIQUE NOT NULL, token_id TEXT NOT NULL, outcome TEXT, payoff REAL,
    question TEXT, reference_time TEXT NOT NULL, volume REAL, neg_risk INTEGER,
    bars INTEGER NOT NULL, observed INTEGER NOT NULL,   -- hours in the series / hours the venue reported a price for
    first_t INTEGER, last_t INTEGER, status TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bars(
    bar_id INTEGER NOT NULL, t INTEGER NOT NULL,        -- t = unix hour (seconds // 3600)
    price REAL NOT NULL, PRIMARY KEY(bar_id, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS calib_points(
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    payoff REAL NOT NULL,             -- 1.0 / 0.0 at resolution
    horizon_hours REAL NOT NULL,      -- price taken this long before reference_time
    price REAL NOT NULL,
    price_time TEXT NOT NULL,
    PRIMARY KEY(market_id, token_id, horizon_hours)
);
CREATE INDEX IF NOT EXISTS idx_calib_h ON calib_points(horizon_hours);
CREATE TABLE IF NOT EXISTS calib_points_end(
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    payoff REAL NOT NULL,
    horizon_hours REAL NOT NULL,      -- 9B: price taken this long before the SCHEDULED end date,
    price REAL NOT NULL,              --     only if the market was still open at that moment
    price_time TEXT NOT NULL,
    PRIMARY KEY(market_id, token_id, horizon_hours)
);
CREATE INDEX IF NOT EXISTS idx_calib_end_h ON calib_points_end(horizon_hours);
CREATE TABLE IF NOT EXISTS calib_skipped(
    market_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resolutions(
    market_id TEXT PRIMARY KEY,
    detected_at TEXT NOT NULL,
    payoffs TEXT NOT NULL,            -- JSON {token_id: 1.0 | 0.0}
    source TEXT NOT NULL
);
"""


class MarketStoreError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _inside_git(path: Path) -> Path | None:
    for parent in [path.parent, *path.parent.parents]:
        if (parent / ".git").exists():
            return parent
    return None


class MarketStore:
    def __init__(self, path: str | Path = DEFAULT_MARKET_DB,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 book_retention_days: int = DEFAULT_BOOK_RETENTION_DAYS,
                 allow_inside_git: bool = False):
        self.path = Path(path).expanduser().resolve()
        repo = _inside_git(self.path)
        if repo is not None and not allow_inside_git:
            raise MarketStoreError(f"market database {self.path} is inside the Git repository {repo}; "
                                   "choose a path outside any repo")
        if retention_days < 1 or book_retention_days < 1:
            raise MarketStoreError("retention must be at least 1 day")
        self.retention_days = int(retention_days)
        self.book_retention_days = int(book_retention_days)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Additive, idempotent column migrations for stores made by 6B."""
        cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
        if "excluded" not in cols:
            db.execute("ALTER TABLE runs ADD COLUMN excluded INTEGER")
        if "excluded_reasons" not in cols:
            db.execute("ALTER TABLE runs ADD COLUMN excluded_reasons TEXT")
        if "tracked" not in cols:
            db.execute("ALTER TABLE runs ADD COLUMN tracked INTEGER")
        cal = {r["name"] for r in db.execute("PRAGMA table_info(calib_markets)").fetchall()}
        if cal and "end_anchor" not in cal:              # 9B: NULL = not yet re-anchored on the end date
            db.execute("ALTER TABLE calib_markets ADD COLUMN end_anchor TEXT")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ write

    def record(self, markets: list[Market], rows: list[SnapshotRow], report: dict[str, Any],
               source: str = "scan", duration_ms: int | None = None) -> int:
        observed = report.get("observed_at") or _now().isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT INTO runs(observed_at, source, markets, tokens, books_ok, usable_rows, batch_endpoint,"
                " errors, duration_ms, excluded, excluded_reasons, tracked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (observed, source[:32], report.get("markets"), report.get("tokens"), report.get("books_ok"),
                 report.get("usable_rows"), int(bool(report.get("batch_endpoint"))),
                 json.dumps(report.get("errors") or [])[:4000], duration_ms,
                 report.get("excluded"), json.dumps(report.get("excluded_reasons") or {})[:1000],
                 report.get("tracked")))
            run_id = cur.lastrowid
            for m in markets:
                db.execute(
                    "INSERT INTO markets(market_id, question, condition_id, slug, end_date, neg_risk, first_seen,"
                    " last_seen) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(market_id) DO UPDATE SET"
                    " question=excluded.question, end_date=excluded.end_date, last_seen=excluded.last_seen",
                    (m.market_id, m.question, m.condition_id, m.slug, m.end_date,
                     None if m.neg_risk is None else int(m.neg_risk), observed, observed))
                for o in m.outcomes:
                    db.execute("INSERT INTO outcomes(token_id, market_id, name) VALUES(?,?,?)"
                               " ON CONFLICT(token_id) DO UPDATE SET name=excluded.name",
                               (o.token_id, m.market_id, o.name))
            for r in rows:
                cur = db.execute(
                    "INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid,"
                    " spread, last_trade, depth_usd, gamma_liquidity, volume_24h, flags, usable)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, observed, r.token_id, r.market_id, r.best_bid, r.best_ask, r.mid, r.spread,
                     r.last_trade_price, r.depth_usd, r.gamma_liquidity, r.volume_24h,
                     json.dumps(r.flags), int(r.usable)))
                sid = cur.lastrowid
                db.executemany(
                    "INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                    " VALUES(?,?,?,?,?,?)",
                    [(sid, "bid", i, p, s, observed) for i, (p, s) in enumerate(r.top_bids)]
                    + [(sid, "ask", i, p, s, observed) for i, (p, s) in enumerate(r.top_asks)])
        self.maybe_prune()
        return run_id

    def prune(self, now: datetime | None = None) -> dict[str, int]:
        now = now or _now()
        cut = (now - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        book_cut = (now - timedelta(days=self.book_retention_days)).isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            levels = db.execute("DELETE FROM book_levels WHERE observed_at < ?", (book_cut,)).rowcount
            snaps = db.execute("DELETE FROM snapshots WHERE observed_at < ?", (cut,)).rowcount
            runs = db.execute("DELETE FROM runs WHERE observed_at < ?", (cut,)).rowcount
            db.execute("DELETE FROM signal_outcomes WHERE signal_id IN"
                       " (SELECT signal_id FROM signals WHERE observed_at < ?)", (cut,))
            db.execute("DELETE FROM signals WHERE observed_at < ?", (cut,))
            db.execute("DELETE FROM watchlist WHERE last_eligible_at < ?",
                       ((now - timedelta(days=14)).isoformat(timespec="seconds"),))
            db.execute("INSERT INTO meta(key, value) VALUES('last_prune', ?)"
                       " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now.isoformat(timespec="seconds"),))
        return {"book_levels": levels, "snapshots": snaps, "runs": runs}

    def maybe_prune(self) -> None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
        if row is None or _now() - datetime.fromisoformat(row["value"]) > timedelta(hours=1):
            self.prune()

    # ------------------------------------------------------------ read

    def history(self, token_id: str, hours: float = 24, limit: int = 2000) -> list[dict[str, Any]]:
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT observed_at, best_bid, best_ask, mid, spread, last_trade, depth_usd, gamma_liquidity,"
                " volume_24h, flags, usable FROM snapshots WHERE token_id=? AND observed_at>=?"
                " ORDER BY observed_at LIMIT ?", (token_id, since, max(1, min(int(limit), 20000)))).fetchall()
        return [{**dict(r), "flags": json.loads(r["flags"]), "usable": bool(r["usable"])} for r in rows]

    def levels(self, token_id: str, observed_at: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            snap = db.execute(
                "SELECT snapshot_id, observed_at FROM snapshots WHERE token_id=?"
                + (" AND observed_at=?" if observed_at else "")
                # snapshot_id breaks the tie: two runs can share a timestamp, and the book of the
                # LATEST of them is the one that belongs with the quote being filled.
                + " ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                (token_id, observed_at) if observed_at else (token_id,)).fetchone()
            if snap is None:
                return {"token_id": token_id, "observed_at": None, "bids": [], "asks": []}
            levels = db.execute("SELECT side, level, price, size FROM book_levels WHERE snapshot_id=?"
                                " ORDER BY side, level", (snap["snapshot_id"],)).fetchall()
        return {"token_id": token_id, "observed_at": snap["observed_at"],
                "bids": [[r["price"], r["size"]] for r in levels if r["side"] == "bid"],
                "asks": [[r["price"], r["size"]] for r in levels if r["side"] == "ask"]}

    def markets(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT m.market_id, m.question, m.end_date, m.first_seen, m.last_seen,"
                " (SELECT COUNT(*) FROM snapshots s WHERE s.market_id=m.market_id) AS snapshots"
                " FROM markets m ORDER BY m.last_seen DESC LIMIT ?", (max(1, min(int(limit), 2000)),)).fetchall()
            outcomes = db.execute("SELECT token_id, market_id, name FROM outcomes").fetchall()
        by_market: dict[str, list] = {}
        for o in outcomes:
            by_market.setdefault(o["market_id"], []).append({"token_id": o["token_id"], "name": o["name"]})
        return [{**dict(r), "outcomes": by_market.get(r["market_id"], [])} for r in rows]

    def stats(self, days: float = 7) -> dict[str, Any]:
        since = (_now() - timedelta(days=float(days))).isoformat(timespec="seconds")
        with self._db() as db:
            runs = db.execute("SELECT * FROM runs WHERE observed_at>=? ORDER BY observed_at", (since,)).fetchall()
            snaps = db.execute("SELECT flags, usable FROM snapshots WHERE observed_at>=?", (since,)).fetchall()
            latest = db.execute("SELECT MAX(run_id) r FROM runs").fetchone()["r"]
            depth_rows = db.execute(
                "SELECT market_id, SUM(depth_usd) depth, MAX(gamma_liquidity) liq, MIN(usable) all_usable"
                " FROM snapshots WHERE run_id=? GROUP BY market_id", (latest,)).fetchall() if latest else []
            counts = {t: db.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                      for t in ("runs", "markets", "outcomes", "snapshots", "book_levels")}
            first = db.execute("SELECT MIN(observed_at) a, MAX(observed_at) b FROM runs").fetchone()
        flags: dict[str, int] = {}
        for s in snaps:
            for f in json.loads(s["flags"]):
                flags[f] = flags.get(f, 0) + 1
        ratios = [r["depth"] / r["liq"] for r in depth_rows
                  if r["liq"] and r["depth"] is not None and r["all_usable"]]
        excluded: dict[str, int] = {}
        for r in runs:
            for reason, n in json.loads(r["excluded_reasons"] or "{}").items():
                excluded[reason] = excluded.get(reason, 0) + int(n)
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "store_version": MARKET_STORE_VERSION, "path": str(self.path),
            "size_mb": round(size / 1_048_576, 2), "counts": counts,
            "first_run": first["a"], "last_run": first["b"], "window_days": days,
            "runs": len(runs), "runs_with_errors": sum(1 for r in runs if json.loads(r["errors"] or "[]")),
            "snapshots": len(snaps),
            "usable_ratio": round(sum(s["usable"] for s in snaps) / len(snaps), 4) if snaps else None,
            "flags": dict(sorted(flags.items(), key=lambda kv: -kv[1])),
            "excluded_settled": {"total": sum(excluded.values()), "by_reason": excluded},
            "depth_vs_gamma_liquidity": {
                "markets": len(ratios),
                "median_ratio": round(statistics.median(ratios), 3) if ratios else None,
                "note": "our USD depth within ±2c of mid (all outcomes) ÷ Polymarket's liquidityNum, "
                        "latest run, markets whose every outcome is usable",
            },
            "retention_days": self.retention_days, "book_retention_days": self.book_retention_days,
        }

    # ------------------------------------------------------------ 6C: export

    EXPORT_FIELDS = ("observed_at", "market_id", "question", "outcome", "token_id", "best_bid", "best_ask",
                     "mid", "spread", "last_trade", "depth_usd", "gamma_liquidity", "volume_24h",
                     "flags", "usable")

    def export_rows(self, token_id: str | None = None, market_id: str | None = None,
                    hours: float = 24 * 7, limit: int = 100_000) -> list[dict[str, Any]]:
        """Snapshot history joined with question/outcome names, oldest first.
        One of token_id / market_id, or neither for every tracked token."""
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        where, args = ["s.observed_at>=?"], [since]
        if token_id:
            where.append("s.token_id=?")
            args.append(token_id)
        if market_id:
            where.append("s.market_id=?")
            args.append(market_id)
        with self._db() as db:
            rows = db.execute(
                "SELECT s.observed_at, s.market_id, m.question, o.name AS outcome, s.token_id, s.best_bid,"
                " s.best_ask, s.mid, s.spread, s.last_trade, s.depth_usd, s.gamma_liquidity, s.volume_24h,"
                " s.flags, s.usable FROM snapshots s LEFT JOIN markets m ON m.market_id=s.market_id"
                " LEFT JOIN outcomes o ON o.token_id=s.token_id WHERE " + " AND ".join(where)
                + " ORDER BY s.observed_at, s.market_id, s.token_id LIMIT ?",
                (*args, max(1, min(int(limit), 1_000_000)))).fetchall()
        return [{**dict(r), "flags": json.loads(r["flags"]), "usable": bool(r["usable"])} for r in rows]

    @classmethod
    def to_csv(cls, rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cls.EXPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "flags": "|".join(r.get("flags") or []), "usable": int(bool(r.get("usable")))})
        return buf.getvalue()

    # ------------------------------------------------------------ 6C: context lookup

    _STOP = frozenset("""the a an and or of to in on for is are be will was were by with at from as it this that
        what which who whom whose how why when where do does did can could should would may might than then
        vs versus about into over under after before between market markets polymarket price prices odds
        chance probability analyse analyze analysis tell me please friday give show your you our any
        yes no not""".split())

    @classmethod
    def terms(cls, text: str) -> set[str]:
        words = re.findall(r"[a-z0-9][a-z0-9'.-]*", str(text).lower())
        return {w.strip(".'-") for w in words if len(w.strip(".'-")) >= 3 and w.strip(".'-") not in cls._STOP}

    def find_markets(self, query: str, hours: float = 48, limit: int = 3) -> list[str]:
        """Tracked markets (seen in the last `hours`) whose question best
        matches the query's significant words. Needs 2 shared words, or 1
        distinctive (6+ letters) word when the query has at most 2."""
        q = self.terms(query)
        if not q:
            return []
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute("SELECT market_id, question, last_seen FROM markets WHERE last_seen>=?",
                              (since,)).fetchall()
        scored = []
        for r in rows:
            shared = q & self.terms(r["question"])
            score = len(shared)
            enough = score >= 2 or (len(q) <= 2 and any(len(w) >= 6 for w in shared))
            if enough:
                scored.append((score, r["last_seen"], r["market_id"]))
        scored.sort(reverse=True)
        return [market_id for _, _, market_id in scored[:max(1, int(limit))]]

    def market_summary(self, market_id: str, hours: float = 24) -> dict[str, Any] | None:
        """Compact, factual summary of one tracked market for Claude."""
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        with self._db() as db:
            m = db.execute("SELECT market_id, question, end_date, first_seen, last_seen FROM markets"
                           " WHERE market_id=?", (market_id,)).fetchone()
            if m is None:
                return None
            outcomes = db.execute("SELECT token_id, name FROM outcomes WHERE market_id=? ORDER BY rowid",
                                  (market_id,)).fetchall()
            out = []
            for o in outcomes[:4]:
                hist = db.execute(
                    "SELECT observed_at, best_bid, best_ask, mid, spread, depth_usd, volume_24h, flags, usable"
                    " FROM snapshots WHERE token_id=? AND observed_at>=? ORDER BY observed_at",
                    (o["token_id"], since)).fetchall()
                if not hist:
                    continue
                last = hist[-1]
                mids = [h["mid"] for h in hist if h["mid"] is not None and h["usable"]]
                out.append({
                    "outcome": o["name"],
                    "latest": {"observed_at": last["observed_at"], "best_bid": last["best_bid"],
                               "best_ask": last["best_ask"], "mid": last["mid"], "spread": last["spread"],
                               "depth_usd_within_2c": last["depth_usd"], "volume_24h": last["volume_24h"],
                               "flags": json.loads(last["flags"]), "usable": bool(last["usable"])},
                    "observations": len(hist),
                    "usable_ratio": round(sum(h["usable"] for h in hist) / len(hist), 2),
                    "mid_change": round(mids[-1] - mids[0], 4) if len(mids) >= 2 else None,
                    "mid_range": [min(mids), max(mids)] if mids else None,
                })
        if not out:
            return None
        return {"market_id": m["market_id"], "question": m["question"], "end_date": m["end_date"],
                "tracked_since": m["first_seen"], "window_hours": hours, "outcomes": out}

    # ------------------------------------------------------------ 7A: bulk reads for the Quant Core

    _ROW_COLS = ("s.snapshot_id, s.run_id, s.observed_at, s.token_id, s.market_id, s.best_bid, s.best_ask, s.mid,"
                 " s.spread, s.last_trade, s.depth_usd, s.gamma_liquidity, s.volume_24h, s.flags, s.usable,"
                 " m.question, m.end_date, m.neg_risk, o.name AS outcome, o.rowid AS outcome_order")

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return {**dict(r), "flags": json.loads(r["flags"]), "usable": bool(r["usable"])}

    def latest_run(self) -> dict[str, Any] | None:
        with self._db() as db:
            r = db.execute("SELECT run_id, observed_at, markets, excluded FROM runs"
                           " ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def latest_rows(self, market_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Latest snapshot per token: of the latest run (market_ids None), or
        of each token of the given markets, whenever it was last observed."""
        base = (f"SELECT {self._ROW_COLS} FROM snapshots s LEFT JOIN markets m ON m.market_id=s.market_id"
                " LEFT JOIN outcomes o ON o.token_id=s.token_id ")
        with self._db() as db:
            if market_ids is None:
                run = db.execute("SELECT MAX(run_id) r FROM runs").fetchone()["r"]
                if run is None:
                    return []
                rows = db.execute(base + "WHERE s.run_id=? ORDER BY s.market_id, o.rowid", (run,)).fetchall()
            else:
                ids = [str(i) for i in market_ids][:500]
                if not ids:
                    return []
                marks = ",".join("?" * len(ids))
                rows = db.execute(
                    base + f"JOIN (SELECT token_id, MAX(snapshot_id) sid FROM snapshots WHERE market_id IN ({marks})"
                    " GROUP BY token_id) t ON t.sid=s.snapshot_id ORDER BY s.market_id, o.rowid", ids).fetchall()
        return [self._row(r) for r in rows]

    def history_bulk(self, token_ids: list[str], hours: float = 25) -> dict[str, list[dict[str, Any]]]:
        """{token_id: snapshots oldest first} for many tokens in one query."""
        ids = [str(t) for t in token_ids][:1000]
        if not ids:
            return {}
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in ids}
        with self._db() as db:
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                rows = db.execute(
                    "SELECT token_id, observed_at, best_bid, best_ask, mid, spread, depth_usd, usable FROM snapshots"
                    f" WHERE token_id IN ({','.join('?' * len(chunk))}) AND observed_at>=?"
                    " ORDER BY token_id, observed_at", (*chunk, since)).fetchall()
                for r in rows:
                    out[r["token_id"]].append({**dict(r), "usable": bool(r["usable"])})
        return out

    def levels_bulk(self, snapshot_ids: list[int]) -> dict[int, dict[str, list[list[float]]]]:
        """{snapshot_id: {"bids": [[p, s], ...], "asks": [...]}} best first."""
        ids = [int(i) for i in snapshot_ids][:2000]
        out: dict[int, dict[str, list[list[float]]]] = {i: {"bids": [], "asks": []} for i in ids}
        if not ids:
            return out
        with self._db() as db:
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                rows = db.execute(
                    "SELECT snapshot_id, side, level, price, size FROM book_levels"
                    f" WHERE snapshot_id IN ({','.join('?' * len(chunk))}) ORDER BY snapshot_id, side, level",
                    chunk).fetchall()
                for r in rows:
                    out[r["snapshot_id"]]["bids" if r["side"] == "bid" else "asks"].append([r["price"], r["size"]])
        return out

    # ------------------------------------------------------------ 7B: watchlist

    def watch(self, market_ids: list[str], now: datetime | None = None) -> int:
        """Mark markets as (still) eligible: they stay observed for a while."""
        stamp = (now or _now()).isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            db.executemany(
                "INSERT INTO watchlist(market_id, added_at, last_eligible_at) VALUES(?,?,?)"
                " ON CONFLICT(market_id) DO UPDATE SET last_eligible_at=excluded.last_eligible_at",
                [(str(m), stamp, stamp) for m in market_ids])
        return len(market_ids)

    def unwatch(self, market_ids: list[str]) -> int:
        ids = [str(m) for m in market_ids]
        if not ids:
            return 0
        with self._lock, self._db() as db:
            return db.execute(f"DELETE FROM watchlist WHERE market_id IN ({','.join('?' * len(ids))})",
                              ids).rowcount

    def market_end_dates(self, market_ids: list[str]) -> dict[str, str]:
        """13B: market_id -> end_date (ISO) for the markets that have one recorded."""
        ids = [str(m) for m in market_ids][:2000]
        if not ids:
            return {}
        with self._db() as db:
            rows = db.execute(
                f"SELECT market_id, end_date FROM markets WHERE end_date IS NOT NULL"
                f" AND market_id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {r["market_id"]: r["end_date"] for r in rows}

    def tokens_for_markets(self, market_ids: list[str]) -> dict[str, str]:
        """13A: token_id -> market_id for the outcomes of the given markets (for the real-time feed)."""
        ids = [str(m) for m in market_ids][:2000]
        if not ids:
            return {}
        with self._db() as db:
            rows = db.execute(f"SELECT token_id, market_id FROM outcomes WHERE market_id IN ({','.join('?' * len(ids))})",
                              ids).fetchall()
        return {r["token_id"]: r["market_id"] for r in rows}

    def market_for_token(self, token_id: str) -> str | None:
        with self._db() as db:
            r = db.execute("SELECT market_id FROM outcomes WHERE token_id=?", (str(token_id),)).fetchone()
        return r["market_id"] if r else None

    def watchlist(self, hours: float = 48) -> list[dict[str, Any]]:
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT w.market_id, w.added_at, w.last_eligible_at, m.question, m.end_date FROM watchlist w"
                " LEFT JOIN markets m ON m.market_id=w.market_id WHERE w.last_eligible_at>=?"
                " ORDER BY w.last_eligible_at DESC", (since,)).fetchall()
        return [dict(r) for r in rows]

    def watchlist_markets(self, hours: float = 48) -> list[Market]:
        """Watched markets rebuilt from recorded data, ready to observe again.
        Volume/liquidity are the last recorded values (Gamma is not asked)."""
        from .models import Outcome
        out = []
        with self._db() as db:
            for w in self.watchlist(hours):
                if not w["question"]:
                    continue
                outcomes = db.execute("SELECT token_id, name FROM outcomes WHERE market_id=? ORDER BY rowid",
                                      (w["market_id"],)).fetchall()
                last = db.execute("SELECT volume_24h, gamma_liquidity FROM snapshots WHERE market_id=?"
                                  " ORDER BY snapshot_id DESC LIMIT 1", (w["market_id"],)).fetchone()
                m = db.execute("SELECT condition_id, slug, neg_risk FROM markets WHERE market_id=?",
                               (w["market_id"],)).fetchone()
                if len(outcomes) < 2:
                    continue
                out.append(Market(
                    market_id=w["market_id"], question=w["question"], condition_id=m["condition_id"],
                    slug=m["slug"], end_date=w["end_date"],
                    neg_risk=None if m["neg_risk"] is None else bool(m["neg_risk"]),
                    volume_24h=last["volume_24h"] if last else None,
                    liquidity=last["gamma_liquidity"] if last else None,
                    outcomes=[Outcome(o["name"], o["token_id"]) for o in outcomes]))
        return out

    # ------------------------------------------------------------ 7B: signals

    SIGNAL_FIELDS = ("run_id", "observed_at", "strategy", "strategy_version", "market_id", "token_id", "outcome",
                     "direction", "strength", "horizon_hours", "expires_at", "mid", "best_bid", "best_ask",
                     "half_spread", "inputs")

    def record_signals(self, signals: list[dict[str, Any]]) -> list[int]:
        ids = []
        with self._lock, self._db() as db:
            for sig in signals:
                row = {**sig, "inputs": json.dumps(sig.get("inputs") or {}, sort_keys=True)[:4000]}
                cur = db.execute(
                    f"INSERT INTO signals({', '.join(self.SIGNAL_FIELDS)}) VALUES({','.join('?' * len(self.SIGNAL_FIELDS))})",
                    tuple(row.get(k) for k in self.SIGNAL_FIELDS))
                ids.append(cur.lastrowid)
        return ids

    def open_signal_keys(self, now: datetime | None = None) -> set[tuple[str, str, str]]:
        """(strategy, token_id, direction) of signals whose horizon is still running."""
        stamp = (now or _now()).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute("SELECT strategy, token_id, direction FROM signals WHERE expires_at>?",
                              (stamp,)).fetchall()
        return {(r["strategy"], r["token_id"], r["direction"]) for r in rows}

    def signals(self, hours: float = 24, strategy: str | None = None, market_id: str | None = None,
                open_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        where, args = ["s.observed_at>=?"], [since]
        if strategy:
            where.append("s.strategy=?")
            args.append(strategy)
        if market_id:
            where.append("s.market_id=?")
            args.append(market_id)
        if open_only:
            where.append("s.expires_at>?")
            args.append(_now().isoformat(timespec="seconds"))
        with self._db() as db:
            rows = db.execute(
                "SELECT s.*, m.question FROM signals s LEFT JOIN markets m ON m.market_id=s.market_id WHERE "
                + " AND ".join(where) + " ORDER BY s.signal_id DESC LIMIT ?",
                (*args, max(1, min(int(limit), 5000)))).fetchall()
        return [{**dict(r), "inputs": json.loads(r["inputs"])} for r in rows]

    def signal_counts(self, hours: float = 24) -> dict[str, dict[str, int]]:
        since = (_now() - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        now = _now().isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT strategy, COUNT(*) n, SUM(expires_at>?) open, SUM(direction='UP') up FROM signals"
                " WHERE observed_at>=? GROUP BY strategy", (now, since)).fetchall()
        return {r["strategy"]: {"signals": r["n"], "open": r["open"] or 0, "up": r["up"] or 0,
                                "down": r["n"] - (r["up"] or 0)} for r in rows}

    # ------------------------------------------------------------ 7C: outcomes + resolutions

    def due_signals(self, now: datetime | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Signals whose horizon has passed and that have no outcome yet."""
        stamp = (now or _now()).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT s.* FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id=s.signal_id"
                " WHERE o.signal_id IS NULL AND s.expires_at<=? ORDER BY s.expires_at LIMIT ?",
                (stamp, max(1, min(int(limit), 5000)))).fetchall()
        return [{**dict(r), "inputs": json.loads(r["inputs"])} for r in rows]

    def exit_snapshot(self, token_id: str, start: str, end: str) -> dict[str, Any] | None:
        """First USABLE snapshot of a token observed in [start, end]."""
        with self._db() as db:
            r = db.execute(
                "SELECT observed_at, best_bid, best_ask, mid FROM snapshots WHERE token_id=? AND usable=1"
                " AND observed_at>=? AND observed_at<=? ORDER BY observed_at LIMIT 1",
                (token_id, start, end)).fetchone()
        return dict(r) if r else None

    OUTCOME_FIELDS = ("signal_id", "scored_at", "status", "exit_observed_at", "exit_mid", "exit_bid", "exit_ask",
                      "mid_move", "net", "cost", "return_on_cost", "resolution_value", "resolution_net",
                      "resolved_at")

    def record_outcomes(self, outcomes: list[dict[str, Any]]) -> int:
        with self._lock, self._db() as db:
            db.executemany(
                f"INSERT OR REPLACE INTO signal_outcomes({', '.join(self.OUTCOME_FIELDS)})"
                f" VALUES({','.join('?' * len(self.OUTCOME_FIELDS))})",
                [tuple(o.get(k) for k in self.OUTCOME_FIELDS) for o in outcomes])
        return len(outcomes)

    def outcomes(self, days: float = 30, strategy: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        """Signals joined with their outcomes (scored or not yet), newest first."""
        since = (_now() - timedelta(days=float(days))).isoformat(timespec="seconds")
        where, args = ["s.observed_at>=?"], [since]
        if strategy:
            where.append("s.strategy=?")
            args.append(strategy)
        with self._db() as db:
            rows = db.execute(
                "SELECT s.signal_id, s.observed_at, s.strategy, s.strategy_version, s.market_id, s.token_id,"
                " s.outcome, s.direction, s.strength, s.horizon_hours, s.expires_at, s.mid, s.best_bid, s.best_ask,"
                " s.half_spread, m.question, o.status, o.scored_at, o.exit_observed_at, o.exit_mid, o.exit_bid,"
                " o.exit_ask, o.mid_move, o.net, o.cost, o.return_on_cost, o.resolution_value, o.resolution_net,"
                " o.resolved_at FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id=s.signal_id"
                " LEFT JOIN markets m ON m.market_id=s.market_id WHERE " + " AND ".join(where)
                + " ORDER BY s.signal_id DESC LIMIT ?", (*args, max(1, min(int(limit), 100000)))).fetchall()
        return [dict(r) for r in rows]

    def record_resolution(self, market_id: str, payoffs: dict[str, float], source: str = "gamma",
                          now: datetime | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO resolutions(market_id, detected_at, payoffs, source) VALUES(?,?,?,?)",
                       (str(market_id), (now or _now()).isoformat(timespec="seconds"),
                        json.dumps(payoffs, sort_keys=True), source[:32]))

    def resolutions(self, market_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        with self._db() as db:
            if market_ids is None:
                rows = db.execute("SELECT * FROM resolutions").fetchall()
            else:
                ids = [str(m) for m in market_ids][:2000]
                rows = db.execute(f"SELECT * FROM resolutions WHERE market_id IN ({','.join('?' * len(ids))})",
                                  ids).fetchall() if ids else []
        return {r["market_id"]: {"detected_at": r["detected_at"], "payoffs": json.loads(r["payoffs"]),
                                 "source": r["source"]} for r in rows}

    def unresolved_signal_markets(self, now: datetime | None = None, quiet_hours: float = 2,
                                  limit: int = 20) -> list[str]:
        """Markets with signals, not known to be resolved, that either passed
        their end date or have not been observed for `quiet_hours` — the ones
        worth asking Polymarket about."""
        now = now or _now()
        quiet = (now - timedelta(hours=float(quiet_hours))).isoformat(timespec="seconds")
        stamp = now.isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT DISTINCT s.market_id, m.end_date, m.last_seen FROM signals s"
                " LEFT JOIN markets m ON m.market_id=s.market_id"
                " LEFT JOIN resolutions r ON r.market_id=s.market_id WHERE r.market_id IS NULL").fetchall()
        due = sorted(r["market_id"] for r in rows
                     if (r["last_seen"] is not None and r["last_seen"] < quiet)
                     or (r["end_date"] and r["end_date"].replace("Z", "+00:00") < stamp))
        limit = max(1, int(limit))
        if len(due) <= limit:
            return due
        # 8C: long-dated CALIBRATION_EDGE markets stay unresolved for weeks; rotate through
        # the due list so the same 20 are not asked about every hour while others starve
        start = int(self.meta_get("resolution_check_cursor") or 0) % len(due)
        picked = (due[start:] + due[:start])[:limit]
        self.meta_set("resolution_check_cursor", str((start + limit) % len(due)))
        return picked

    def signalled_markets(self, strategy: str) -> set[str]:
        """8C: markets that already have a signal from `strategy` (CALIBRATION_EDGE fires once per market)."""
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT DISTINCT market_id FROM signals WHERE strategy=?",
                                              (strategy,)).fetchall()}

    def meta_get(self, key: str) -> str | None:
        with self._db() as db:
            r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    # ------------------------------------------------------------ 8A: calibration dataset

    def calibration_known(self, market_ids: list[str]) -> set[str]:
        """Market ids already in the calibration dataset (kept or skipped)."""
        ids = [str(m) for m in market_ids][:1000]
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        with self._db() as db:
            kept = db.execute(f"SELECT market_id FROM calib_markets WHERE market_id IN ({marks})", ids).fetchall()
            skipped = db.execute(f"SELECT market_id FROM calib_skipped WHERE market_id IN ({marks})", ids).fetchall()
        return {r["market_id"] for r in kept} | {r["market_id"] for r in skipped}

    def record_calibration_market(self, market: dict[str, Any], points: list[dict[str, Any]]) -> None:
        stamp = _now().isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO calib_markets(market_id, question, slug, end_date, closed_time, reference_time,"
                " volume, neg_risk, fetched_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (market["market_id"], market.get("question"), market.get("slug"), market.get("end_date"),
                 market.get("closed_time"), market["reference_time"], market.get("volume"),
                 None if market.get("neg_risk") is None else int(bool(market["neg_risk"])), stamp))
            db.executemany(
                "INSERT OR REPLACE INTO calib_points(market_id, token_id, outcome, payoff, horizon_hours, price,"
                " price_time) VALUES(?,?,?,?,?,?,?)",
                [(market["market_id"], p["token_id"], p.get("outcome"), float(p["payoff"]), float(p["horizon_hours"]),
                  float(p["price"]), p["price_time"]) for p in points])

    def record_calibration_skip(self, market_id: str, reason: str) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO calib_skipped(market_id, reason, at) VALUES(?,?,?)",
                       (str(market_id), reason[:64], _now().isoformat(timespec="seconds")))

    def forget_calibration_skips(self, reason_prefix: str) -> int:
        """8C: let markets skipped for `reason_prefix...` be evaluated again (e.g. 8A read a blank
        volume field as 0 and skipped them as thin)."""
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM calib_skipped WHERE reason LIKE ?", (reason_prefix + "%",)).rowcount

    def pair_sums(self, days: float = 30) -> list[dict[str, Any]]:
        """9C: per run and two-outcome market, the sum of the two best asks and of the two best bids
        (usable quotes only), with the thinner side's depth. An ask sum below 1 means both outcomes
        could be bought for less than the $1 one of them must pay."""
        since = (_now() - timedelta(days=float(days))).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT s.run_id, s.market_id, MIN(s.observed_at) observed_at, SUM(s.best_ask) ask_sum,"
                " SUM(s.best_bid) bid_sum, MIN(s.depth_usd) depth_usd, m.question, m.neg_risk"
                " FROM snapshots s LEFT JOIN markets m ON m.market_id=s.market_id WHERE s.observed_at>=?"
                " GROUP BY s.run_id, s.market_id HAVING COUNT(*)=2 AND MIN(s.usable)=1"
                " AND COUNT(s.best_ask)=2 AND COUNT(s.best_bid)=2 ORDER BY s.run_id", (since,)).fetchall()
        return [dict(r) for r in rows]

    def arbitrage_candidates(self, max_age_minutes: float = 15.0, limit: int = 200,
                             as_of: str | None = None) -> list[dict[str, Any]]:
        """15: two-outcome markets whose two best asks, in FRIDAY's most recent snapshot of each,
        sum to less than $1. Buying both locks the difference whatever the outcome.

        Only the LATEST snapshot per token is considered: an arbitrage that existed an hour ago is
        not one that can be traded now. Returns both legs with the snapshot each ask came from, so
        the real depth behind it can be read."""
        # `as_of` rather than the wall clock: the engine is clock-injected everywhere else, and a
        # scan is always judged against the moment it is being acted on.
        base = _now() if not as_of else datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        since = (base - timedelta(minutes=float(max_age_minutes))).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "WITH latest AS ("
                "  SELECT s.* FROM snapshots s"
                "  JOIN (SELECT token_id, MAX(observed_at) at, MAX(snapshot_id) sid FROM snapshots"
                "        WHERE observed_at>=? GROUP BY token_id) l"
                "    ON l.token_id=s.token_id AND l.sid=s.snapshot_id"
                "  WHERE s.usable=1 AND s.best_ask IS NOT NULL AND s.best_bid IS NOT NULL)"
                " SELECT market_id, SUM(best_ask) ask_sum, COUNT(*) legs,"
                "        MIN(observed_at) observed_at,"
                "        GROUP_CONCAT(token_id) tokens, GROUP_CONCAT(snapshot_id) snapshot_ids,"
                "        GROUP_CONCAT(best_ask) asks"
                " FROM latest GROUP BY market_id"
                " HAVING legs=2 AND SUM(best_ask) < 1.0"
                " ORDER BY SUM(best_ask) ASC LIMIT ?", (since, max(1, min(int(limit), 1000)))).fetchall()
        out = []
        for r in rows:
            tokens = str(r["tokens"]).split(",")
            snaps = [int(x) for x in str(r["snapshot_ids"]).split(",")]
            asks = [float(x) for x in str(r["asks"]).split(",")]
            if len(tokens) != 2 or len(snaps) != 2 or len(asks) != 2:
                continue
            with self._db() as db:
                m = db.execute("SELECT question, end_date, neg_risk FROM markets WHERE market_id=?",
                               (r["market_id"],)).fetchone()
            out.append({"market_id": r["market_id"], "ask_sum": round(float(r["ask_sum"]), 6),
                        "observed_at": r["observed_at"], "question": m["question"] if m else None,
                        "end_date": m["end_date"] if m else None, "neg_risk": m["neg_risk"] if m else None,
                        "legs": [{"token_id": tokens[i], "snapshot_id": snaps[i], "best_ask": asks[i]}
                                 for i in (0, 1)]})
        return out

    def levels_for_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        """The recorded book of one specific snapshot, best first. 15 needs the book that belongs to
        a named ask rather than the newest book for the token."""
        with self._db() as db:
            rows = db.execute("SELECT side, level, price, size FROM book_levels WHERE snapshot_id=?"
                              " ORDER BY side, level", (int(snapshot_id),)).fetchall()
        bids = sorted([[float(r["price"]), float(r["size"])] for r in rows if r["side"] == "bid"],
                      key=lambda lv: -lv[0])
        asks = sorted([[float(r["price"]), float(r["size"])] for r in rows if r["side"] == "ask"],
                      key=lambda lv: lv[0])
        return {"bids": bids, "asks": asks}

    def snapshot_span(self) -> tuple[str, str] | None:
        """(first, last) observation time FRIDAY has recorded, or None."""
        with self._db() as db:
            r = db.execute("SELECT MIN(observed_at) a, MAX(observed_at) b FROM snapshots").fetchone()
        return (r["a"], r["b"]) if r and r["a"] else None

    def quote_near(self, token_id: str, at: str, tolerance_hours: float = 3.0) -> dict[str, Any] | None:
        """9B: FRIDAY's own last USABLE recorded quote of a token at or before `at` (within the tolerance)."""
        start = (datetime.fromisoformat(at) - timedelta(hours=float(tolerance_hours))).isoformat(timespec="seconds")
        with self._db() as db:
            r = db.execute(
                "SELECT observed_at, best_bid, best_ask, mid FROM snapshots WHERE token_id=? AND usable=1"
                " AND observed_at>=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                (token_id, start, at)).fetchone()
        return dict(r) if r else None

    def record_end_anchor(self, market_id: str, points: list[dict[str, Any]], status: str) -> None:
        """9B: prices at fixed times before the scheduled end date (replaces any earlier set)."""
        with self._lock, self._db() as db:
            db.execute("DELETE FROM calib_points_end WHERE market_id=?", (str(market_id),))
            db.executemany(
                "INSERT OR REPLACE INTO calib_points_end(market_id, token_id, outcome, payoff, horizon_hours, price,"
                " price_time) VALUES(?,?,?,?,?,?,?)",
                [(str(market_id), p["token_id"], p.get("outcome"), float(p["payoff"]), float(p["horizon_hours"]),
                  float(p["price"]), p["price_time"]) for p in points])
            db.execute("UPDATE calib_markets SET end_anchor=? WHERE market_id=?", (status[:40], str(market_id)))

    def reset_end_anchor(self, statuses: tuple[str, ...]) -> int:
        """Mark markets with these end-anchor statuses as pending again (their points are rebuilt)."""
        marks = ",".join("?" * len(statuses))
        with self._lock, self._db() as db:
            return db.execute(f"UPDATE calib_markets SET end_anchor=NULL WHERE end_anchor IN ({marks})",
                              tuple(statuses)).rowcount

    def end_anchor_pending(self, limit: int = 200) -> list[dict[str, Any]]:
        """Calibration markets not yet re-anchored, newest resolution first, with their tokens."""
        with self._db() as db:
            rows = db.execute("SELECT market_id, question, neg_risk, end_date, closed_time, reference_time"
                              " FROM calib_markets WHERE end_anchor IS NULL ORDER BY reference_time DESC LIMIT ?",
                              (max(1, int(limit)),)).fetchall()
            out = []
            for r in rows:
                tokens = db.execute("SELECT token_id, outcome, payoff, horizon_hours, price, price_time"
                                    " FROM calib_points WHERE market_id=?", (r["market_id"],)).fetchall()
                out.append({**dict(r), "points": [dict(t) for t in tokens]})
        return out

    def end_anchor_stats(self) -> dict[str, Any]:
        with self._db() as db:
            status = db.execute("SELECT COALESCE(end_anchor, 'pending') s, COUNT(*) n FROM calib_markets"
                                " GROUP BY s").fetchall()
            points = db.execute("SELECT horizon_hours h, COUNT(*) n, COUNT(DISTINCT market_id) m"
                                " FROM calib_points_end GROUP BY horizon_hours ORDER BY horizon_hours").fetchall()
        return {"markets": {r["s"]: r["n"] for r in status},
                "pending": next((r["n"] for r in status if r["s"] == "pending"), 0),
                "points_by_horizon": {f"{r['h']:g}h": r["n"] for r in points},
                "markets_by_horizon": {f"{r['h']:g}h": r["m"] for r in points}}

    def calibration_points(self, horizon_hours: float, min_volume: float = 0,
                           since: str | None = None, anchor: str = "resolution") -> list[dict[str, Any]]:
        """anchor='resolution' (8A: before the actual resolution — knows when it resolved) or
        'end' (9B: before the scheduled end date — what FRIDAY knows live)."""
        table = {"resolution": "calib_points", "end": "calib_points_end"}[anchor]
        with self._db() as db:
            rows = db.execute(
                "SELECT p.market_id, p.token_id, p.outcome, p.payoff, p.price, p.price_time, m.volume,"
                f" m.reference_time, m.question, m.neg_risk, m.end_date FROM {table} p"
                " JOIN calib_markets m ON m.market_id=p.market_id"
                " WHERE p.horizon_hours=? AND COALESCE(m.volume, 0)>=? AND (? IS NULL OR m.reference_time>=?)",
                (float(horizon_hours), float(min_volume), since, since)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ hourly bars (11B)

    def bars_pending(self, limit: int = 200, min_volume: float = 0) -> list[dict[str, Any]]:
        """Resolved two-outcome calibration markets with no hourly series yet, largest volume first.
        The token is the one FRIDAY recorded first alphabetically by outcome ("No" before "Yes" is
        avoided: "Yes" is preferred when the market has one)."""
        with self._db() as db:
            rows = db.execute(
                "SELECT m.market_id, m.question, m.reference_time, m.volume, m.neg_risk FROM calib_markets m"
                " WHERE m.end_anchor='done' AND COALESCE(m.volume, 0) >= ?"
                " AND NOT EXISTS (SELECT 1 FROM bar_markets b WHERE b.market_id=m.market_id)"
                " ORDER BY m.volume DESC LIMIT ?", (float(min_volume), max(1, int(limit)))).fetchall()
            out = []
            for r in rows:
                tokens = db.execute("SELECT DISTINCT token_id, outcome, payoff FROM calib_points_end WHERE market_id=?",
                                    (r["market_id"],)).fetchall()
                if len(tokens) != 2:
                    continue
                pick = next((t for t in tokens if str(t["outcome"]).strip().lower() == "yes"),
                            sorted(tokens, key=lambda t: str(t["outcome"]))[0])
                out.append({**dict(r), "token_id": pick["token_id"], "outcome": pick["outcome"], "payoff": pick["payoff"]})
        return out

    def record_bars(self, market: dict[str, Any], series: list[tuple[int, float]], observed: int, status: str) -> bool:
        """Store one market's series, once. False if it was already stored (a second builder running
        at the same time): a series, once written, is never replaced, so a test cannot shift under it."""
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO bar_markets(market_id, token_id, outcome, payoff, question, reference_time, volume, neg_risk,"
                " bars, observed, first_t, last_t, status, fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(market["market_id"]), str(market["token_id"]), market.get("outcome"), market.get("payoff"),
                 market.get("question"), market["reference_time"], market.get("volume"), market.get("neg_risk"),
                 len(series), int(observed), series[0][0] if series else None, series[-1][0] if series else None,
                 status[:40], datetime.now(timezone.utc).isoformat(timespec="seconds")))
            if cur.rowcount != 1:              # another builder stored this market a moment ago: keep theirs
                return False
            db.executemany("INSERT INTO bars(bar_id, t, price) VALUES(?,?,?)",
                           [(cur.lastrowid, int(t), float(p)) for t, p in series])
            return True

    def bar_markets(self, status: str = "OK") -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM bar_markets WHERE status=? ORDER BY reference_time, market_id", (status,)).fetchall()]

    def bar_series(self, status: str = "OK") -> list[dict[str, Any]]:
        """Every stored series of that status, oldest resolution first, each with its closes."""
        with self._db() as db:
            markets = [dict(r) for r in db.execute(
                "SELECT * FROM bar_markets WHERE status=? ORDER BY reference_time, market_id", (status,)).fetchall()]
            for m in markets:
                rows = db.execute("SELECT t, price FROM bars WHERE bar_id=? ORDER BY t", (m["bar_id"],)).fetchall()
                m["t"], m["closes"] = [r["t"] for r in rows], [r["price"] for r in rows]
        return markets

    def bar_series_for(self, bar_ids: list[int]) -> list[dict[str, Any]]:
        out = []
        with self._db() as db:
            for bar_id in bar_ids:
                m = db.execute("SELECT bar_id, market_id, reference_time FROM bar_markets WHERE bar_id=?", (int(bar_id),)).fetchone()
                if m is None:
                    continue
                rows = db.execute("SELECT t, price FROM bars WHERE bar_id=? ORDER BY t", (int(bar_id),)).fetchall()
                out.append({**dict(m), "t": [r["t"] for r in rows], "closes": [r["price"] for r in rows]})
        return out

    def bar_stats(self) -> dict[str, Any]:
        with self._db() as db:
            by = db.execute("SELECT status, COUNT(*) n, COALESCE(SUM(bars), 0) bars, COALESCE(SUM(observed), 0) observed"
                            " FROM bar_markets GROUP BY status ORDER BY status").fetchall()
            span = db.execute("SELECT MIN(reference_time) a, MAX(reference_time) b FROM bar_markets WHERE status='OK'").fetchone()
            eligible = db.execute("SELECT COUNT(*) n FROM calib_markets WHERE end_anchor='done'").fetchone()["n"]
        return {"markets": {r["status"]: r["n"] for r in by}, "bars": {r["status"]: r["bars"] for r in by},
                "observed": {r["status"]: r["observed"] for r in by},
                "first_resolution": span["a"], "last_resolution": span["b"], "calibration_markets": eligible}

    def calibration_stats(self) -> dict[str, Any]:
        with self._db() as db:
            markets = db.execute("SELECT COUNT(*) n, MIN(reference_time) a, MAX(reference_time) b"
                                 " FROM calib_markets").fetchone()
            points = db.execute("SELECT horizon_hours h, COUNT(*) n FROM calib_points GROUP BY horizon_hours"
                                " ORDER BY horizon_hours").fetchall()
            skipped = db.execute("SELECT reason, COUNT(*) n FROM calib_skipped GROUP BY reason"
                                 " ORDER BY n DESC").fetchall()
        return {"markets": markets["n"], "first_resolution": markets["a"], "last_resolution": markets["b"],
                "points_by_horizon": {f"{r['h']:g}h": r["n"] for r in points},
                "skipped": {r["reason"]: r["n"] for r in skipped}}
