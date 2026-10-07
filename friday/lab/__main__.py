"""
FRIDAY Validation Lab CLI (read-only).

    python -m friday.lab backtest [--horizon 24] [--folds 5] [--half-spread 0.01]
                                  [--min-price 0.03] [--max-price 0.97] [--anchor end|resolution] [--json]
    python -m friday.lab robustness [--half-spread 0.01] [--json]      horizons, leak, thresholds, real asks
    python -m friday.lab inconsistencies [--days 30] [--json]          both outcomes for under $1 (recorded asks)
    python -m friday.lab gate [--json]                                 what FRIDAY may paper-trade (9C)
    python -m friday.lab bars [--bar-days 30] [--max-markets 500] [--minutes 60]
                                                                       build hourly price series (11B; reads Polymarket)
    python -m friday.lab bars-status [--json]                          what hourly history FRIDAY holds
    python -m friday.lab book-test [--workers N] [--json]              run the book rules once on that history (11C)
    python -m friday.lab book-results [--json]                         the stored result of that test

Database: --db PATH, else $FRIDAY_MARKET_DB, else ~/friday/data/market_data.db
"""

import argparse
import json
import os
import sys

from friday.market.store import DEFAULT_MARKET_DB, MarketStore, MarketStoreError
from friday.quant.fair import EdgeSettings

from .backtest import BacktestSettings, render_text, run_backtest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m friday.lab", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["backtest", "robustness", "inconsistencies", "gate", "bars", "bars-status", "book-test", "book-results"])
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--anchor", choices=["end", "resolution"], default="end")
    parser.add_argument("--db", default=os.getenv("FRIDAY_MARKET_DB", DEFAULT_MARKET_DB))
    parser.add_argument("--horizon", type=float, default=24)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--half-spread", type=float, default=0.01)
    parser.add_argument("--min-price", type=float, default=0.03)
    parser.add_argument("--max-price", type=float, default=0.97)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--bar-days", type=int, default=30)
    parser.add_argument("--max-markets", type=int, default=500)
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--workers", type=int, default=0)
    a = parser.parse_args(argv)
    path = os.path.expanduser(a.db)
    if not os.path.exists(path):
        print(f"no market store at {path}")
        return 2
    try:
        store = MarketStore(path)
        settings = BacktestSettings(horizon_hours=a.horizon, folds=a.folds, half_spread=a.half_spread,
                                    min_price=a.min_price, max_price=a.max_price, anchor=a.anchor,
                                    edge=EdgeSettings.from_environment())
    except (MarketStoreError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    if a.command in ("book-test", "book-results"):
        from .bookbt import BookSettings
        from .bookrun import render_test, run_book_test, stored_results
        if a.command == "book-results":
            result = stored_results(store)
            if not result:
                print("no book test has been run yet")
                return 1
            result["from_store"] = True
        else:
            last = {"at": 0.0}

            def progress(part, done, total):
                import time as _t
                if _t.monotonic() - last["at"] > 60 or done >= total:
                    last["at"] = _t.monotonic()
                    print(f"  ... {part}: {done} of {total} markets", flush=True)
            try:
                result = run_book_test(store, BookSettings(a.half_spread), workers=a.workers or None, progress=progress)
            except ValueError as exc:
                print(f"ERROR: {exc}")
                return 2
        print(json.dumps(result, indent=2, default=str) if a.json else render_test(result))
        return 0
    if a.command in ("bars", "bars-status"):
        from friday.market.bars import BarBuilder
        from .bookbt import split
        if a.command == "bars":
            from friday.app.polymarket import PolyMarketClient
            builder = BarBuilder(PolyMarketClient(), store, days=a.bar_days,
                                 pace_seconds=float(os.getenv("FRIDAY_CALIBRATION_PACE_SECONDS", "0.05") or 0.05))
            summary = builder.run(a.max_markets, a.minutes * 60, progress=lambda s: print(
                f"  ... {s['fetched']} fetched ({s['ok']} usable, {s['thin']} thin, {s['empty']} empty)", flush=True))
            print(f"built: {summary['fetched']} markets fetched, {summary['ok']} usable, {summary['thin']} thin, "
                  f"{summary['empty']} empty, {summary['out_of_scope']} out of scope, {summary['errors']} errors; "
                  f"stopped: {summary['stopped']}" + (f" ({summary['last_error']})" if summary.get("last_error") else ""))
        stats = store.bar_stats()
        frozen = store.meta_get("book_holdout_start")
        if frozen:
            ok = store.bar_markets("OK")
            dev = [m for m in ok if str(m["reference_time"]) < frozen]
            hold = [m for m in ok if str(m["reference_time"]) >= frozen]
        else:
            dev, hold = split(store.bar_markets("OK"))
        stats["development_markets"], stats["holdout_markets"] = len(dev), len(hold)
        stats["holdout_starts"] = frozen or (hold[0]["reference_time"] if hold else None)
        stats["holdout_frozen"] = bool(frozen)
        if a.json:
            print(json.dumps(stats, indent=2, default=str))
        else:
            m, b, o = stats["markets"], stats["bars"], stats["observed"]
            done = sum(m.values())
            print(f"hourly history: {done} of {stats['calibration_markets']} resolved markets fetched")
            print(f"  usable   : {m.get('OK', 0)} markets, {b.get('OK', 0)} hourly bars "
                  f"({o.get('OK', 0)} hours with a reported price)")
            print(f"  thin     : {m.get('THIN', 0)} (too few hours with a price; never tested)")
            print(f"  empty    : {m.get('EMPTY', 0)} · out of scope: {m.get('OUT_OF_SCOPE', 0)}")
            if m.get("OK"):
                print(f"  resolved : {str(stats['first_resolution'])[:10]} to {str(stats['last_resolution'])[:10]}")
                print(f"  split    : {len(dev)} development markets, {len(hold)} holdout markets "
                      f"(holdout = resolved on or after {str(stats['holdout_starts'])[:10]}"
                      + ("; boundary frozen)" if frozen else "; not yet frozen)"))
        return 0
    if a.command == "inconsistencies":
        from .inconsistency import render_inconsistencies, scan_inconsistencies
        result = scan_inconsistencies(store, days=a.days, fee=settings.edge.fee)
        print(json.dumps(result, indent=2, default=str) if a.json else render_inconsistencies(result))
        return 0
    if a.command == "gate":
        from .gate import build_gate, render_gate
        result = build_gate(store, settings)
        print(json.dumps(result, indent=2, default=str) if a.json else render_gate(result))
        return 0
    if a.command == "robustness":
        from .robustness import render_robustness, run_robustness
        result = run_robustness(store, settings, headline_horizon=a.horizon)
        print(json.dumps(result, indent=2, default=str) if a.json else render_robustness(result))
        return 0
    result = run_backtest(store, settings)
    print(json.dumps(result, indent=2, default=str) if a.json else render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
