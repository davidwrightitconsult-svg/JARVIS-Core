"""
FRIDAY execution authority (Phase 16B): may this strategy open a trade right now, at this size?

There were two authorities and a gap between them.

    the 9C gate         may this strategy EVER trade?      slow, once, on 100 signals over 7+ days
    the Risk Governor   how much, given the portfolio?     per order, on exposure and drawdown

Nothing asked the question in between. A strategy could pass the gate in October, decay through
November, and keep trading on a promotion it had long stopped deserving — because the gate is a
promotion event, not a continuous check, and the Governor judges the portfolio rather than the
strategy. Worse, each paper source carried its own `enabled` flag and its own rules, so there was
no single place to turn one off or to find out why nothing was trading.

This is that place:

    the Executor        may it trade NOW, at THIS size?    per order, on the live edge report

Three authorities, three distinct questions, and each can only say no. The Executor originates
nothing, sizes nothing upward, and cannot approve what the gate has not allowed. It can only
withhold permission the gate already gave, which is the one thing that was missing.

### What it checks, in order, failing closed at every step
1. **The master switch.** Off by default. Nothing trades until the owner turns it on.
2. **The per-strategy allow list**, when one is set. Naming strategies explicitly is how a single
   suspect strategy is stopped without stopping the platform.
3. **The gate.** `APPROVED_FOR_PAPER` trades at full size; `FORWARD_TEST` is capped small, because
   a strategy still gathering evidence should not be able to lose much while it gathers it. Anything
   else, or no gate at all, trades nothing.
4. **Mixed strategy versions.** A strategy whose scored signals come from more than one version is
   not one strategy, and its confidence interval is about nothing. Refused until the record is
   rebuilt under a single version.
5. **The live edge, after fees.** If the 16A report's recent window says the whole interval is
   below zero, the strategy is losing money now whatever the gate decided then. Refused. This is
   the decay check the system did not have.
6. **The measured capacity ceiling.** A stake above `capacity_fraction` of the largest size the
   edge actually survived (14B) is cut to it, not refused: the trade is fine, the size is not.

### What it deliberately does not do
It never raises a stake, never overrides a gate refusal, and never decides direction or market.
It holds no opinion about any market. Everything it knows comes from the gate and from FRIDAY's own
scored record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

EXECUTOR_VERSION = "16B.1"

APPROVED_FOR_PAPER = "APPROVED_FOR_PAPER"
FORWARD_TEST = "FORWARD_TEST"
LOSING_VERDICTS = ("NEGATIVE_AFTER_COSTS",)


@dataclass(frozen=True)
class ExecutionSettings:
    # OFF by default. The platform can be fully built, fully wired and still not trading.
    enabled: bool = False
    # Empty means "every strategy the gate allows". A non-empty set is an explicit allow list.
    strategies: frozenset[str] = frozenset()
    # A strategy still in FORWARD_TEST may trade, but only this much per position.
    forward_test_stake: float = 10.0
    # Below this many scored signals the edge report cannot veto: there is nothing to judge yet.
    min_scored_to_judge: int = 30
    # Stop a strategy whose recent record is conclusively negative, even if the gate approved it.
    stop_on_negative_edge: bool = True
    # Refuse a strategy whose scored signals span more than one version of itself.
    stop_on_mixed_versions: bool = True
    # Never stake more than this fraction of the largest size the edge was shown to survive.
    capacity_fraction: float = 0.5
    # How long an edge report may be reused before it is rebuilt (it is a scan of the outcomes).
    cache_seconds: float = 300.0


@dataclass(frozen=True)
class Verdict:
    strategy: str
    allowed: bool
    max_stake: float
    reason: str
    checks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    gate_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy, "allowed": self.allowed,
                "max_stake": round(self.max_stake, 6), "reason": self.reason,
                "gate_status": self.gate_status, "checks": list(self.checks)}


def _no(strategy: str, reason: str, checks: list[dict[str, Any]], gate: str | None = None) -> Verdict:
    return Verdict(strategy=strategy, allowed=False, max_stake=0.0, reason=reason,
                   checks=tuple(checks), gate_status=gate)


class Executor:
    """Asked by every paper source before it proposes a position to the Risk Governor."""

    def __init__(self, gate_provider: Callable[[], dict[str, Any]] | None,
                 edge_provider: Callable[[], dict[str, Any]] | None = None,
                 capacity_provider: Callable[[], dict[str, Any]] | None = None,
                 settings: ExecutionSettings | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.gate_provider = gate_provider
        self.edge_provider = edge_provider
        self.capacity_provider = capacity_provider
        self.settings = settings or ExecutionSettings()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.verdicts: list[dict[str, Any]] = []      # a ring of the most recent decisions
        self.counts: dict[str, int] = {}

    # ------------------------------------------------------------ inputs
    def _cached(self, key: str, provider: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
        """A provider result reused for `cache_seconds`. A provider that raises yields {}, which
        every caller below treats as "cannot judge" rather than as permission."""
        if provider is None:
            return {}
        now = self.clock().timestamp()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.settings.cache_seconds:
            return hit[1]
        try:
            value = provider()
        except Exception:                                   # noqa: BLE001 - fail closed, never guess
            value = {}
        value = value if isinstance(value, dict) else {}
        self._cache[key] = (now, value)
        return value

    def gate_status(self, strategy: str) -> str | None:
        """APPROVED_FOR_PAPER, FORWARD_TEST, or None. No gate means None: nothing trades."""
        gate = self._cached("gate", self.gate_provider)
        for key, status in (("approved_for_paper", APPROVED_FOR_PAPER), ("forward_test", FORWARD_TEST)):
            v = gate.get(key, ())
            if isinstance(v, (list, tuple, set, frozenset)) and strategy in v:
                return status
        return None

    def edge_of(self, strategy: str) -> dict[str, Any]:
        report = self._cached("edge", self.edge_provider)
        strategies = report.get("strategies")
        row = strategies.get(strategy) if isinstance(strategies, dict) else None
        return row if isinstance(row, dict) else {}

    def capacity_of(self, strategy: str) -> float | None:
        """The largest stake the edge was shown to survive (14B), or None when unmeasured."""
        report = self._cached("capacity", self.capacity_provider)
        strategies = report.get("strategies")
        row = strategies.get(strategy) if isinstance(strategies, dict) else None
        if not isinstance(row, dict):
            return None
        ceiling = row.get("max_profitable_stake")
        return float(ceiling) if isinstance(ceiling, (int, float)) and ceiling > 0 else None

    # ------------------------------------------------------------ the decision
    def may_open(self, strategy: str, proposed_stake: float, record: bool = True) -> Verdict:
        s = self.settings
        self._recording = record
        checks: list[dict[str, Any]] = []
        strategy = str(strategy)

        def note(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})

        if not s.enabled:
            note("master_switch", False, "execution is off")
            return self._record(_no(strategy, "execution is off (JARVIS_EXECUTE is not on)", checks))
        note("master_switch", True, "on")

        if s.strategies and strategy not in s.strategies:
            note("allow_list", False, f"{strategy} is not in the allow list")
            return self._record(_no(strategy, f"{strategy} is not in the execution allow list", checks))
        note("allow_list", True, "allowed" if s.strategies else "no allow list set")

        gate = self.gate_status(strategy)
        if gate is None:
            note("gate", False, "the gate does not allow this strategy")
            return self._record(_no(strategy, f"the validation gate does not allow {strategy}", checks))
        note("gate", True, gate)

        if not (isinstance(proposed_stake, (int, float)) and proposed_stake > 0):
            note("stake", False, f"{proposed_stake!r} is not a positive stake")
            return self._record(_no(strategy, "the proposed stake must be a positive number", checks, gate))
        cap = float(proposed_stake)

        if gate == FORWARD_TEST and cap > s.forward_test_stake:
            cap = s.forward_test_stake
            note("forward_test_cap", True, f"capped at {cap:g}: still gathering evidence")

        edge = self.edge_of(strategy)
        scored = int(edge.get("scored") or 0)
        if s.stop_on_mixed_versions and edge.get("mixed_versions"):
            note("versions", False, f"scored signals span {edge.get('versions')}")
            return self._record(_no(strategy, (f"{strategy} has scored signals from more than one version "
                                               f"of itself ({edge.get('versions')}): that is not one "
                                               f"strategy and its interval means nothing"), checks, gate))
        note("versions", True, str(edge.get("versions") or "unknown"))

        if s.stop_on_negative_edge and scored >= s.min_scored_to_judge:
            verdict = str(edge.get("verdict") or "")
            money = edge.get("net_after_fees") or {}
            ci = money.get("ci95")
            if verdict in LOSING_VERDICTS or (isinstance(ci, list) and len(ci) == 2 and ci[1] < 0):
                note("live_edge", False, f"{verdict} over {scored} scored signals, ci95 {ci}")
                return self._record(_no(strategy, (f"{strategy} is losing money now: {scored} scored "
                                                   f"signals put the whole interval below zero ({ci}). "
                                                   f"The gate approved it; the record no longer does."),
                                        checks, gate))
            note("live_edge", True, f"{verdict or 'no verdict yet'} over {scored} scored signals")
        else:
            note("live_edge", True, f"{scored} scored signals: too few to judge, not a reason to refuse")

        ceiling = self.capacity_of(strategy)
        if ceiling is not None:
            allowed_by_capacity = ceiling * s.capacity_fraction
            if cap > allowed_by_capacity:
                cap = allowed_by_capacity
                note("capacity", True, f"cut to {cap:g}: {s.capacity_fraction:g} of the ${ceiling:g} "
                                       f"the edge was shown to survive")
            else:
                note("capacity", True, f"within the measured ${ceiling:g} ceiling")
        else:
            note("capacity", True, "no measured ceiling yet")

        if cap <= 0:
            return self._record(_no(strategy, "nothing left to stake after the caps", checks, gate))
        return self._record(Verdict(strategy=strategy, allowed=True, max_stake=cap,
                                    reason=f"allowed up to {cap:g} ({gate})",
                                    checks=tuple(checks), gate_status=gate))

    # ------------------------------------------------------------ reporting
    def _record(self, verdict: Verdict) -> Verdict:
        # report() previews every strategy; those previews must not be counted as real decisions.
        if not getattr(self, "_recording", True):
            return verdict
        key = f"{verdict.strategy}:{'allowed' if verdict.allowed else verdict.reason[:60]}"
        self.counts[key] = self.counts.get(key, 0) + 1
        self.verdicts.append({"at": self.clock().isoformat(timespec="seconds"), **verdict.as_dict()})
        del self.verdicts[:-200]
        return verdict

    def report(self) -> dict[str, Any]:
        """Why is nothing trading? This is where the answer lives."""
        gate = self._cached("gate", self.gate_provider)
        edge = self._cached("edge", self.edge_provider)
        allowed = sorted({x for key in ("approved_for_paper", "forward_test")
                          for x in (gate.get(key) or ()) if isinstance(x, str)})
        return {"version": EXECUTOR_VERSION, "enabled": self.settings.enabled,
                "allow_list": sorted(self.settings.strategies) or None,
                "gate_allows": allowed,
                "forward_test_stake": self.settings.forward_test_stake,
                "capacity_fraction": self.settings.capacity_fraction,
                "strategies": {name: self.may_open(name, 1e9, record=False).as_dict() for name in allowed},
                "edge_verdicts": {k: v.get("verdict") for k, v in
                                  (edge.get("strategies") or {}).items()},
                "decisions": dict(sorted(self.counts.items(), key=lambda kv: -kv[1])[:20]),
                "recent": self.verdicts[-10:]}
