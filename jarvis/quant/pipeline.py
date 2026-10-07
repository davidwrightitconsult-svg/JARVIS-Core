"""
FRIDAY Quant pipeline (Phase 7B): runs after every stored market snapshot.

    stored run -> features -> universe -> watchlist -> strategies -> signals
               -> score due signals (7C) -> resolutions (hourly) -> daily scorecard note

- Wired as an observer of the market-data engine, so the existing Hermes
  MARKET_SCAN (every 15 min) drives it: no new job type, no new capability
- Eligible markets are (re)added to the watchlist, which keeps them observed
  even after they leave the top-volume ranking
- A strategy does not fire again for the same token and direction while
  its previous signal's horizon is still running (no double counting)
- Signals are hypotheses recorded with their inputs; nothing here can size,
  approve or place anything
- 7C: due signals are scored against the recorded order book; once an hour
  an INJECTED read-only resolver (the market-data client) is asked whether
  signalled markets have resolved; once a UTC day an injected note writer
  (the knowledge recorder) gets the scorecard. The Quant Core itself has no
  network path.
- 8C: when CALIBRATION_EDGE is enabled and a fair-value model is given,
  every EDGE estimate on an eligible market is recorded as a signal (once
  per market, on the side to buy, with every model input), so the 7C
  machinery scores it at its horizon AND held to resolution. This is how
  the 8B edge estimates are tested against what actually happened.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .engine import QuantEngine
from .scorecard import build_scorecard
from .scoring import Scorer
from .strategies import EDGE_STRATEGY, LIBRARY, STRATEGIES_VERSION, Strategy, evaluate_market

RESOLUTION_CHECK_SECONDS = 3600


class QuantPipeline:
    def __init__(self, engine: QuantEngine, strategies: tuple[Strategy, ...], record: bool = True,
                 resolver=None, note_writer=None, calibration_updater=None,
                 calibration_interval_hours: float = 3.0, fair_model=None):
        self.engine = engine
        self.strategies = strategies
        self.record = record
        self.scorer = Scorer(engine.store, record=record)
        self.resolver = resolver              # callable(market_ids) -> [{market_id, resolved, payoffs}]
        self.note_writer = note_writer        # callable(card, day) -> note link or None
        self.calibration_updater = calibration_updater   # callable() -> bool (starts a background backfill)
        self.calibration_interval_hours = float(calibration_interval_hours)
        self.fair_model = fair_model          # FairValueModel (8B) — drives CALIBRATION_EDGE (8C)
        self.runs = 0
        self.last: dict[str, Any] | None = None

    def evaluate(self, run_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Evaluate the latest run. With record=False nothing is written (preview)."""
        now = now or datetime.now(timezone.utc)
        store = self.engine.store
        markets = self.engine.features(now=now)
        eligible = [m for m in markets if m.eligible]
        candidates = [sig for m in eligible for sig in evaluate_market(m, self.strategies, run_id, now)]
        open_keys = store.open_signal_keys(now)
        fresh = [s for s in candidates if (s["strategy"], s["token_id"], s["direction"]) not in open_keys]
        fresh += self._edge_signals(eligible, run_id, now)
        if self.record:
            store.watch([m.market_id for m in eligible], now)
            store.record_signals(fresh)
        resolutions = self._check_resolutions(now) if self.record else None
        scoring = self.scorer.score_due(now)
        scoring.pop("outcomes", None)
        if self.record and resolutions and resolutions.get("resolved"):
            scoring["resolution_updates"] = self.scorer.apply_resolutions()
        calibration = self._calibration(now) if self.record else None
        note = self._daily_note(now) if self.record else None
        summary = {
            "strategies_version": STRATEGIES_VERSION, "run_id": run_id,
            "markets": len(markets), "eligible": len(eligible),
            "signals_new": len(fresh), "suppressed_open": len(candidates) - len(fresh),
            "by_strategy": dict(Counter(s["strategy"] for s in fresh)),
            "recorded": self.record, "scoring": scoring,
            **({"resolutions": resolutions} if resolutions else {}),
            **({"scorecard_note": note} if note else {}),
            **({"calibration_backfill": calibration} if calibration else {}),
        }
        if self.record:
            self.runs += 1
            self.last = {**summary, "at": now.isoformat(timespec="seconds")}
        return {**summary, "signals": fresh} if not self.record else summary

    def _edge_signals(self, eligible, run_id: int | None, now: datetime) -> list[dict[str, Any]]:
        """8C: the fair-value model's EDGE estimates, recorded as CALIBRATION_EDGE signals."""
        if self.fair_model is None or not any(s.name == EDGE_STRATEGY for s in self.strategies):
            return []
        spec = next(s for s in LIBRARY if s.name == EDGE_STRATEGY)
        done = self.engine.store.signalled_markets(EDGE_STRATEGY)
        out = []
        for m in eligible:
            if m.market_id in done:
                continue                                  # once per market: no re-counting one bet
            e = self.fair_model.evaluate(m)
            if e is None or e["status"] != "EDGE":
                continue
            o = next((t for t in m.outcomes if t.token_id == e["best_token_id"]), None)
            side = next((x for x in e["sides"] if x["token_id"] == e["best_token_id"]), None)
            if o is None or side is None or o.best_ask is None or o.best_bid is None:
                continue
            h = float(e["horizon_hours"])
            out.append({
                "run_id": run_id, "observed_at": o.observed_at, "strategy": EDGE_STRATEGY,
                "strategy_version": spec.version, "market_id": m.market_id, "token_id": o.token_id,
                "outcome": o.outcome, "direction": "UP",
                "strength": round(max(0.0, min(1.0, side["edge"] / 0.10)), 3), "horizon_hours": h,
                "expires_at": (now + timedelta(hours=h)).isoformat(timespec="seconds"),
                "mid": o.mid, "best_bid": o.best_bid, "best_ask": o.best_ask, "half_spread": o.half_spread,
                "inputs": {"fair": side["fair"], "ask": side["ask"], "edge": side["edge"], "z": side["z"],
                           "z_statistical": side["z_statistical"], "segment": e["segment"], "bin": e["bin"],
                           "bin_samples": e["bin_samples"], "bin_bias": e["bin_bias"], "weight": e["weight"],
                           "stake_fraction": e["stake_fraction"], "fair_version": e["fair_version"],
                           "primary_price": e["price"], "hours_to_end": m.hours_to_end,
                           "question": m.question[:120], "attention": m.attention},
            })
        return out

    def _check_resolutions(self, now: datetime) -> dict[str, Any] | None:
        store = self.engine.store
        last = store.meta_get("resolution_check_at")
        if self.resolver is None or (last and (now - datetime.fromisoformat(last)).total_seconds()
                                     < RESOLUTION_CHECK_SECONDS):
            return None
        store.meta_set("resolution_check_at", now.isoformat(timespec="seconds"))
        ids = store.unresolved_signal_markets(now)
        if not ids:
            return {"checked": 0, "resolved": 0}
        results = self.resolver(ids)
        resolved = [r for r in results if r.get("resolved") and r.get("payoffs")]
        for r in resolved:
            store.record_resolution(r["market_id"], r["payoffs"], "gamma", now)
        return {"checked": len(ids), "resolved": len(resolved),
                "errors": sum(1 for r in results if r.get("error"))}

    def _calibration(self, now: datetime) -> str | None:
        """8A: start a background calibration backfill every few hours."""
        store = self.engine.store
        last = store.meta_get("calibration_backfill_at")
        if self.calibration_updater is None or (last and (now - datetime.fromisoformat(last)).total_seconds()
                                                < self.calibration_interval_hours * 3600):
            return None
        store.meta_set("calibration_backfill_at", now.isoformat(timespec="seconds"))
        return "started" if self.calibration_updater() else "already running"

    def _daily_note(self, now: datetime) -> str | None:
        store = self.engine.store
        day = now.strftime("%Y-%m-%d")
        if self.note_writer is None or store.meta_get("scorecard_note_day") == day:
            return None
        store.meta_set("scorecard_note_day", day)
        return self.note_writer(build_scorecard(store), day)

    # market-data observer signature
    def on_run(self, run_id: int) -> dict[str, Any]:
        return self.evaluate(run_id)

    def status(self) -> dict[str, Any]:
        return {"strategies_version": STRATEGIES_VERSION, "strategies": [s.name for s in self.strategies],
                "runs": self.runs, "last": self.last}
