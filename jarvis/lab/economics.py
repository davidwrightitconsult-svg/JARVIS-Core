"""
FRIDAY Validation Lab — venue economics (Phase 18C): where is it cheapest to trade?

Every strategy has to beat the round trip before it beats anything. That cost is not a constant:
across the markets FRIDAY actually records it varies by more than a factor of five, because it is
driven by three things that all move:

    the spread      paid once, in at the ask and out at the bid
    the fee rate    0 (geopolitics) to 0.07 (crypto), by category
    the price       the fee is rate x p x (1 - p), so it collapses in the tails

The consequence is that **the same prediction is profitable in one market and a loss in another**.
A 2c edge is real money on a 0.95 contract in a zero-fee category and a guaranteed loss on a
mid-priced crypto contract with a 6c spread. Phase 17 found the same thing from the other side when
an oracle that could see the future still lost money.

So this reports, over the recorded universe:
  - the round trip by fee category, and by price band inside it
  - the same figure as a fraction of the capital committed, which is what compares across prices
  - what a maker entry would save, since resting at the bid skips the spread AND the entry fee
  - the cheapest and dearest individual markets

It is a map of where to point a strategy. It says nothing about whether any strategy works.
"""

from __future__ import annotations

import statistics
from typing import Any

from jarvis.market.segments import category_of, segment_of
from jarvis.paper.costs import CostSettings, round_trip

ECONOMICS_VERSION = "18C.1"
BANDS = (("0.01-0.10", 0.01, 0.10), ("0.10-0.30", 0.10, 0.30), ("0.30-0.70", 0.30, 0.70),
         ("0.70-0.90", 0.70, 0.90), ("0.90-0.99", 0.90, 1.0))


def _band(price: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= price < hi:
            return name
    return "outside"


def _category(question: str | None, neg_risk: Any) -> str:
    if not question:
        return "unknown"
    if segment_of(question, neg_risk) == "sports":
        return "sports"
    return category_of(question)


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"markets": 0}
    totals = [r["taker"].total for r in rows]
    fractions = [r["taker"].fraction_of_cost for r in rows]
    maker = [r["maker"].total for r in rows]
    saved = [r["taker"].total - r["maker"].total for r in rows]
    return {
        "markets": len(rows),
        "median_spread": round(statistics.median(r["taker"].spread for r in rows), 4),
        "median_round_trip": round(statistics.median(totals), 5),
        "median_fraction_of_cost": round(statistics.median(fractions), 4),
        "worst_fraction_of_cost": round(max(fractions), 4),
        "best_fraction_of_cost": round(min(fractions), 4),
        "median_round_trip_as_maker": round(statistics.median(maker), 5),
        "median_saved_by_resting": round(statistics.median(saved), 5),
        # The edge a strategy must find here just to break even, in cents a share.
        "breakeven_edge_cents": round(statistics.median(totals) * 100, 2),
    }


def report(store, max_age_minutes: float = 120.0, costs: CostSettings | None = None,
           limit: int = 4000) -> dict[str, Any]:
    """Reads the latest recorded quote of every token FRIDAY is tracking."""
    settings = costs or CostSettings()
    try:
        rows = store.latest_rows()
    except Exception:                                          # noqa: BLE001
        rows = []
    priced: list[dict[str, Any]] = []
    for r in rows[:limit]:
        bid, ask = r.get("best_bid"), r.get("best_ask")
        category = _category(r.get("question"), r.get("neg_risk"))
        taker = round_trip(settings, bid, ask, category)
        maker = round_trip(settings, bid, ask, category, maker_entry=True)
        if taker is None or maker is None:
            continue
        priced.append({"market_id": r.get("market_id"), "token_id": r.get("token_id"),
                       "question": (r.get("question") or "")[:80], "outcome": r.get("outcome"),
                       "bid": bid, "ask": ask, "category": category, "band": _band(ask),
                       "taker": taker, "maker": maker})

    by_category: dict[str, Any] = {}
    for cat in sorted({p["category"] for p in priced}):
        group = [p for p in priced if p["category"] == cat]
        by_category[cat] = {**_summarise(group), "rate": settings.rate_for(cat),
                            "by_band": {b: _summarise([p for p in group if p["band"] == b])
                                        for b in sorted({p["band"] for p in group})}}

    cheapest = sorted(priced, key=lambda p: p["taker"].fraction_of_cost)[:10]
    dearest = sorted(priced, key=lambda p: -p["taker"].fraction_of_cost)[:10]

    def row(p: dict[str, Any]) -> dict[str, Any]:
        return {"market_id": p["market_id"], "outcome": p["outcome"], "question": p["question"],
                "bid": p["bid"], "ask": p["ask"], "category": p["category"],
                "round_trip": p["taker"].as_dict(), "as_maker": p["maker"].as_dict()}

    return {"economics_version": ECONOMICS_VERSION, "tokens": len(priced),
            "fees": {"enabled": settings.enabled, "multiplier": settings.fee_multiplier},
            "overall": _summarise(priced), "by_category": by_category,
            "cheapest": [row(p) for p in cheapest], "dearest": [row(p) for p in dearest]}


def render(r: dict[str, Any]) -> str:
    L = [f"VENUE ECONOMICS {r['economics_version']} — what a round trip costs, where",
         f"{r['tokens']} tokens priced · fees {'on' if r['fees']['enabled'] else 'OFF'}"
         f" (x{r['fees']['multiplier']:g})", ""]
    if not r["tokens"]:
        return "\n".join(L + ["  no recorded two-sided quotes yet"])
    o = r["overall"]
    L += [f"  OVERALL  median round trip {o['median_round_trip']:.4f} a share "
          f"= {o['median_fraction_of_cost'] * 100:.1f}% of capital "
          f"(best {o['best_fraction_of_cost'] * 100:.1f}%, worst {o['worst_fraction_of_cost'] * 100:.1f}%)",
          f"           a strategy must find {o['breakeven_edge_cents']:.2f}c a share here just to break even",
          f"           resting at the bid instead of crossing would save "
          f"{o['median_saved_by_resting']:.4f} a share "
          f"({o['median_round_trip_as_maker']:.4f} round trip as a maker)", ""]
    L.append(f"  {'CATEGORY':<14}{'rate':>6}{'mkts':>6}{'spread':>9}{'round trip':>12}"
             f"{'% capital':>11}{'as maker':>10}")
    for cat, v in sorted(r["by_category"].items(), key=lambda kv: kv[1].get("median_fraction_of_cost") or 9):
        if not v["markets"]:
            continue
        L.append(f"  {cat:<14}{v['rate']:>6.2f}{v['markets']:>6}{v['median_spread']:>9.3f}"
                 f"{v['median_round_trip']:>12.4f}{v['median_fraction_of_cost'] * 100:>10.1f}%"
                 f"{v['median_round_trip_as_maker']:>10.4f}")
    L.append("")
    L.append("  CHEAPEST MARKETS TO TRADE")
    for p in r["cheapest"][:5]:
        rt = p["round_trip"]
        L.append(f"    {rt['fraction_of_cost'] * 100:5.1f}%  {p['bid']:.2f}/{p['ask']:.2f} "
                 f"{p['category']:<12} {p['question'][:48]} ({p['outcome']})")
    L.append("")
    L.append("  DEAREST — an edge here has to be enormous")
    for p in r["dearest"][:5]:
        rt = p["round_trip"]
        L.append(f"    {rt['fraction_of_cost'] * 100:5.1f}%  {p['bid']:.2f}/{p['ask']:.2f} "
                 f"{p['category']:<12} {p['question'][:48]} ({p['outcome']})")
    L += ["", "  The same prediction is profitable in one of these markets and a loss in another.",
          "  This is a map of where to point a strategy, not evidence that one works."]
    return "\n".join(L)
