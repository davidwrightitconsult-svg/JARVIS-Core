"""
FRIDAY book-strategy test (Phase 11C): the registered rules, run once on real
hourly history, judged by the rule fixed in 11B.

Order of events, and why:
1. The holdout boundary is frozen (a date). Markets whose outcome became known
   on or after it are the holdout. It is stored and never moves again.
2. Every signal rule is run on the DEVELOPMENT markets only.
3. A rule whose development result passes is run on the holdout. A rule that
   fails development never touches the holdout, so the holdout stays clean
   for anything tested later.
4. Results are stored with a fingerprint of the rule settings. Asking again
   returns the stored result; it is not recomputed and cannot be re-rolled.

Nothing here can trade. A PASS allows PAPER trading only, through the gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from friday.strategies import LONG, STRATEGIES, Strategy

from .bookbt import (COSTS, HOLDOUT_FRACTION, MIN_DAYS_DEV, MIN_TRADES_DEV, Z_DEVELOPMENT, Z_HOLDOUT, BookSettings,
                     split, trades_for, verdict)

BOOKRUN_VERSION = "11C.1"
META_BOUNDARY = "book_holdout_start"
META_RESULTS = "book_results"
CHUNK = 40


def fingerprint(strategies: Sequence[Strategy] = STRATEGIES) -> str:
    blob = json.dumps([(s.key, s.params, s.exit and s.exit.__dict__) for s in strategies], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- frozen split

def freeze_boundary(store) -> str:
    """The holdout start (ISO time). Set once from the markets held at that moment; never recomputed."""
    saved = store.meta_get(META_BOUNDARY)
    if saved:
        return saved
    _, hold = split(store.bar_markets("OK"))
    if not hold:
        raise ValueError("no usable hourly history yet: run `python -m friday.lab bars` first")
    boundary = str(hold[0]["reference_time"])
    store.meta_set(META_BOUNDARY, boundary)
    return boundary


def parts(store) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    boundary = freeze_boundary(store)
    markets = store.bar_markets("OK")
    return (boundary, [m for m in markets if str(m["reference_time"]) < boundary],
            [m for m in markets if str(m["reference_time"]) >= boundary])


# ---------------------------------------------------------------- aggregation (small, mergeable)

def _empty() -> dict[str, Any]:
    return {"n": 0, "gross": 0.0, "days": {}, "wins": [0] * len(COSTS), "exits": {}, "long": 0, "markets": 0}


def aggregate(trades: list[dict[str, Any]]) -> dict[str, Any]:
    a = _empty()
    for t in trades:
        a["n"] += 1
        a["gross"] += t["gross"]
        d = a["days"].setdefault(t["day"], [0, 0.0])
        d[0] += 1
        d[1] += t["gross"]
        for i, c in enumerate(COSTS):
            a["wins"][i] += t["gross"] - 2 * c > 0
        a["exits"][t["reason"]] = a["exits"].get(t["reason"], 0) + 1
        a["long"] += t["side"] == LONG
    a["markets"] = 1 if trades else 0
    return a


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a["n"] += b["n"]
    a["gross"] += b["gross"]
    for day, (n, g) in b["days"].items():
        d = a["days"].setdefault(day, [0, 0.0])
        d[0] += n
        d[1] += g
    a["wins"] = [x + y for x, y in zip(a["wins"], b["wins"])]
    for k, v in b["exits"].items():
        a["exits"][k] = a["exits"].get(k, 0) + v
    a["long"] += b["long"]
    a["markets"] += b["markets"]
    return a


def summary(a: dict[str, Any], cost: float, z: float, stop: float) -> dict[str, Any]:
    """The same numbers bookbt.summarise gives, from the mergeable totals."""
    n = a["n"]
    if not n:
        return {"trades": 0, "markets": 0, "days": 0, "avg_net": None, "interval": None, "avg_gross": None,
                "avg_r": None, "win_rate": None, "total_net": 0.0, "max_drawdown": 0.0, "exits": {}, "long": 0, "short": 0,
                "by_cost": {}}
    mean = a["gross"] / n - 2 * cost
    g = len(a["days"])
    interval = None
    if g >= 2:
        resid = [gross - 2 * cost * k - mean * k for k, gross in a["days"].values()]
        se = math.sqrt(g / (g - 1) * sum(x * x for x in resid)) / n
        interval = [round(mean - z * se, 5), round(mean + z * se, 5)]
    path = peak = drawdown = 0.0
    for day in sorted(a["days"], key=int):
        k, gross = a["days"][day]
        path += gross - 2 * cost * k
        peak = max(peak, path)
        drawdown = max(drawdown, peak - path)
    ci = COSTS.index(cost) if cost in COSTS else None
    return {"trades": n, "markets": a["markets"], "days": g, "avg_net": round(mean, 5), "interval": interval,
            "avg_gross": round(a["gross"] / n, 5), "avg_r": round(mean / stop, 4),
            "win_rate": round(a["wins"][ci] / n, 3) if ci is not None else None,
            "total_net": round(mean * n, 4), "max_drawdown": round(drawdown, 4), "exits": dict(a["exits"]),
            "long": a["long"], "short": n - a["long"],
            "by_cost": {f"{c * 100:g}c": round(a["gross"] / n - 2 * c, 5) for c in COSTS}}


# ---------------------------------------------------------------- running

def _work(args: tuple[str, list[int], list[str]]) -> dict[str, dict[str, Any]]:
    """One chunk of markets, in a worker process: opens the store read-only-style and returns totals."""
    path, bar_ids, keys = args
    from friday.market.store import MarketStore
    from friday.strategies import REGISTRY
    store = MarketStore(path)
    out = {k: _empty() for k in keys}
    for m in store.bar_series_for(bar_ids):
        for k in keys:
            merge(out[k], aggregate(trades_for(REGISTRY[k], m["closes"], m["t"], str(m["market_id"]))))
    return out


def run_part(store, markets: list[dict[str, Any]], strategies: Sequence[Strategy], workers: int = 1,
             progress: Callable[[int, int], None] | None = None) -> dict[str, dict[str, Any]]:
    totals = {s.key: _empty() for s in strategies}
    ids = [m["bar_id"] for m in markets]
    chunks = [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]
    registered = all(s.key in {x.key for x in STRATEGIES} and s is next(x for x in STRATEGIES if x.key == s.key)
                     for s in strategies)
    done = 0
    if workers > 1 and registered and len(chunks) > 1:
        keys = [s.key for s in strategies]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(_work, [(str(store.path), c, keys) for c in chunks]):
                for k, a in part.items():
                    merge(totals[k], a)
                done += 1
                if progress:
                    progress(min(done * CHUNK, len(ids)), len(ids))
    else:
        for c in chunks:
            for m in store.bar_series_for(c):
                for s in strategies:
                    merge(totals[s.key], aggregate(trades_for(s, m["closes"], m["t"], str(m["market_id"]))))
            done += 1
            if progress:
                progress(min(done * CHUNK, len(ids)), len(ids))
    return totals


def run_book_test(store, settings: BookSettings | None = None, strategies: Sequence[Strategy] | None = None,
                  workers: int | None = None, progress: Callable[[str, int, int], None] | None = None,
                  save: bool = True) -> dict[str, Any]:
    """Development for every rule, holdout only for those that pass. Stored; never recomputed for the
    same settings fingerprint and cost."""
    s = settings or BookSettings()
    rules = [x for x in (strategies or STRATEGIES) if x.kind == "SIGNAL"]
    fp = fingerprint(rules if strategies else STRATEGIES)
    if save:
        old = stored_results(store)
        if old and old["fingerprint"] == fp and old["cost_per_side"] == s.cost_per_side:
            return {**old, "from_store": True}
    workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1))
    boundary, dev, hold = parts(store)
    dev_tot = run_part(store, dev, rules, workers, (lambda a, b: progress("development", a, b)) if progress else None)
    rows = []
    passed = []
    for r in rules:
        d = summary(dev_tot[r.key], s.cost_per_side, Z_DEVELOPMENT, r.exit.stop)
        rows.append({"strategy": r.key, "name": r.name, "development": d, "holdout": None, "verdict": verdict(d, None)})
        if rows[-1]["verdict"].startswith("PASSED_DEVELOPMENT"):
            passed.append(r)
    if passed:
        hold_tot = run_part(store, hold, passed, workers, (lambda a, b: progress("holdout", a, b)) if progress else None)
        for row in rows:
            r = next((x for x in passed if x.key == row["strategy"]), None)
            if r is not None:
                row["holdout"] = summary(hold_tot[r.key], s.cost_per_side, Z_HOLDOUT, r.exit.stop)
                row["verdict"] = verdict(row["development"], row["holdout"])
    result = {"bookrun_version": BOOKRUN_VERSION, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "fingerprint": fp, "cost_per_side": s.cost_per_side, "holdout_start": boundary,
              "development_markets": len(dev), "holdout_markets": len(hold),
              "holdout_run_for": [r.key for r in passed],
              "rule": {"development": f"{MIN_TRADES_DEV} trades on {MIN_DAYS_DEV} days; 99.5% interval above zero",
                       "holdout": "95% interval above zero", "holdout_fraction": HOLDOUT_FRACTION},
              "strategies": rows, "passed": [r["strategy"] for r in rows if r["verdict"].startswith("PASSED (")],
              "execution_locked": True}
    if save:
        store.meta_set(META_RESULTS, json.dumps(result))
    return result


def stored_results(store) -> dict[str, Any] | None:
    raw = store.meta_get(META_RESULTS)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def gate_entries(store) -> list[dict[str, Any]]:
    """What the validation gate says about each tested book rule. A result made with other settings
    than the registry holds today approves nothing."""
    res = stored_results(store)
    if not res:
        return []
    current = res["fingerprint"] == fingerprint()
    out = []
    for row in res["strategies"]:
        ok = current and row["verdict"].startswith("PASSED (")
        d, h = row["development"], row["holdout"]
        out.append({"strategy": row["strategy"], "status": "APPROVED_FOR_PAPER" if ok else "NOT_APPROVED",
                    "reason": row["verdict"] if current else "tested with settings the registry no longer holds; must be re-tested",
                    "evidence": {"source": "book backtest on hourly history (11C)", "book": True, "name": row["name"],
                                 "trades": d["trades"], "days": d["days"], "markets": d["markets"], "avg_net": d["avg_net"],
                                 "interval": d["interval"], "avg_gross": d["avg_gross"], "win_rate": d["win_rate"],
                                 "cost_per_side": res["cost_per_side"],
                                 "holdout": None if not h else {k: h[k] for k in ("trades", "days", "avg_net", "interval")}}})
    return out


def render_test(r: dict[str, Any]) -> str:
    c = r["cost_per_side"] * 100
    lines = [f"BOOK STRATEGY TEST {r['bookrun_version']} — {r['at']}" + ("  (stored result)" if r.get("from_store") else ""),
             f"development: {r['development_markets']} markets resolved before {r['holdout_start'][:10]} · "
             f"holdout: {r['holdout_markets']} markets from that date · cost {c:g}c a side",
             f"settings fingerprint {r['fingerprint'][:8]}", "",
             f"{'strategy':<21} {'trades':>7} {'mkts':>5} {'days':>4} {'win':>4} {'net c':>6} {'99.5% interval (c)':>19} "
             f"{'gross c':>7} {'at 0.5c':>7} {'avg R':>6}  verdict"]
    for row in r["strategies"]:
        d = row["development"]
        if not d["trades"]:
            lines.append(f"{row['name']:<21} {0:>7} {'':>5} {'':>4} {'':>4} {'':>6} {'':>19} {'':>7} {'':>7} {'':>6}  {row['verdict']}")
            continue
        iv = f"{d['interval'][0] * 100:+.2f} to {d['interval'][1] * 100:+.2f}" if d["interval"] else "n/a"
        win = "n/a" if d["win_rate"] is None else f"{d['win_rate']:.0%}"
        lines.append(f"{row['name']:<21} {d['trades']:>7} {d['markets']:>5} {d['days']:>4} {win:>4} "
                     f"{d['avg_net'] * 100:>+6.2f} {iv:>19} {d['avg_gross'] * 100:>+7.2f} {d['by_cost']['0.5c'] * 100:>+7.2f} "
                     f"{d['avg_r']:>+6.2f}  {row['verdict']}")
    held = [row for row in r["strategies"] if row["holdout"]]
    lines.append("")
    if held:
        lines.append("HOLDOUT (only rules that passed development):")
        for row in held:
            h = row["holdout"]
            iv = f"{h['interval'][0] * 100:+.2f} to {h['interval'][1] * 100:+.2f}" if h["interval"] else "n/a"
            net = "n/a" if h["avg_net"] is None else f"{h['avg_net'] * 100:+.2f}"
            lines.append(f"  {row['name']:<21} {h['trades']:>7} trades on {h['days']} days · net {net}c · 95% interval {iv}c")
    else:
        lines.append("HOLDOUT: not run. No rule passed development, so the holdout is untouched.")
    lines.append("")
    lines.append("PASSED: " + (", ".join(r["passed"]) if r["passed"] else "none"))
    return "\n".join(lines)
