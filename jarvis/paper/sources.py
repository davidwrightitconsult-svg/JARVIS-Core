"""
FRIDAY paper strategy sources (Phase 12B): the code that turns what FRIDAY
records into paper orders. A source can only ask; the engine and the Risk
Governor decide.

BOLLINGER_RESTING — a FORWARD TEST, declared before it has traded once.

Where it comes from: in Phase 11C the Bollinger Bands rule (Murphy ch. 9)
was the only one of eleven that made money before costs (+0.29 cents a
share) and it lost 1.71 cents after paying the spread twice. The hypothesis
is that the same signal is worth having if the entry earns the spread
instead of paying it: buy with an order that rests at the bid.

That hypothesis was formed AFTER seeing the Phase 11 result, so the backtest
is not evidence for it. Its only evidence is its own paper record, judged by
the rule below, which is fixed here before the first order:

  settings   the book's own: 20 hourly bars, 2 standard deviations (frozen)
  signal     the previous hour closed outside a band and the last hour closed
             back inside. Below the lower band: buy this outcome. Above the
             upper band: buy the opposite outcome.
  entry      a resting buy at the recorded bid, waiting up to 60 minutes
  target     the 20-bar average at the time of the signal (at least 1 cent away)
  stop       3 cents plus the spread at the time of the signal, at most 8 cents
  time limit 48 hours
  stake      asks for $20; the Risk Governor sizes it

  SUCCESS    at least 100 closed trades entered on at least 14 days, with the
             95% interval (clustered by day) of profit per share above zero
  FAILURE    the same count with the interval entirely below zero, OR
  LOSS LIMIT the test stops itself for good once it has lost $50 of paper money

Until one of those is reached the gate lists it as FORWARD_TEST.

PROMOTION (12C). SUCCESS does not approve anything by itself. The owner
promotes a test that has succeeded, with the literal word PROMOTE, from the
command line on the server. From then on the gate lists the strategy as
APPROVED_FOR_PAPER and the $50 test limit no longer applies (the Risk
Governor's limits still do). An approved strategy is re-judged on its whole
record every time: if the 95% interval falls entirely below zero, the
approval is revoked automatically and it stops. Paper only, always.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

from jarvis.market.bars import regular
from jarvis.strategies import LONG, REGISTRY, signal
from jarvis.strategies import indicators as ta

from .costs import round_trip
from .engine import Order, PaperEngine

SOURCES_VERSION = "12C.1"
PROMOTE_WORD = "PROMOTE"
NAME = "BOLLINGER_RESTING"
RULE = REGISTRY["BOOK_BOLLINGER"]
TTL_MINUTES = 60.0
STAKE = 20.0
MAX_HOLD_HOURS = 48.0
STOP_BASE, STOP_MAX, MIN_TARGET = 0.03, 0.08, 0.01
# 18B: a target must clear the round trip by this much, not just MIN_TARGET. The old 1c floor was
# below the cost of the trade it authorised -- a maker entry still pays the exit fee, which is about
# 1.25c a share at mid prices in a 0.05 category, so a 1c target was a guaranteed loss on average.
# Being a multiple of the MEASURED round trip means the floor adapts to where the trade is: cheap in
# the tails and in zero-fee categories, expensive in wide mid-priced crypto markets.
MIN_TARGET_MULTIPLE = 1.5
MIN_BARS, MIN_OBSERVED = 22, 18              # of the last 24 hours, at least 18 must have a recorded price
MIN_TRADES, MIN_DAYS, LOSS_LIMIT = 100, 14, 50.0
Z = 1.96


def verdict(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """The forward test's standing, by the rule fixed above."""
    n = len(trades)
    pnl = round(sum(t["pnl"] for t in trades), 2)
    days: dict[str, list[float]] = {}
    for t in trades:
        days.setdefault(str(t["opened_at"])[:10], []).append(t["per_share"])
    out = {"strategy": NAME, "trades": n, "days": len(days), "pnl": pnl, "avg_per_share": None, "interval": None,
           "needs": f"{MIN_TRADES} closed trades entered on {MIN_DAYS} days", "loss_limit": LOSS_LIMIT}
    if n:
        mean = sum(t["per_share"] for t in trades) / n
        out["avg_per_share"] = round(mean, 5)
        if len(days) >= 2:
            g = len(days)
            resid = [sum(v) - mean * len(v) for v in days.values()]
            se = math.sqrt(g / (g - 1) * sum(x * x for x in resid)) / n
            out["interval"] = [round(mean - Z * se, 5), round(mean + Z * se, 5)]
    if pnl <= -LOSS_LIMIT:
        out["status"], out["reason"] = "STOPPED", f"the forward test lost ${-pnl:.2f} of paper money (limit ${LOSS_LIMIT:g}) and has stopped for good"
    elif n >= MIN_TRADES and len(days) >= MIN_DAYS and out["interval"]:
        if out["interval"][0] > 0:
            out["status"], out["reason"] = "SUCCEEDED", "profit per share is above zero with 95% confidence on its paper record"
        elif out["interval"][1] < 0:
            out["status"], out["reason"] = "FAILED", "loses money on its paper record (95% interval entirely below zero)"
        else:
            out["status"], out["reason"] = "RUNNING", "enough trades, no clear result either way yet; the test continues"
    else:
        out["status"], out["reason"] = "RUNNING", f"forward test under way: {n} closed trades on {len(days)} days (needs {out['needs']})"
    return out


class BollingerResting:
    def __init__(self, engine: PaperEngine, store, enabled: bool = True, executor: Any = None,
                 clock: Callable[[], datetime] | None = None):
        self.engine, self.store, self.enabled = engine, store, bool(enabled)
        self.executor = executor                   # 16B: may this trade open now, at this size?
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._seen: dict[str, int] = {}            # token -> the last completed hour it was evaluated at
        self.last: dict[str, Any] | None = None

    def standing(self) -> dict[str, Any]:
        v = verdict([t for t in self.engine.trades(5000) if t["strategy"] == NAME])
        v["enabled"] = self.enabled
        promo = self.engine.promotion(NAME)
        v["promoted"] = bool(promo and not promo["revoked_at"])
        if v["promoted"]:
            if v["interval"] and v["interval"][1] < 0:           # the record has turned clearly negative
                self.engine.revoke_promotion(NAME, f"paper record fell below zero with 95% confidence after {v['trades']} trades")
                v["promoted"] = False
                v["status"], v["reason"] = "DEMOTED", "approved, then its paper record turned clearly negative; approval revoked"
            else:
                v["status"] = "APPROVED"
                v["reason"] = (f"promoted by the owner on {promo['promoted_at'][:10]} after its forward test succeeded; "
                               f"{v['trades']} paper trades so far")
        elif promo and promo["revoked_at"]:
            v["status"], v["reason"] = "DEMOTED", f"approval revoked on {promo['revoked_at'][:10]}: {promo['revoke_reason']}"
        v["active"] = self.enabled and v["status"] in ("RUNNING", "SUCCEEDED", "APPROVED")
        return v

    def promote(self, confirm: str, by: str = "owner") -> dict[str, Any]:
        """The owner approves a forward test that has succeeded. Needs the literal word PROMOTE."""
        if confirm != PROMOTE_WORD:
            raise PermissionError(f'promoting needs --confirm {PROMOTE_WORD}')
        v = self.standing()
        if v["status"] != "SUCCEEDED":
            raise ValueError(f"only a forward test that has SUCCEEDED can be promoted; this one is {v['status']}: {v['reason']}")
        import json
        self.engine.record_promotion(NAME, by, json.dumps({k: v[k] for k in ("trades", "days", "pnl", "avg_per_share", "interval")}))
        return self.standing()

    def revoke(self, reason: str = "revoked by the owner") -> dict[str, Any]:
        self.engine.revoke_promotion(NAME, reason)
        return self.standing()

    def gate_entry(self) -> dict[str, Any] | None:
        """What the validation gate should say about this test. A test that is switched off is not listed."""
        if not self.enabled:
            return None
        v = self.standing()
        status = ("APPROVED_FOR_PAPER" if v["status"] == "APPROVED" else
                  "FORWARD_TEST" if v["status"] in ("RUNNING", "SUCCEEDED") else "NOT_APPROVED")
        if v["status"] == "SUCCEEDED":
            v["reason"] += ". It is waiting for the owner's decision to promote it"
        return {"strategy": NAME, "status": status, "reason": v["reason"],
                "evidence": {"source": "its own paper record (12B forward test)", "forward": True,
                             "name": "Bollinger fade, resting orders", "standing": v["status"],
                             "promoted_by_owner": v["promoted"], **{k: v[k] for k in (
                                 "trades", "days", "pnl", "avg_per_share", "interval", "needs", "loss_limit")}}}

    def _closes(self, snapshots: list[dict[str, Any]], this_hour: int) -> list[float] | None:
        points = [(int(datetime.fromisoformat(r["observed_at"]).timestamp()), r["mid"]) for r in snapshots
                  if r.get("usable") and isinstance(r.get("mid"), (int, float))]
        series, _ = regular(points, this_hour - 1)                     # completed hours only
        if len(series) < MIN_BARS or series[-1][0] != this_hour - 1:
            return None
        recent = {ts // 3600 for ts, _ in points if this_hour - 24 <= ts // 3600 < this_hour}
        if len(recent) < MIN_OBSERVED:
            return None
        return [p for _, p in series]

    def on_run(self, *_: Any) -> dict[str, Any]:
        """After a market scan: look at each two-outcome market once per completed hour."""
        out = {"at": self.clock().isoformat(timespec="seconds"), "looked": 0, "signals": 0, "placed": 0,
               "refused": 0, "refused_uneconomic": 0}
        if not self.enabled or not self.standing()["active"]:
            out["inactive"] = True
            self.last = out
            return out
        this_hour = int(self.clock().timestamp()) // 3600
        by_market: dict[str, list[dict[str, Any]]] = {}
        for r in self.store.latest_rows():
            by_market.setdefault(str(r["market_id"]), []).append(r)
        pairs = {m: rows for m, rows in by_market.items() if len(rows) == 2 and all(
            x["usable"] and x["best_bid"] is not None and x["best_ask"] is not None for x in rows)}
        fresh = [m for m, rows in pairs.items() if self._seen.get(rows[0]["token_id"]) != this_hour]
        history = self.store.history_bulk([pairs[m][0]["token_id"] for m in fresh], hours=31)
        for m in fresh:
            first, other = pairs[m]
            self._seen[first["token_id"]] = this_hour
            closes = self._closes(history.get(first["token_id"], []), this_hour)
            if closes is None:
                continue
            out["looked"] += 1
            side = signal(RULE, closes)
            if not side:
                continue
            out["signals"] += 1
            middle = ta.sma(closes, RULE.params["period"])
            buy = first if side == LONG else other
            fair = middle if side == LONG else 1.0 - middle          # the average, seen from the outcome being bought
            bid, ask = buy["best_bid"], buy["best_ask"]
            target = fair - bid
            # The entry rests at the bid, so it is a maker fill: no spread crossed and no entry
            # fee. Getting OUT is still a taker fill, and that is what the target has to beat.
            floor = MIN_TARGET
            rt = round_trip(self.engine.settings.costs, bid, ask,
                            self.engine._fee_category(m), maker_entry=True)
            if rt is not None:
                floor = max(MIN_TARGET, round(MIN_TARGET_MULTIPLE * rt.total, 4))
            if target < floor:
                out["refused"] += 1
                out["refused_uneconomic"] += 1
                continue
            stop = min(STOP_MAX, STOP_BASE + max(0.0, ask - bid))
            stake = STAKE
            if self.executor is not None:
                v = self.executor.may_open(NAME, STAKE)
                if not v.allowed:
                    out["refused"] += 1
                    continue
                stake = min(STAKE, v.max_stake)
            r = self.engine.rest(Order(NAME, m, buy["token_id"], stake, round(stop, 4), round(target, 4), MAX_HOLD_HOURS,
                                       label=f"{first.get('question') or m} — {buy.get('outcome') or ''}".strip(" —")),
                                 bid, TTL_MINUTES)
            out["placed" if r["status"] == "RESTING" else "refused"] += 1
        if len(self._seen) > 5000:
            self._seen = {k: v for k, v in self._seen.items() if v == this_hour}
        self.last = out
        return out
