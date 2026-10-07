"""
FRIDAY Validation Lab — walk-forward backtest of the calibration edge (Phase 9A).

Phase 8 measured a bias and built a fair-value model on it. That is
IN-SAMPLE evidence: the same resolved markets taught the model and judged
it. 9A asks the only question that matters before anything depends on the
model: had FRIDAY traded its rule on markets it had NOT yet seen, what
would it have made after costs?

Method (deterministic, no network, reads the 8A calibration dataset):

  universe   resolved two-outcome markets of the in-scope segments
             (FRIDAY_EXCLUDED_SEGMENTS; sports are out), with a recorded
             price for both outcomes `horizon` hours before resolution that
             sums to ~1, and a primary price inside the live universe's
             price band (3c-97c by default — FRIDAY never looks at markets
             outside it, so the backtest may not either)
  folds      markets sorted by resolution time, cut into folds+1 equal
             chunks; chunk 0 only ever trains. Each later chunk is TESTED
             with tables built from markets that had already resolved
             `horizon` hours before the chunk's first resolution — i.e.
             before the first test price was even observed. No test market
             and nothing later than it ever enters its own training set.
  rule       exactly the live rule (fair.py arithmetic): the price band's
             bias in the market's segment, shrunk by n/(n+200) and capped;
             edge = fair - ask - fee; bet the better side only when the
             band has >= 30 training samples, edge >= FRIDAY_EDGE_MIN and
             z >= FRIDAY_EDGE_MIN_Z (with the model-uncertainty floor)
  costs      the dataset holds traded/mid prices, not asks, so the ask is
             ASSUMED: price + `half_spread` (default 1c). The result is
             reported for 0 / 0.5 / 1 / 2 / 3c so its sensitivity is visible
  result     held to resolution: payoff - ask - fee per $1 share

Reported: bets, win rate, mean net per share with a 95% interval, return
on capital committed, a fixed-fraction bankroll path (sum of stake x
return, max drawdown) — and the same by market type, segment, bought-price
band, side and fold, plus the in-sample figure for comparison.

Two intervals are given. The per-bet interval treats bets as independent;
they are not (ten "number one song" markets resolve together), so the
verdict uses the DAY-CLUSTERED interval, which treats each resolution day
as one observation:

  INSUFFICIENT_DATA     fewer than 30 bets or fewer than 5 resolution days
  NEGATIVE_OUT_OF_SAMPLE  clustered interval entirely below zero
  INCONCLUSIVE          it straddles zero
  PASSES_OUT_OF_SAMPLE  entirely above zero — necessary, not sufficient:
                        costs are assumed, and paper trading (Phase 12)
                        must reproduce it with real asks

9B — anchor. 9A ran on the 8A points (prices a fixed time before the ACTUAL
resolution) and passed with +11c per share — but those points know when a
market resolved, which FRIDAY never does for a market that resolves early.
The default is now anchor="end": prices h hours before the SCHEDULED end
date, for markets still open then. A bet's decision time is end - h, folds
are cut by decision time, and a fold trains only on markets that had
RESOLVED before its first decision. anchor="resolution" is kept so the two
can be compared: the gap between them is the size of the leak.

9B — real asks. Where FRIDAY itself recorded the order book of a market at
its decision time, the backtest can price the bet at the recorded ask
instead of an assumed one (`asks`), and reports how far the recorded ask
sat above the history price.

Nothing here places, sizes or approves anything.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from friday.market.segments import SEGMENTS, category_of, excluded_segments, segment_of
from friday.quant.calibration import BIN_EDGES, MIN_BIN_SAMPLE, _bin
from friday.quant.fair import EdgeSettings, bin_table, kelly, shrunk_fair

BACKTEST_VERSION = "9B.1"
MIN_BETS = 30
MIN_DAYS = 5
COST_GRID = (0.0, 0.005, 0.01, 0.02, 0.03)
BANDS = (("<0.10", 0.0, 0.10), ("0.10-0.50", 0.10, 0.50), ("0.50-0.90", 0.50, 0.90), (">=0.90", 0.90, 1.01))


@dataclass(frozen=True)
class BacktestSettings:
    horizon_hours: float = 24.0
    folds: int = 5
    half_spread: float = 0.01            # assumed distance from the recorded price to the ask
    min_price: float = 0.03              # the live universe's primary-price band
    max_price: float = 0.97
    min_train_markets: int = 100
    anchor: str = "end"                  # "end" (scheduled end date, 9B) or "resolution" (8A points; leaky)
    edge: EdgeSettings = field(default_factory=EdgeSettings)

    def __post_init__(self):
        if not 2 <= self.folds <= 20:
            raise ValueError("folds must be 2..20")
        if not 0 <= self.half_spread <= 0.1:
            raise ValueError("half_spread must be 0..0.1")
        if not 0 <= self.min_price < self.max_price <= 1:
            raise ValueError("price band must satisfy 0 <= min_price < max_price <= 1")
        if self.anchor not in ("end", "resolution"):
            raise ValueError("anchor must be 'end' or 'resolution'")
        if self.horizon_hours <= 0 or self.min_train_markets < 1:
            raise ValueError("horizon_hours must be > 0 and min_train_markets >= 1")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["edge"] = self.edge.as_dict()
        return d


def _r(x, d=4):
    return None if x is None else round(x, d)


def load_markets(store, horizon_hours: float, excluded: frozenset[str] | None = None, anchor: str = "resolution"
                 ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Two-outcome in-scope markets with both prices at the horizon, oldest decision first.
    `t` is the decision time: resolution - h (anchor resolution) or scheduled end - h (anchor end)."""
    excluded = excluded_segments() if excluded is None else frozenset(excluded)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for p in store.calibration_points(horizon_hours, anchor=anchor):
        grouped.setdefault(p["market_id"], []).append(p)
    out, dropped = [], {"out of scope": 0, "not two outcomes": 0, "prices do not sum to 1": 0, "no resolution time": 0}
    for market_id, pts in grouped.items():
        first = pts[0]
        segment = segment_of(first.get("question"), first.get("neg_risk"))
        if segment in excluded:
            dropped["out of scope"] += 1
            continue
        if len(pts) != 2:
            dropped["not two outcomes"] += 1
            continue
        pts = sorted(pts, key=lambda p: p["token_id"])
        if abs(pts[0]["price"] + pts[1]["price"] - 1) > 0.05 or pts[0]["payoff"] + pts[1]["payoff"] != 1:
            dropped["prices do not sum to 1"] += 1
            continue
        try:
            ref = datetime.fromisoformat(str(first["reference_time"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            dropped["no resolution time"] += 1
            continue
        base = ref
        if anchor == "end":
            try:
                base = datetime.fromisoformat(str(first.get("end_date")).replace("Z", "+00:00"))
                if base.tzinfo is None:
                    base = base.replace(tzinfo=ref.tzinfo)
            except (TypeError, ValueError):
                dropped["no resolution time"] += 1
                continue
        out.append({"market_id": market_id, "question": first.get("question") or "", "segment": segment,
                    "category": category_of(first.get("question")), "ref": ref,
                    "t": base - timedelta(hours=horizon_hours),
                    "tokens": [(p["price"], p["payoff"], p.get("outcome"), p["token_id"]) for p in pts]})
    out.sort(key=lambda m: (m["t"], m["market_id"]))
    return out, dropped


def _tables(markets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_seg: dict[str, list[dict[str, Any]]] = {s: [] for s in SEGMENTS}
    for m in markets:
        by_seg[m["segment"]].extend({"price": t[0], "payoff": t[1]} for t in m["tokens"])
    return {s: bin_table(pts) for s, pts in by_seg.items()}


def decide(m: dict[str, Any], tables: dict[str, list[dict[str, Any]]], s: BacktestSettings,
           half_spread: float | None = None, asks: tuple[float, float] | None = None) -> dict[str, Any] | None:
    """The live rule applied to one resolved market. None = no bet.
    `asks`: recorded asks for the two outcomes (9B) instead of price + half-spread."""
    hs = s.half_spread if half_spread is None else half_spread
    p0 = m["tokens"][0][0]
    b = tables[m["segment"]][_bin(p0)]
    if b["n"] < MIN_BIN_SAMPLE:
        return None
    fair0, w, adj, se = shrunk_fair(b, p0)
    total_se = math.sqrt(se ** 2 + s.edge.model_se ** 2) if se else None
    best = None
    for i, q in enumerate((fair0, 1 - fair0)):
        price, payoff, outcome = m["tokens"][i][:3]
        ask = asks[i] if asks is not None else min(0.999, price + hs)
        cost = ask + s.edge.fee
        edge = q - cost
        if best is None or edge > best["edge"]:
            best = {"side": i, "outcome": outcome, "fair": q, "ask": ask, "cost": cost, "edge": edge,
                    "payoff": payoff}
    z = best["edge"] / total_se if total_se else None
    if best["edge"] < s.edge.min_edge or z is None or z < s.edge.min_z:
        return None
    net = best["payoff"] - best["cost"]
    stake = min(s.edge.max_stake, s.edge.kelly_fraction * kelly(best["fair"], best["cost"]))
    return {"market_id": m["market_id"], "question": m["question"][:120], "segment": m["segment"],
            "category": m["category"], "ref": m["ref"], "side": "primary" if best["side"] == 0 else "other",
            "outcome": best["outcome"], "bought_at": _r(best["ask"]), "fair": _r(best["fair"]),
            "edge": _r(best["edge"]), "z": _r(z, 2), "bin_samples": b["n"], "won": best["payoff"] >= 0.5,
            "net": _r(net), "cost": _r(best["cost"]), "stake": _r(stake)}


def summarise(bets: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(bets)
    if not n:
        return {"bets": 0, "days": 0, "win_rate": None, "avg_net": None, "ci95_net": None, "ci95_net_clustered": None,
                "return_on_cost": None, "total_net": 0.0, "bankroll_return": 0.0, "max_drawdown": 0.0,
                "verdict": verdict(0, 0, None)}
    nets = [b["net"] for b in bets]
    mean = statistics.fmean(nets)
    sd = statistics.stdev(nets) if n >= 2 else None
    ci = [_r(mean - 1.96 * sd / math.sqrt(n)), _r(mean + 1.96 * sd / math.sqrt(n))] if sd is not None else None
    days: dict[str, list[float]] = {}
    for b in bets:
        days.setdefault(b["ref"].strftime("%Y-%m-%d"), []).append(b["net"])
    d = len(days)
    clustered = None
    if d >= 2:
        # cluster-robust standard error of the per-bet mean, clusters = resolution days
        resid = [sum(v) - mean * len(v) for v in days.values()]
        se = math.sqrt(d / (d - 1) * sum(x * x for x in resid)) / n
        clustered = [_r(mean - 1.96 * se), _r(mean + 1.96 * se)]
    path, peak, drawdown = 0.0, 0.0, 0.0
    for b in sorted(bets, key=lambda x: (x["ref"], x["market_id"])):
        path += b["stake"] * b["net"] / b["cost"]
        peak = max(peak, path)
        drawdown = max(drawdown, peak - path)
    return {"bets": n, "days": d, "win_rate": _r(sum(b["won"] for b in bets) / n, 3), "avg_net": _r(mean),
            "ci95_net": ci, "ci95_net_clustered": clustered,
            "return_on_cost": _r(sum(nets) / sum(b["cost"] for b in bets)), "total_net": _r(sum(nets)),
            "bankroll_return": _r(path), "max_drawdown": _r(drawdown), "verdict": verdict(n, d, clustered)}


def verdict(bets: int, days: int, clustered: list[float] | None) -> str:
    if bets < MIN_BETS or days < MIN_DAYS or clustered is None:
        return f"INSUFFICIENT_DATA (needs >= {MIN_BETS} bets on >= {MIN_DAYS} resolution days; has {bets} on {days})"
    if clustered[1] < 0:
        return "NEGATIVE_OUT_OF_SAMPLE"
    if clustered[0] > 0:
        return "PASSES_OUT_OF_SAMPLE (assumed costs; paper trading must confirm)"
    return "INCONCLUSIVE"


def _band(x: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= x < hi:
            return name
    return BANDS[-1][0]


def _walk(markets: list[dict[str, Any]], s: BacktestSettings, half_spread: float | None = None,
          asks: dict[str, tuple[float, float]] | None = None
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(bets, per-fold info). Chunk 0 only trains. With `asks`, only markets that have
    recorded asks are bet, at those asks."""
    n = len(markets)
    size = n // (s.folds + 1)
    bets, folds = [], []
    if size < 1:
        return bets, folds
    for k in range(1, s.folds + 1):
        test = markets[k * size: (k + 1) * size if k < s.folds else n]
        cutoff = test[0]["t"]                      # the fold's first decision: train only on what had resolved
        train = [m for m in markets if m["ref"] < cutoff]
        info = {"fold": k, "test_from": min(m["ref"] for m in test).isoformat(timespec="seconds"),
                "test_to": max(m["ref"] for m in test).isoformat(timespec="seconds"), "test_markets": len(test),
                "train_markets": len(train), "train_until": cutoff.isoformat(timespec="seconds")}
        if len(train) < s.min_train_markets:
            folds.append({**info, "skipped": f"fewer than {s.min_train_markets} training markets", "bets": 0})
            continue
        tables = _tables(train)
        mine = []
        for m in test:
            if not s.min_price <= m["tokens"][0][0] <= s.max_price:
                continue
            if asks is not None and m["market_id"] not in asks:
                continue
            bet = decide(m, tables, s, half_spread, asks.get(m["market_id"]) if asks is not None else None)
            if bet is not None:
                mine.append({**bet, "fold": k})
        bets.extend(mine)
        st = summarise(mine)
        folds.append({**info, "in_band": sum(1 for m in test if s.min_price <= m["tokens"][0][0] <= s.max_price),
                      "bets": st["bets"], "win_rate": st["win_rate"], "avg_net": st["avg_net"]})
    return bets, folds


def run_backtest(store, settings: BacktestSettings | None = None, excluded: frozenset[str] | None = None,
                 sensitivity: bool = True) -> dict[str, Any]:
    s = settings or BacktestSettings()
    markets, dropped = load_markets(store, s.horizon_hours, excluded, s.anchor)
    in_band = [m for m in markets if s.min_price <= m["tokens"][0][0] <= s.max_price]
    bets, folds = _walk(markets, s)
    overall = summarise(bets)

    def split(key):
        groups: dict[str, list[dict[str, Any]]] = {}
        for b in bets:
            groups.setdefault(key(b), []).append(b)
        return {k: summarise(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}

    # in-sample: the tables have seen every market they then judge (what Phase 8 measured)
    all_tables = _tables(markets)
    size = len(markets) // (s.folds + 1)
    tested = markets[size:] if size else []
    insample = summarise([b for b in (decide(m, all_tables, s) for m in tested
                                      if s.min_price <= m["tokens"][0][0] <= s.max_price) if b is not None])
    grid = []
    for hs in (COST_GRID if sensitivity else ()):
        st = overall if hs == s.half_spread else summarise(_walk(markets, s, hs)[0])
        grid.append({"half_spread": hs, "bets": st["bets"], "win_rate": st["win_rate"], "avg_net": st["avg_net"],
                            "ci95_net_clustered": st["ci95_net_clustered"], "return_on_cost": st["return_on_cost"],
                            "verdict": st["verdict"]})
    cats: dict[str, int] = {}
    for m in in_band:
        cats[m["category"]] = cats.get(m["category"], 0) + 1
    sample = sorted(bets, key=lambda b: b["ref"])[-15:]
    return {
        "backtest_version": BACKTEST_VERSION, "settings": s.as_dict(), "anchor": s.anchor,
        "dataset": {"markets": len(markets), "in_price_band": len(in_band), "dropped": dropped,
                    "from": min(m["ref"] for m in markets).isoformat(timespec="seconds") if markets else None,
                    "to": max(m["ref"] for m in markets).isoformat(timespec="seconds") if markets else None,
                    "in_band_by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
                    "price_bins": [f"{BIN_EDGES[i]:.2f}-{BIN_EDGES[i + 1]:.2f}" for i in range(len(BIN_EDGES) - 1)]},
        "out_of_sample": overall, "in_sample": insample, "folds": folds,
        "by_category": split(lambda b: b["category"]), "by_segment": split(lambda b: b["segment"]),
        "by_bought_price": split(lambda b: _band(b["bought_at"])),
        "by_fold": split(lambda b: f"fold {b['fold']}"), "cost_sensitivity": grid,
        "recent_bets": [{**b, "ref": b["ref"].isoformat(timespec="seconds")} for b in sample],
        "note": ("Walk-forward: every bet was decided with tables built only from markets that had resolved "
                 "before its price was observed. Prices are anchored on the "
                 + ("SCHEDULED END DATE (what FRIDAY knows live). " if s.anchor == "end" else
                    "ACTUAL RESOLUTION (knows when a market resolved — leaky, for comparison only). ")
                 + "Asks are ASSUMED (recorded price + half-spread). The verdict "
                 "uses the day-clustered interval. Nothing here authorises any trade."),
    }


def render_text(r: dict[str, Any]) -> str:
    def c(x):
        return "   —  " if x is None else f"{x * 100:+6.2f}"

    def ci(x):
        return "—" if not x else f"{x[0] * 100:+.2f} … {x[1] * 100:+.2f}"

    def row(name, st):
        win = "—" if st["win_rate"] is None else f"{st['win_rate'] * 100:.0f}%"
        return (f"{name:<16} {st['bets']:>5} {st['days']:>4}  {win:>4} "
                f"{c(st['avg_net'])} {ci(st['ci95_net_clustered']):<18} {c(st['return_on_cost'])}  {st['verdict']}")

    d, s = r["dataset"], r["settings"]
    lines = [f"WALK-FORWARD BACKTEST {r['backtest_version']} — calibration edge, held to resolution · anchor: "
             + ("scheduled end date" if r.get("anchor") == "end" else "actual resolution (leaky)"),
             f"horizon {s['horizon_hours']:g}h · {s['folds']} folds · assumed half-spread {s['half_spread'] * 100:g}c · "
             f"price band {s['min_price']}-{s['max_price']} · min edge {s['edge']['min_edge'] * 100:g}c, z >= {s['edge']['min_z']:g}",
             f"dataset: {d['markets']} in-scope two-outcome markets ({d['from']} → {d['to']}), {d['in_price_band']} in the "
             f"price band; dropped {d['dropped']}",
             f"in the band by type: {d['in_band_by_category']}", "",
             f"{'':<16} {'bets':>5} {'days':>4}  {'win':>4} {'net c':>6} {'95% CI (clustered)':<18} {'on cost %':>6}  verdict",
             row("OUT OF SAMPLE", r["out_of_sample"]), row("in sample", r["in_sample"]), "", "by market type:"]
    lines += [row("  " + k, v) for k, v in r["by_category"].items()] or ["  (no bets)"]
    lines += ["", "by bought price:"] + ([row("  " + k, v) for k, v in r["by_bought_price"].items()] or ["  (no bets)"])
    lines += ["", "folds (train only on markets resolved before the fold's first price):"]
    for f in r["folds"]:
        lines.append(f"  fold {f['fold']}: test {f['test_from'][:10]} → {f['test_to'][:10]} ({f['test_markets']} markets), "
                     f"train {f['train_markets']} until {f['train_until'][:10]} · "
                     + (f["skipped"] if f.get("skipped") else
                        f"{f['bets']} bets of {f['in_band']} in band, win {f['win_rate']}, net {c(f['avg_net'])}c"))
    lines += ["", "cost sensitivity (assumed half-spread → out-of-sample result):"]
    for x in r["cost_sensitivity"]:
        lines.append(f"  {x['half_spread'] * 100:>4g}c: {x['bets']:>5} bets, net {c(x['avg_net'])}c, CI {ci(x['ci95_net_clustered'])}"
                     f"  → {x['verdict']}")
    lines += ["", r["note"]]
    return "\n".join(lines)
