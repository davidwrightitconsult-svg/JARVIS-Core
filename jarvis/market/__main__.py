"""
FRIDAY market data CLI (Phase 6B/6C). Read-only against Polymarket.

    python -m jarvis.market snapshot [--limit 25]     take one live snapshot and store it
    python -m jarvis.market stats [--days 7]          store statistics (quality, depth check, size)
    python -m jarvis.market markets [--limit 20]      markets being tracked
    python -m jarvis.market history TOKEN_ID [--hours 24]
    python -m jarvis.market prune                     apply retention now
    python -m jarvis.market export [--token T | --market M] [--hours 168] [--format csv|json] [--out FILE]
    python -m jarvis.market context "command text" [--market M]   what Claude would be given (no Claude)
    python -m jarvis.market backfill [--markets 200] [--min-volume 1000]   calibration dataset (8A, read-only)
    python -m jarvis.market reanchor [--markets 300]                       end-anchored prices for it (9B)
    python -m jarvis.market rebuild [--days 7] [--markets 200] [--min-volume 1000]
                                     markets that reached their end, by end-date window (9B.2)

Database: --db PATH, else $JARVIS_MARKET_DB, else ~/jarvis/data/market_data.db
"""

import argparse
import json
import os
import sys

from .store import DEFAULT_MARKET_DB, MarketStore, MarketStoreError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.market")
    parser.add_argument("command", choices=["snapshot", "stats", "markets", "history", "prune",
                                            "export", "context", "backfill", "reanchor", "rebuild"])
    parser.add_argument("--markets", type=int, default=200)
    parser.add_argument("--min-volume", type=float, default=1000)
    parser.add_argument("args", nargs="*")
    parser.add_argument("--db", default=os.getenv("JARVIS_MARKET_DB", DEFAULT_MARKET_DB))
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--market", default=None)
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--out", default=None)
    a = parser.parse_args(argv)
    try:
        store = MarketStore(a.db)
    except MarketStoreError as exc:
        print(f"ERROR: {exc}")
        return 2

    if a.command == "snapshot":
        from jarvis.app.config import GAMMA_HOST, POLYMARKET_HOST
        from jarvis.app.polymarket import market_filter_settings
        from .client import PolymarketData
        import httpx
        try:
            settings = market_filter_settings()
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST, store=store, **settings)
        try:
            rows, report = client.snapshot(limit=a.limit)
        except httpx.HTTPError as exc:
            print(f"ERROR: Polymarket unreachable ({type(exc).__name__}: {str(exc)[:160]})")
            return 1
        print(json.dumps({k: v for k, v in report.as_dict().items()}, indent=2))
        return 0 if report.stored_run else 1
    if a.command == "stats":
        print(json.dumps(store.stats(days=a.days), indent=2))
        return 0
    if a.command == "markets":
        for m in store.markets(limit=a.limit):
            names = " / ".join(f"{o['name']}={o['token_id'][:10]}…" for o in m["outcomes"])
            print(f"{m['market_id']:>8}  {m['snapshots']:>5} snaps  {m['question'][:60]}  [{names}]")
        return 0
    if a.command == "history":
        if not a.args:
            print("ERROR: usage: history TOKEN_ID [--hours 24]")
            return 2
        for r in store.history(a.args[0], hours=a.hours or 24):
            print(f"{r['observed_at']}  bid {r['best_bid']}  ask {r['best_ask']}  mid {r['mid']}  "
                  f"depth ${r['depth_usd'] or 0:,.0f}  {','.join(r['flags']) or 'ok'}")
        return 0
    if a.command == "export":
        rows = store.export_rows(token_id=a.token, market_id=a.market, hours=a.hours or 168)
        text = store.to_csv(rows) if a.format == "csv" else json.dumps(rows, indent=1)
        if a.out:
            with open(os.path.expanduser(a.out), "w", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
            print(f"exported {len(rows)} rows to {a.out}")
        else:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0
    if a.command == "context":
        from .context import MarketContext
        block = MarketContext(store).context_for(" ".join(a.args), [a.market] if a.market else None)
        print(json.dumps(block or {"markets": [], "note": "no tracked market matches"}, indent=2))
        return 0
    if a.command == "backfill":
        from jarvis.app.config import GAMMA_HOST, POLYMARKET_HOST
        from .backfill import CalibrationBackfill
        from .client import PolymarketData
        client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST)
        from .segments import excluded_segments
        result = CalibrationBackfill(client, store, min_volume=a.min_volume,
                                     exclude_segments=excluded_segments()).run(
            max_markets=a.markets, max_pages=max(30, a.markets // 20))
        print(json.dumps({**result, "dataset": store.calibration_stats()}, indent=2))
        return 0 if not result.get("errors") or result.get("added") else 1
    if a.command == "rebuild":
        from jarvis.app.config import GAMMA_HOST, POLYMARKET_HOST
        from .backfill import CalibrationBackfill
        from .client import PolymarketData
        from .segments import excluded_segments
        client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST)
        result = CalibrationBackfill(client, store, min_volume=a.min_volume, pace_seconds=0.05,
                                     exclude_segments=excluded_segments()).run_windows(a.days, a.markets)
        print(json.dumps({**result, "end_anchor": store.end_anchor_stats()}, indent=2))
        return 0
    if a.command == "reanchor":
        from jarvis.app.config import GAMMA_HOST, POLYMARKET_HOST
        from .backfill import CalibrationBackfill
        from .client import PolymarketData
        from .segments import excluded_segments
        client = PolymarketData(clob_host=POLYMARKET_HOST, gamma_host=GAMMA_HOST)
        result = CalibrationBackfill(client, store, exclude_segments=excluded_segments()).reanchor(a.markets)
        print(json.dumps({**result, "end_anchor": store.end_anchor_stats()}, indent=2))
        return 0
    if a.command == "prune":
        print(json.dumps(store.prune(), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
