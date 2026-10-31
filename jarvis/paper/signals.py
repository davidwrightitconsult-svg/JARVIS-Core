"""
FRIDAY signal trading (Phase 13C): the paper orders behind the strategies the validation gate
already allows.

Until now the gate could approve a strategy and nothing would trade it. The scorecard (7C) measured
every signal, the gate (9C) judged the evidence, and the only source that ever placed an order was
the Bollinger forward test (12B). This module closes that gap: it is the hands for the gate's own
judgement, and it originates nothing of its own.

THE BET IS THE ONE THAT WAS MEASURED. The scorecard scores a signal like this:

    UP    buy the token at the entry ask, sell at the exit bid when the horizon ends
    DOWN  the other side of a two-outcome market, the same way
    net   exit_bid - entry_ask        (there is no stop and there is no target)

So that is exactly what is placed here: entry at the recorded ask, exit at the signal's own expiry,
no stop and no target. A stop would be a different bet from the one the gate approved, and its paper
record would not be evidence for anything. Position size is the Risk Governor's job, and a long
position cannot lose more than its stake, which the governor already caps.

Two honest differences from the scorecard, both in the conservative direction:
  - DOWN is traded by BUYING the other outcome's token at ITS OWN recorded ask. The scorecard models
    that side as `1 - best_bid` of the signalled token. The real book is usually a little worse, so
    the paper record is the stricter of the two.
  - The engine refuses an entry whose spread is too wide, whose price is in the tails, or whose
    quote is stale. The scorecard scores those signals anyway. The paper record will therefore cover
    fewer signals than the scorecard did, never more.

Rules fixed here, in advance, before the first order:
  - only strategies the gate currently allows (APPROVED_FOR_PAPER or FORWARD_TEST)
  - only signals still inside their horizon, observed within `max_signal_age_minutes`
  - DOWN only in a market with exactly two outcomes; otherwise "the other side" is not defined
  - one paper order per signal, ever, across restarts
  - the position is held until the signal's own expiry, then closed at the bid
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .engine import Order, PaperEngine

SIGNALS_VERSION = "13C.1"
STAKE = 20.0                      # what each signal asks for; the Risk Governor decides the real size
MAX_SIGNAL_AGE_MINUTES = 30.0     # older than this and the entry is no longer the signal's entry
NO_STOP = 0.999                   # the engine requires a stop distance; this one can never be reached
MIN_HOLD_HOURS = 0.25


class SignalTrader:
    def __init__(self, engine: PaperEngine, store, gate_provider: Callable[[], dict[str, Any]] | None,
                 enabled: bool = True, stake: float = STAKE,
                 max_signal_age_minutes: float = MAX_SIGNAL_AGE_MINUTES,
                 clock: Callable[[], datetime] | None = None):
        self.engine, self.store, self.gate_provider = engine, store, gate_provider
        self.enabled, self.stake = bool(enabled), float(stake)
        self.max_signal_age_minutes = float(max_signal_age_minutes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last: dict[str, Any] | None = None

    # ------------------------------------------------------------ the gate

    def allowed_strategies(self) -> set[str]:
        """What the gate permits for PAPER right now. No gate, nothing trades (fail closed)."""
        if self.gate_provider is None:
            return set()
        try:
            gate = self.gate_provider()
        except Exception:                                   # noqa: BLE001 - fail closed, never guess
            return set()
        if not isinstance(gate, dict):
            return set()
        out: set[str] = set()
        for key in ("approved_for_paper", "forward_test"):
            v = gate.get(key, ())
            if isinstance(v, (list, tuple, set, frozenset)):
                out |= {x for x in v if isinstance(x, str)}
        return out

    # ------------------------------------------------------------ bookkeeping

    def _traded(self) -> set[int]:
        with self.engine._db() as db:
            return {r["signal_id"] for r in db.execute("SELECT signal_id FROM paper_signal_orders").fetchall()}

    def _record(self, sig: dict[str, Any], token_id: str, status: str,
                position_id: int | None, note: str) -> None:
        with self.engine._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO paper_signal_orders(signal_id, at, strategy, market_id, token_id,"
                " direction, status, position_id, note) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(sig["signal_id"]), self.clock().isoformat(timespec="seconds"), str(sig["strategy"])[:80],
                 str(sig["market_id"])[:80], str(token_id)[:120], str(sig["direction"])[:8],
                 status, position_id, str(note)[:200]))

    def _other_token(self, sig: dict[str, Any]) -> str | None:
        """The other outcome of a two-outcome market. None if the market is not binary."""
        reader = getattr(self.store, "tokens_for_markets", None)
        if reader is None:
            return None
        tokens = sorted(t for t, m in (reader([sig["market_id"]]) or {}).items() if str(m) == str(sig["market_id"]))
        if len(tokens) != 2 or sig["token_id"] not in tokens:
            return None
        return tokens[0] if tokens[1] == sig["token_id"] else tokens[1]

    # ------------------------------------------------------------ the run

    def on_run(self, *_: Any) -> dict[str, Any]:
        """After a market scan: place one paper order for each new signal the gate allows."""
        now = self.clock()
        out: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "version": SIGNALS_VERSION,
                               "allowed": [], "considered": 0, "placed": 0, "refused": 0, "skipped": 0,
                               "orders": []}
        if not self.enabled:
            out["inactive"] = "disabled"
            self.last = out
            return out
        allowed = self.allowed_strategies()
        out["allowed"] = sorted(allowed)
        if not allowed:
            out["inactive"] = "the gate allows no strategy for paper trading"
            self.last = out
            return out

        traded = self._traded()
        oldest = (now - timedelta(minutes=self.max_signal_age_minutes)).isoformat(timespec="seconds")
        for sig in self.store.signals(hours=24, open_only=True, limit=2000):
            if sig["strategy"] not in allowed or int(sig["signal_id"]) in traded:
                continue
            out["considered"] += 1
            if str(sig["observed_at"]) < oldest:
                out["skipped"] += 1
                self._record(sig, sig["token_id"], "SKIPPED", None, "signal older than the entry window")
                continue
            token = sig["token_id"] if sig["direction"] == "UP" else self._other_token(sig)
            if token is None:
                out["skipped"] += 1
                self._record(sig, sig["token_id"], "SKIPPED", None,
                             "DOWN needs a two-outcome market for the other side")
                continue
            hold = (datetime.fromisoformat(sig["expires_at"]) - now).total_seconds() / 3600.0
            if hold < MIN_HOLD_HOURS:
                out["skipped"] += 1
                self._record(sig, token, "SKIPPED", None, "the signal's horizon has almost run out")
                continue
            # No stop and no target: this is the bet the scorecard measured and the gate approved.
            result = self.engine.submit(Order(
                strategy=sig["strategy"], market_id=str(sig["market_id"]), token_id=token, stake=self.stake,
                stop=NO_STOP, target=None, max_hold_hours=hold,
                label=f"{sig['strategy']} {sig['direction']} · signal {sig['signal_id']}"))
            if result["status"] == "OPENED":
                out["placed"] += 1
                self._record(sig, token, "OPENED", result["position"]["position_id"], "")
                out["orders"].append({"signal_id": sig["signal_id"], "strategy": sig["strategy"],
                                      "direction": sig["direction"], "token_id": token,
                                      "position_id": result["position"]["position_id"]})
            else:
                out["refused"] += 1
                self._record(sig, token, "REFUSED", None, result.get("reason", ""))
        self.last = out
        return out

    def report(self, limit: int = 200) -> dict[str, Any]:
        with self.engine._db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM paper_signal_orders ORDER BY signal_id DESC LIMIT ?",
                (max(1, min(int(limit), 2000)),)).fetchall()]
            counts = {r["status"]: r["n"] for r in db.execute(
                "SELECT status, COUNT(*) n FROM paper_signal_orders GROUP BY status").fetchall()}
        return {"version": SIGNALS_VERSION, "enabled": self.enabled, "stake": self.stake,
                "allowed": sorted(self.allowed_strategies()), "counts": counts,
                "last_run": self.last, "recent": rows}
