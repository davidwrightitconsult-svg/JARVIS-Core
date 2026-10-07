"""
FRIDAY Quant Core CLI (Phase 7A). Reads the market store only.

    python -m jarvis.quant universe                 eligible markets + rejection reasons
    python -m jarvis.quant features [--market ID] [--eligible] [--limit 20]
    python -m jarvis.quant strategies               the strategy library + signal counts (7B)
    python -m jarvis.quant signals [--hours 24] [--strategy NAME] [--market ID] [--open]
    python -m jarvis.quant watchlist [--hours 48]
    python -m jarvis.quant run [--record]           evaluate strategies on the latest run
                                                    (preview by default; --record writes signals)
    python -m jarvis.quant score [--record]         score due signals (preview by default) (7C)
    python -m jarvis.quant scorecard [--days 30] [--json]   per-strategy results after costs
    python -m jarvis.quant outcomes [--strategy NAME] [--days 7] [--limit 20]
    python -m jarvis.quant calibration [--horizon 24] [--min-volume 0] [--json]   (8A)
    python -m jarvis.quant edges [--market ID] [--limit 20] [--json]   fair value + edge vs live ask (8B)

Database: --db PATH, else $JARVIS_MARKET_DB, else ~/jarvis/data/market_data.db
"""

import argparse
import json
import os
import sys

from jarvis.market.store import DEFAULT_MARKET_DB, MarketStore, MarketStoreError

from .engine import QuantEngine
from .pipeline import QuantPipeline
from .scorecard import build_scorecard
from .scoring import Scorer
from .strategies import LIBRARY, enabled_strategies
from .universe import UniverseRules


def _fmt(v, digits=3):
    return "—" if v is None else f"{v:+.{digits}f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.quant")
    parser.add_argument("command", choices=["universe", "features", "strategies", "signals", "watchlist", "run",
                                            "score", "scorecard", "outcomes", "calibration", "edges"])
    parser.add_argument("--horizon", type=float, default=24)
    parser.add_argument("--min-volume", type=float, default=0)
    parser.add_argument("--segment", choices=["sports", "field", "binary"], default=None)
    parser.add_argument("--days", type=float, default=None)
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--db", default=os.getenv("JARVIS_MARKET_DB", DEFAULT_MARKET_DB))
    parser.add_argument("--market", default=None)
    parser.add_argument("--eligible", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    a = parser.parse_args(argv)
    try:
        engine = QuantEngine(MarketStore(a.db), UniverseRules.from_environment())
    except (MarketStoreError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    if a.command == "universe":
        print(json.dumps(engine.universe(), indent=2))
        return 0
    store = engine.store
    if a.command == "strategies":
        counts = store.signal_counts(hours=a.hours or 24)
        for s in LIBRARY:
            c = counts.get(s.name, {"signals": 0, "open": 0, "up": 0, "down": 0})
            print(f"{s.name:<15} {s.horizon_hours:>4g}h  signals {c['signals']:>4} (open {c['open']}, "
                  f"up {c['up']}, down {c['down']})\n    hypothesis: {s.hypothesis}\n    rule: {s.rule}")
        return 0
    if a.command == "signals":
        rows = store.signals(hours=a.hours or 24, strategy=a.strategy.upper() if a.strategy else None,
                             market_id=a.market, open_only=a.open, limit=a.limit)
        for g in rows:
            print(f"{g['observed_at']}  {g['strategy']:<15} {g['direction']:<4} str {g['strength']:.2f} "
                  f"{g['horizon_hours']:g}h  mid {g['mid']} (bid {g['best_bid']} ask {g['best_ask']})  "
                  f"{(g['question'] or '')[:50]} — {g['outcome']}")
        if not rows:
            print("no signals in the window")
        return 0
    if a.command == "watchlist":
        rows = store.watchlist(a.hours or 48)
        for w in rows:
            print(f"{w['market_id']:>8}  last eligible {w['last_eligible_at']}  {(w['question'] or '')[:70]}")
        print(f"{len(rows)} markets on the watchlist")
        return 0
    if a.command == "score":
        result = Scorer(store, record=a.record).score_due()
        outcomes = result.pop("outcomes", [])
        print(json.dumps(result, indent=2))
        for o in outcomes[:a.limit]:
            print(f"  signal {o['signal_id']}: {o['status']} net {o.get('net')} mid_move {o.get('mid_move')}")
        return 0
    if a.command == "scorecard":
        card = build_scorecard(store, days=a.days or 30)
        if a.json:
            print(json.dumps(card, indent=2))
            return 0
        def c(x):
            return "     —" if x is None else f"{x * 100:+6.2f}c"
        for s in card["strategies"]:
            ci = s["ci95_net"]
            print(f"{s['strategy']:<15} signals {s['signals']:>4} scored {s['scored']:>4} open {s['open']:>3} "
                  f"no-exit {s['no_exit_data']:>3} | hit {s['hit_rate'] if s['hit_rate'] is not None else '—'} "
                  f"avg net {c(s['avg_net'])} CI {('—' if not ci else c(ci[0]) + '…' + c(ci[1]))} "
                  f"| {s['verdict']}")
            bands = "  ".join(f"{k}: {c(v['avg_net'])} ({v['scored']})" for k, v in s["by_band"].items())
            print(f"{'':<15} by price band — {bands}")
        print(card["note"])
        return 0
    if a.command == "calibration":
        from .calibration import build_calibration
        cal = build_calibration(store, horizon_hours=a.horizon, min_volume=a.min_volume, segment=a.segment)
        if a.json:
            print(json.dumps(cal, indent=2))
            return 0
        d = cal["dataset"]
        print(f"dataset: {d['markets']} resolved markets ({d['first_resolution']} → {d['last_resolution']}); "
              f"points {d['points_by_horizon']}; skipped {d['skipped']}")
        segs = "; ".join(f"{k} {v['markets']} mkts (long shots {v['longshots']['verdict']}, favourites "
                         f"{v['favourites']['verdict']})" for k, v in cal["by_segment"].items())
        print(f"segments: {segs}")
        print(f"segment {cal['segment']} · horizon {cal['horizon_hours']:g}h: {cal['samples']} samples, {cal['markets']} markets, "
              f"Brier {cal['brier']} (no-skill {cal['baseline_brier']}), log loss {cal['log_loss']}")
        def pc(x):
            return "     —" if x is None else f"{x * 100:5.1f}%"
        print(f"{'price bin':<11} {'samples':>7} {'markets':>7}  priced   won   95% CI          bias    verdict")
        for b in cal["bins"]:
            ci = b["win_rate_ci95"]
            bias = "     —" if b["bias"] is None else f"{b['bias'] * 100:+5.1f}pp"
            print(f"{b['bin']:<11} {b['samples']:>7} {b['markets']:>7} {pc(b['mean_price'])} {pc(b['win_rate'])} "
                  f"{('—' if not ci else pc(ci[0]) + '…' + pc(ci[1])):<16} {bias}  {b['verdict']}")
        for key in ("longshots", "favourites"):
            g = cal[key]
            print(f"{key:<11} {g['samples']:>7} {g['markets']:>7} {pc(g['mean_price'])} {pc(g['win_rate'])}  → {g['verdict']}")
        print(cal["note"])
        return 0
    if a.command == "edges":
        from .fair import EdgeSettings, FairValueModel
        try:
            model = FairValueModel(store, EdgeSettings.from_environment())
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        rows = model.edges(engine.features([a.market] if a.market else None))[:a.limit]
        if a.json:
            print(json.dumps({"edges": rows, "model": model.status()}, indent=2))
            return 0
        st = model.status()
        print(f"model {st['fair_version']}: calibration samples {st['samples_by_horizon']}; settings {st['settings']}")
        for e in rows:
            s = {x["outcome"]: x for x in e["sides"]}[e["best_side"]]
            print(f"{e['status']:<24} {e['question'][:48]:<48} p {e['price']:.3f} → fair {e['fair']:.3f} "
                  f"({e['segment']} {e['horizon_hours'] or '—'}h bin {e['bin']} n={e['bin_samples']} w={e['weight']}) | "
                  f"best {e['best_side'][:10]} ask {s['ask']} edge {e['edge'] * 100:+.2f}c z {e['z']} (stat {e['z_statistical']}) "
                  f"stake {e['stake_fraction']}")
        if not rows:
            print("no eligible binary markets")
        print("ESTIMATED and UNVALIDATED (Phases 8C-9): EDGE rows are recorded as CALIBRATION_EDGE signals and "
              "scored at resolution. Stake fractions are arithmetic only — no execution authority.")
        return 0
    if a.command == "outcomes":
        rows = store.outcomes(days=a.days or 7, strategy=a.strategy.upper() if a.strategy else None,
                              limit=a.limit)
        for r in rows:
            status = r["status"] or "OPEN"
            print(f"{r['observed_at']} {r['strategy']:<15} {r['direction']:<4} entry {r['best_bid']}/{r['best_ask']} "
                  f"→ exit {r['exit_bid']}/{r['exit_ask']}  mid_move {r['mid_move']} net {r['net']}  {status}  "
                  f"{(r['question'] or '')[:44]}")
        if not rows:
            print("no signals in the window")
        return 0
    if a.command == "run":
        try:
            strategies = enabled_strategies()
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        run = store.latest_run()
        result = QuantPipeline(engine, strategies, record=a.record).evaluate(run["run_id"] if run else None)
        print(json.dumps(result, indent=2, default=str))
        return 0
    markets = engine.features([a.market] if a.market else None)
    if a.eligible:
        markets = [m for m in markets if m.eligible]
    markets = markets[:max(1, a.limit)]
    if a.json:
        print(json.dumps([m.as_dict() for m in markets], indent=2))
        return 0
    for m in markets:
        tag = f"ELIGIBLE attention {m.attention}" if m.eligible else "; ".join(m.reasons)
        print(f"{m.market_id:>8}  {m.question[:64]}  [{tag}]")
        for o in m.outcomes:
            print(f"          {o.outcome[:16]:<16} bid {o.best_bid} ask {o.best_ask} depth ${o.depth_usd:,.0f} "
                  f"1h {_fmt(o.change_1h)} 24h {_fmt(o.change_24h)} vol {o.volatility} imb {o.imbalance} "
                  f"obs {o.observations}")
    if not markets:
        print("no markets (the store has no runs yet, or none match)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
