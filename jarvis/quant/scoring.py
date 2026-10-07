"""
FRIDAY Quant Core — signal outcome scoring (Phase 7C).

When a signal's horizon ends, FRIDAY looks up the token's first USABLE
recorded observation at or after the expiry (within a tolerance window) and
scores the signal against it. Nothing is estimated: every number comes from
the recorded order book, and the scoring is deterministic and reproducible.

Two measures per signal (prices are $ per $1-payoff share):
  mid_move  direction x (exit mid - entry mid). Did the price move the
            predicted way? Ignores costs.
  net       what a position would actually have made, crossing the spread
            at entry AND exit:
              UP   buy the token at the entry ask, sell at the exit bid
                   net = exit_bid - entry_ask
              DOWN the opposite side (e.g. buying NO at 1 - YES bid)
                   net = entry_bid - exit_ask
  cost      capital committed per share: UP entry_ask, DOWN 1 - entry_bid
  return_on_cost = net / cost (a 1c gain on a 3c long shot is 33%)

Resolution: once a market resolves (payoff 1 or 0 per token), every signal
on it also gets `resolution_net` — the executable result had the position
been held to the end:
  UP  payoff - entry_ask        DOWN  entry_bid - payoff
If the market resolved before a signal could be scored at its horizon, the
payoff is the exit price (status RESOLVED_BEFORE_EXIT).

A signal with no usable observation in its exit window (e.g. the market was
dropped) is NO_EXIT_DATA: counted, never guessed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

SCORING_VERSION = "7C.1"


def exit_window(expires_at: str, horizon_hours: float) -> tuple[str, str]:
    start = datetime.fromisoformat(expires_at)
    tolerance = timedelta(hours=max(1.0, 0.5 * float(horizon_hours)))
    return start.isoformat(timespec="seconds"), (start + tolerance).isoformat(timespec="seconds")


def _r(x: float | None, d: int = 4) -> float | None:
    return None if x is None else round(x, d)


def cost_of(sig: dict[str, Any]) -> float | None:
    if sig["direction"] == "UP":
        return sig["best_ask"]
    return None if sig["best_bid"] is None else 1 - sig["best_bid"]


def score_against(sig: dict[str, Any], exit_mid: float, exit_bid: float, exit_ask: float) -> dict[str, Any]:
    sign = 1 if sig["direction"] == "UP" else -1
    if sig["direction"] == "UP":
        net = exit_bid - sig["best_ask"]
    else:
        net = sig["best_bid"] - exit_ask
    cost = cost_of(sig)
    return {
        "exit_mid": exit_mid, "exit_bid": exit_bid, "exit_ask": exit_ask,
        "mid_move": _r(sign * (exit_mid - sig["mid"])), "net": _r(net), "cost": _r(cost),
        "return_on_cost": _r(net / cost) if cost else None,
    }


def resolution_net(sig: dict[str, Any], payoff: float) -> float:
    if sig["direction"] == "UP":
        return _r(payoff - sig["best_ask"])
    return _r(sig["best_bid"] - payoff)


class Scorer:
    """Scores due signals from the store. record=False previews only."""

    def __init__(self, store, record: bool = True):
        self.store = store
        self.record = record

    def score_due(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        due = self.store.due_signals(now)
        resolved = self.store.resolutions(list({s["market_id"] for s in due}))
        outcomes, pending = [], 0
        for sig in due:
            base = {"signal_id": sig["signal_id"], "scored_at": stamp}
            if sig["mid"] is None or sig["best_bid"] is None or sig["best_ask"] is None:
                outcomes.append({**base, "status": "NO_EXIT_DATA"})
                continue
            start, end = exit_window(sig["expires_at"], sig["horizon_hours"])
            snap = self.store.exit_snapshot(sig["token_id"], start, end)
            res = resolved.get(sig["market_id"])
            payoff = res["payoffs"].get(sig["token_id"]) if res else None
            if snap is not None:
                outcomes.append({**base, "status": "SCORED", "exit_observed_at": snap["observed_at"],
                                 **score_against(sig, snap["mid"], snap["best_bid"], snap["best_ask"]),
                                 **self._resolution(sig, res, payoff)})
            elif payoff is not None:
                outcomes.append({**base, "status": "RESOLVED_BEFORE_EXIT", "exit_observed_at": res["detected_at"],
                                 **score_against(sig, payoff, payoff, payoff), **self._resolution(sig, res, payoff)})
            elif stamp > end:
                outcomes.append({**base, "status": "NO_EXIT_DATA"})
            else:
                pending += 1                     # exit window still open: try again next run
        if self.record and outcomes:
            self.store.record_outcomes(outcomes)
        by_status: dict[str, int] = {}
        for o in outcomes:
            by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        summary = {"scoring_version": SCORING_VERSION, "due": len(due), "scored": len(outcomes),
                   "by_status": by_status, "waiting_for_exit": pending, "recorded": self.record}
        return summary if self.record else {**summary, "outcomes": outcomes}

    @staticmethod
    def _resolution(sig, res, payoff) -> dict[str, Any]:
        if payoff is None:
            return {}
        return {"resolution_value": float(payoff), "resolution_net": resolution_net(sig, float(payoff)),
                "resolved_at": res["detected_at"]}

    def apply_resolutions(self) -> int:
        """Add resolution results to already-scored signals of resolved markets."""
        resolved = self.store.resolutions()
        if not resolved:
            return 0
        updates = []
        for row in self.store.outcomes(days=3650):
            res = resolved.get(row["market_id"])
            if not res or row["status"] is None or row["resolution_value"] is not None:
                continue
            payoff = res["payoffs"].get(row["token_id"])
            if payoff is None or row["best_bid"] is None or row["best_ask"] is None:
                continue
            updates.append({k: row.get(k) for k in self.store.OUTCOME_FIELDS}
                           | {"resolution_value": float(payoff), "resolved_at": res["detected_at"],
                              "resolution_net": resolution_net(row, float(payoff))})
        if self.record and updates:
            self.store.record_outcomes(updates)
        return len(updates)
