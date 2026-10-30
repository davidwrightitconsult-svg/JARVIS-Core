"""
FRIDAY Risk Governor (Phase 10A). Survival first.

Every proposed position — from any strategy, in any market — must pass the
governor before the paper engine (Phase 12) may open it. The governor can
only say no, or say "less". It cannot originate, enlarge or place anything,
and it has no network path.

A proposal is checked in this order; the first failure rejects it:

  1. LIVE is locked      a proposal with mode LIVE is always rejected. Live
                         trading needs the Phase 17 audit and the owner.
  2. kill switch         engaged by the owner (or an operator): nothing new.
  3. validation gate     the strategy must be APPROVED_FOR_PAPER or a
                         FORWARD_TEST in the Phase 9C gate. If the gate
                         cannot be read, the answer is no (fail closed).
  4. sanity              a real strategy and market, a positive finite
                         stake, a positive equity.
  5. drawdown halt       equity fell max_drawdown below its peak: halted
                         until the owner resumes (persisted, survives a
                         restart).
  6. daily loss limit    equity fell daily_loss_limit below the day's start:
                         no new positions until the next UTC day.
  7. open positions      at most max_open_positions (adding to a market
                         already held does not count as a new one).
  8. size caps           the stake is cut to the tightest of: per position,
                         per market, per strategy, total exposure — all as
                         fractions of CURRENT equity. If what is left is
                         below min_stake the proposal is rejected; if it is
                         less than asked, the decision is REDUCED.

"Stake" is the money at risk: for a $1-payoff share bought at 40c the stake
is the 40c paid, which is also the most that position can lose. A strategy
that can lose more than it pays in (a short, leverage) must pass its
maximum loss as the stake.

The governor is market-agnostic: it sees strategies, market ids and money,
not order books. Every decision is recorded with its checks.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from jarvis.directives import get_sentinel, get_arbiter, get_terminator

RISK_VERSION = "10C.1"
APPROVED, REDUCED, REJECTED = "APPROVED", "REDUCED", "REJECTED"


@dataclass(frozen=True)
class RiskSettings:
    bankroll: float = 1000.0             # PAPER bankroll (USD, notional) a fresh portfolio starts with
    max_position_fraction: float = 0.02  # one position
    max_market_fraction: float = 0.04    # everything held in one market
    max_strategy_fraction: float = 0.15  # everything one strategy holds
    max_total_exposure: float = 0.30     # everything at risk at once
    max_open_positions: int = 15
    daily_loss_limit: float = 0.03       # of the day's starting equity
    max_drawdown: float = 0.10           # from peak equity -> halt until the owner resumes
    min_stake: float = 1.0               # USD; below this a position is not worth opening

    def __post_init__(self):
        fractions = (self.max_position_fraction, self.max_market_fraction, self.max_strategy_fraction,
                     self.max_total_exposure, self.daily_loss_limit, self.max_drawdown)
        if any(not 0 < f <= 1 for f in fractions):
            raise ValueError("every JARVIS_RISK_* fraction must be in (0, 1]")
        if self.max_position_fraction > 0.10 or self.max_total_exposure > 0.60 or self.max_drawdown > 0.25 \
                or self.daily_loss_limit > 0.10:
            raise ValueError("risk limits exceed the hard ceilings (position 10%, exposure 60%, drawdown 25%, "
                             "daily loss 10%)")
        if not (self.max_position_fraction <= self.max_market_fraction <= self.max_total_exposure
                and self.max_strategy_fraction <= self.max_total_exposure):
            raise ValueError("limits must nest: position <= market <= total exposure, strategy <= total exposure")
        if self.bankroll <= 0 or self.min_stake <= 0 or self.max_open_positions < 1:
            raise ValueError("bankroll and min_stake must be > 0 and max_open_positions >= 1")

    @classmethod
    def from_environment(cls) -> "RiskSettings":
        d = cls()

        def f(name, default):
            raw = os.getenv(name, "").strip()
            return type(default)(float(raw)) if raw else default
        return cls(bankroll=f("JARVIS_RISK_BANKROLL", d.bankroll),
                   max_position_fraction=f("JARVIS_RISK_MAX_POSITION", d.max_position_fraction),
                   max_market_fraction=f("JARVIS_RISK_MAX_MARKET", d.max_market_fraction),
                   max_strategy_fraction=f("JARVIS_RISK_MAX_STRATEGY", d.max_strategy_fraction),
                   max_total_exposure=f("JARVIS_RISK_MAX_EXPOSURE", d.max_total_exposure),
                   max_open_positions=f("JARVIS_RISK_MAX_POSITIONS", d.max_open_positions),
                   daily_loss_limit=f("JARVIS_RISK_DAILY_LOSS", d.daily_loss_limit),
                   max_drawdown=f("JARVIS_RISK_MAX_DRAWDOWN", d.max_drawdown),
                   min_stake=f("JARVIS_RISK_MIN_STAKE", d.min_stake))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Position:
    market_id: str
    strategy: str
    stake: float                         # USD at risk (the most this position can lose)


@dataclass(frozen=True)
class Portfolio:
    equity: float                        # cash + positions at their current value
    peak_equity: float
    day_start_equity: float
    positions: tuple[Position, ...] = ()

    @classmethod
    def fresh(cls, bankroll: float) -> "Portfolio":
        return cls(equity=bankroll, peak_equity=bankroll, day_start_equity=bankroll)

    @property
    def exposure(self) -> float:
        return sum(p.stake for p in self.positions)


@dataclass(frozen=True)
class Proposal:
    strategy: str
    market_id: str
    stake: float                         # USD the strategy wants at risk
    mode: str = "PAPER"                  # "PAPER" | "LIVE"
    venue: str = ""                      # e.g. "polymarket"; information only
    detail: dict[str, Any] = field(default_factory=dict)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


class RiskGovernor:
    def __init__(self, settings: RiskSettings, state, gate_provider: Callable[[], dict[str, Any]] | None,
                 clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.state = state                    # RiskState (kill switch, drawdown halt, decision log)
        self.gate_provider = gate_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(self, proposal: Proposal, portfolio: Portfolio | None = None, record: bool = True) -> dict[str, Any]:
        # === SENTINEL / ARBITER / TERMINATOR enforcement ===
        try:
            from jarvis.directives import get_sentinel, get_arbiter, get_terminator
            terminator = get_terminator()
            if terminator.is_terminated():
                return {
                    "approved": False,
                    "reason": "TERMINATED: " + (terminator.snapshot().get("reason") or "platform terminated by directive"),
                }

            sentinel = get_sentinel()
            arbiter = get_arbiter()
            if sentinel.is_survival_mode():
                max_losses = sentinel.max_consecutive_losses()
                if arbiter.should_terminate(max_losses):
                    terminator.terminate(reason=f"Directive enforcement: {max_losses} consecutive losses")
                    return {
                        "approved": False,
                        "reason": f"TERMINATED: {max_losses} consecutive losses reached",
                    }
                # Soft pause
                soft_threshold = int(sentinel.get("soft_pause_after_losses", 2))
                if arbiter.should_soft_pause(soft_threshold):
                    return {
                        "approved": False,
                        "reason": f"SOFT PAUSE: {soft_threshold} consecutive losses — reducing exposure",
                    }
        except ImportError:
            # Directives layer not installed; fail open (allow trading)
            pass

        # === Original risk evaluation continues below ===
        s = self.settings
        pf = portfolio or Portfolio.fresh(s.bankroll)
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})
            return bool(ok)

        def done(verdict: str, stake: float, reason: str) -> dict[str, Any]:
            decision = {"risk_version": RISK_VERSION, "at": self.clock().isoformat(timespec="seconds"),
                        "verdict": verdict, "approved_stake": math.floor(stake * 100 + 1e-7) / 100,   # whole cents, never rounded up
                        "requested_stake": proposal.stake
                        if _finite(proposal.stake) else None, "reason": reason, "strategy": proposal.strategy,
                        "market_id": proposal.market_id, "mode": proposal.mode, "venue": proposal.venue,
                        "checks": checks, "execution_locked": True}
            if record:
                decision["decision_id"] = self.state.record_decision(decision)
            return decision

        mode = str(proposal.mode).upper()
        if not check("live trading locked", mode != "LIVE", "LIVE is never approved by the governor"):
            return done(REJECTED, 0.0, "LIVE_TRADING_LOCKED")
        if not check("mode", mode == "PAPER", f"mode {proposal.mode!r}"):
            return done(REJECTED, 0.0, "UNKNOWN_MODE")
        kill = self.state.kill_switch()
        if not check("kill switch", not kill["engaged"], kill.get("reason") or "not engaged"):
            return done(REJECTED, 0.0, "KILL_SWITCH")
        try:
            gate = self.gate_provider() if self.gate_provider else None
            if gate is not None and not isinstance(gate, dict):
                raise TypeError("the gate is not a mapping")

            def names(key):                            # only real lists count; a bare string would match substrings
                v = gate.get(key, ())
                return {x for x in v if isinstance(x, str)} if isinstance(v, (list, tuple, set, frozenset)) else set()
            paper, forward = (names("approved_for_paper"), names("forward_test")) if gate else (set(), set())
            ok = bool(gate) and (proposal.strategy in paper or proposal.strategy in forward)
            detail = "gate unavailable" if not gate else (
                "approved for paper" if proposal.strategy in paper else
                "forward test" if ok else "not approved by the validation gate")
        except Exception as exc:                       # fail closed: no gate, no trade
            gate, ok, detail = None, False, f"gate error: {type(exc).__name__}: {str(exc)[:80]}"
        if not check("validation gate", ok, detail):
            return done(REJECTED, 0.0, "GATE_UNAVAILABLE" if not gate else "NOT_ALLOWED_BY_GATE")
        sane = (bool(str(proposal.strategy).strip()) and bool(str(proposal.market_id).strip())
                and _finite(proposal.stake) and proposal.stake > 0
                and all(_finite(x) and x > 0 for x in (pf.equity, pf.peak_equity, pf.day_start_equity))
                and all(_finite(p.stake) and p.stake >= 0 for p in pf.positions))
        if not check("sanity", sane, "stake, equity and positions must be positive finite numbers"):
            return done(REJECTED, 0.0, "INVALID_PROPOSAL")
        floor = pf.peak_equity * (1 - s.max_drawdown)
        halted = self.state.drawdown_halt()
        if pf.equity <= floor and not halted["engaged"]:
            self.state.set_drawdown_halt(f"equity {pf.equity:.2f} fell to/below {floor:.2f} "
                                         f"({s.max_drawdown:.0%} under the peak {pf.peak_equity:.2f})")
            halted = self.state.drawdown_halt()
        if not check("drawdown halt", not halted["engaged"], halted.get("reason") or
                     f"equity {pf.equity:.2f} above the halt level {floor:.2f}"):
            return done(REJECTED, 0.0, "DRAWDOWN_HALT")
        day_floor = pf.day_start_equity * (1 - s.daily_loss_limit)
        if not check("daily loss limit", pf.equity > day_floor,
                     f"equity {pf.equity:.2f} vs today's limit {day_floor:.2f}"):
            return done(REJECTED, 0.0, "DAILY_LOSS_LIMIT")
        markets = {p.market_id for p in pf.positions}
        if not check("open positions", proposal.market_id in markets or len(markets) < s.max_open_positions,
                     f"{len(markets)} open of {s.max_open_positions}"):
            return done(REJECTED, 0.0, "MAX_OPEN_POSITIONS")
        in_market = sum(p.stake for p in pf.positions if p.market_id == proposal.market_id)
        in_strategy = sum(p.stake for p in pf.positions if p.strategy == proposal.strategy)
        caps = {
            "per position": s.max_position_fraction * pf.equity,
            "per market": s.max_market_fraction * pf.equity - in_market,
            "per strategy": s.max_strategy_fraction * pf.equity - in_strategy,
            "total exposure": s.max_total_exposure * pf.equity - pf.exposure,
        }
        tightest = min(caps, key=caps.get)
        allowed = max(0.0, min(proposal.stake, caps[tightest]))
        for name, room in caps.items():
            check(f"cap: {name}", room >= min(proposal.stake, s.min_stake), f"room ${max(room, 0):.2f}")
        if not check("minimum stake", allowed >= s.min_stake, f"${allowed:.2f} left vs minimum ${s.min_stake:.2f}"):
            return done(REJECTED, 0.0, f"NO_ROOM ({tightest})")
        if allowed < proposal.stake - 1e-9:
            return done(REDUCED, allowed, f"cut by the {tightest} cap")
        return done(APPROVED, allowed, "within every limit")

    def status(self, portfolio: Portfolio | None = None) -> dict[str, Any]:
        pf = portfolio or Portfolio.fresh(self.settings.bankroll)
        try:
            gate = self.gate_provider() if self.gate_provider else None
            gate_view = {"available": bool(gate), "approved_for_paper": (gate or {}).get("approved_for_paper", []),
                         "forward_test": (gate or {}).get("forward_test", []), "live_approved": []}
        except Exception as exc:
            gate_view = {"available": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        return {"risk_version": RISK_VERSION, "settings": self.settings.as_dict(),
                "kill_switch": self.state.kill_switch(), "drawdown_halt": self.state.drawdown_halt(),
                "portfolio": {"equity": pf.equity, "peak_equity": pf.peak_equity,
                              "day_start_equity": pf.day_start_equity, "exposure": round(pf.exposure, 2),
                              "open_markets": len({p.market_id for p in pf.positions})},
                "gate": gate_view, "decisions": self.state.decision_counts(), "live_trading": "LOCKED",
                "execution_locked": True}
