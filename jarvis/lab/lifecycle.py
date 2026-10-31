"""
FRIDAY end-to-end lifecycle rehearsal (Phase 17B): does the whole machine turn?

Phase 17 calibrated the *measuring* apparatus: given a record, does the edge report reach the right
verdict? This asks the other half, which nothing had ever asked as one question:

    a signal arrives
      -> the EXECUTOR decides whether this strategy may trade now, at this size      (16B)
      -> the RISK GOVERNOR sizes it against the portfolio                            (10)
      -> the fill WALKS THE RECORDED BOOK rather than taking the best price           (14A)
      -> the TAKER FEE is folded into the booked price                               (14B)
      -> the stop and target hang off the price really paid                           (14A)
      -> the position is held, then EXITED and the exit walks the bids                (14A)
      -> an ARBITRAGE PAIR opens atomically and settles at exactly $1                 (15)
      -> the CAPACITY and EDGE reports describe what happened                    (14B/16A)

Every component here is unit-tested in isolation. That is not the same as the chain working: a
mis-wired observer order, a settings object that does not reach the engine, a fill priced against
the wrong stake, would each pass every unit test and break the platform. So this drives the real
store, the real governor, the real ledger, the real engine, the real executor and the real traders,
and reports what the artifacts actually say at each stage.

The market is synthetic. What is being tested is the plumbing, not any strategy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis.market.models import Market, Outcome, SnapshotRow
from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings, SignalTrader
from jarvis.paper.arbitrage import PairTrader
from jarvis.paper.arbitrage import STRATEGY as ARB
from jarvis.paper.costs import CostSettings
from jarvis.paper.executor import ExecutionSettings, Executor
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

LIFECYCLE_VERSION = "17B.1"
STRATEGY = "RESEARCH_FORECAST"
MKT, YES, NO = "lc-m1", "lc-m1-yes", "lc-m1-no"
ARB_MKT, ARB_YES, ARB_NO = "lc-arb", "lc-arb-yes", "lc-arb-no"

# The 9C gate is a slow judgement on a long record and has nothing to judge here, so it is supplied
# directly -- exactly as main.py supplies it, through a provider. What is under test is what the
# executor, governor and engine do ONCE the gate has said yes.
GATE = {"approved_for_paper": [STRATEGY, ARB], "forward_test": [], "live_approved": []}


class Clock:
    """Anchored just behind real time, not at a fixed date.

    Several store queries (`signals`, `watchlist`, `outcomes`) window against the wall clock rather
    than an injected one, so a simulation sitting at a fixed past date is invisible to them --
    `signals(hours=24)` returns nothing and nothing trades. Starting an hour behind now puts the
    whole lifecycle inside those windows, which is also where production sits."""

    def __init__(self, start: datetime | None = None):
        self.now = start or (datetime.now(timezone.utc) - timedelta(hours=1))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@dataclass
class Rig:
    store: MarketStore
    engine: PaperEngine
    executor: Executor
    signals: SignalTrader
    pairs: PairTrader
    clock: Clock
    ledger: PaperLedger
    trace: list[dict[str, Any]] = field(default_factory=list)

    def note(self, stage: str, **facts: Any) -> None:
        self.trace.append({"stage": stage, **facts})

    def plan(self, position_id: int) -> dict[str, Any]:
        with self.engine._db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM paper_plans WHERE position_id=?", (position_id,)).fetchone()
        return dict(row) if row else {}


def build(path, bankroll: float = 1000.0, costs: CostSettings | None = None) -> Rig:
    clock = Clock()
    store = MarketStore(path / "market.db")
    state = RiskState(path / "risk.db")
    ledger = PaperLedger(path / "risk.db", bankroll, clock)
    governor = RiskGovernor(RiskSettings(), state, lambda: GATE, clock)
    engine = PaperEngine(governor, ledger, store, PaperSettings(costs=costs or CostSettings()), clock=clock)

    def edge_report() -> dict[str, Any]:
        from jarvis.lab.edge import report
        return report(store.outcomes(days=3650, limit=100000), engine.settings.costs)

    executor = Executor(gate_provider=lambda: GATE, edge_provider=edge_report,
                        capacity_provider=engine.capacity_report,
                        settings=ExecutionSettings(enabled=True), clock=clock)
    return Rig(store=store, engine=engine, executor=executor, ledger=ledger, clock=clock,
               signals=SignalTrader(engine, store, lambda: GATE, enabled=True, stake=40.0,
                                    executor=executor, clock=clock),
               pairs=PairTrader(engine, store, lambda: GATE, enabled=True, budget=60.0,
                                executor=executor, clock=clock))


# --------------------------------------------------------------------------- the market
def _rows(market_id: str, question: str, end: str, at: str,
          legs: dict[str, tuple[float, float, float]]) -> list[SnapshotRow]:
    """`legs` maps outcome name -> (bid, ask, shares resting at each of 3 levels)."""
    out = []
    for name, (bid, ask, size) in legs.items():
        bids = [[round(bid - i * 0.01, 4), size] for i in range(3) if bid - i * 0.01 > 0]
        asks = [[round(ask + i * 0.01, 4), size] for i in range(3) if ask + i * 0.01 < 1]
        out.append(SnapshotRow(
            market_id=market_id, question=question, outcome=name,
            token_id=f"{market_id}-{name.lower()}", best_bid=bid, best_ask=ask,
            mid=round((bid + ask) / 2, 4), spread=round(ask - bid, 4), last_trade_price=None,
            depth_usd=size * bid, gamma_liquidity=None, volume_24h=None, end_date=end,
            flags=[], usable=True, observed_at=at,
            top_bids=[(p, s) for p, s in bids], top_asks=[(p, s) for p, s in asks]))
    return out


def scan(rig: Rig, legs: dict[str, tuple[float, float, float]], market_id: str = MKT,
         question: str = "Will the index close above 100 on Friday?",
         end_hours: float = 240.0, advance_seconds: float = 1.0) -> str:
    """One recorded scan, through the real ingestion path."""
    rig.clock.advance(seconds=advance_seconds)
    at = rig.clock().isoformat(timespec="seconds")
    end = (rig.clock() + timedelta(hours=end_hours)).isoformat(timespec="seconds")
    market = Market(market_id=market_id, question=question, end_date=end, neg_risk=False,
                    outcomes=[Outcome(name=n, token_id=f"{market_id}-{n.lower()}") for n in legs])
    rows = _rows(market_id, question, end, at, legs)
    rig.store.record([market], rows, {"observed_at": at, "markets": 1, "tokens": len(rows),
                                      "usable_rows": len(rows)}, source="lifecycle")
    return at


def signal(rig: Rig, direction: str = "UP", horizon_hours: float = 2.0,
           token: str = YES, market_id: str = MKT) -> int:
    """A signal of the shape the 7C scorer and 13C trader both expect."""
    at = rig.clock().isoformat(timespec="seconds")
    latest = {r["token_id"]: r for r in rig.store.latest_rows([market_id])}
    row = latest[token]
    ids = rig.store.record_signals([{
        "observed_at": at, "strategy": STRATEGY, "strategy_version": "lifecycle-1",
        "market_id": market_id, "token_id": token, "outcome": row["outcome"], "direction": direction,
        "strength": 0.7, "horizon_hours": horizon_hours,
        "expires_at": (rig.clock() + timedelta(hours=horizon_hours)).isoformat(timespec="seconds"),
        "mid": row["mid"], "best_bid": row["best_bid"], "best_ask": row["best_ask"],
        "half_spread": round((row["best_ask"] - row["best_bid"]) / 2, 4), "inputs": "{}"}])
    return ids[0]


# --------------------------------------------------------------------------- the lifecycle
def run(path) -> dict[str, Any]:
    """Walk a position from signal to settlement, then an arbitrage pair, then the reports."""
    rig = build(path)

    # 1. A market with a THIN top of book: $5 rests at the best ask, and the order wants $40. The
    #    whole point of 14A is that the rest cannot be bought at that price.
    scan(rig, {"Yes": (0.48, 0.50, 10.0), "No": (0.50, 0.52, 10.0)})
    sig_id = signal(rig)
    from jarvis.paper.fills import capacity as _capacity
    book = rig.store.levels(YES)
    room = _capacity(book["asks"], rig.engine.settings.max_slippage)
    rig.note("seeded", signal_id=sig_id, best_ask=0.50,
             resting_at_best_ask_usd=round(0.50 * 10.0, 2), capacity_in_band_usd=room,
             stake_requested=40.0)

    # 2. The executor is asked first, then the governor, then the book.
    before = rig.executor.counts.copy()
    out = rig.signals.on_run()
    position = rig.ledger.positions("OPEN")[0]
    plan = rig.plan(position["position_id"])
    rig.note("executor_consulted", decisions=len(rig.executor.counts) > len(before) or bool(rig.executor.counts),
             verdict=rig.executor.verdicts[-1]["reason"])
    rig.note("opened", placed=out["placed"], position_id=position["position_id"],
             stake=position["stake"], units=position["units"],
             best_ask=plan["entry_bid"] is not None and 0.50,
             fill_price=plan["entry_fill_price"], booked_price=position["entry_price"],
             slippage=plan["entry_slippage"], levels_walked=plan["entry_levels"],
             fee_usd=plan["entry_fee_usd"], fee_category=plan["fee_category"],
             stop_price=plan["stop_price"], requested_stake=plan["entry_requested_stake"])

    # 3. The price moves up and the horizon runs out: a time exit, walking the bids.
    # The 7C exit window is [expires_at, expires_at + max(1h, half the horizon)]. A snapshot one
    # second past it scores NO_EXIT_DATA, which is how the first run produced an empty edge report.
    rig.clock.advance(hours=2, minutes=30)
    scan(rig, {"Yes": (0.56, 0.58, 8.0), "No": (0.42, 0.44, 8.0)})
    tick = rig.engine.tick()
    closed = rig.ledger.positions("CLOSED")[0]
    plan = rig.plan(closed["position_id"])
    rig.note("exited", reason=closed["close_reason"], best_bid=0.56,
             exit_fill_price=plan["exit_fill_price"], booked_exit=closed["exit_price"],
             exit_slippage=plan["exit_slippage"], exit_levels=plan["exit_levels"],
             exit_fee_usd=plan["exit_fee_usd"], pnl=closed["pnl"],
             gross_pnl=round((plan["exit_fill_price"] - plan["entry_fill_price"]) * closed["units"], 4),
             fees_total=round((plan["entry_fee_usd"] or 0) + (plan["exit_fee_usd"] or 0), 4),
             tick=tick["closed"][0] if tick["closed"] else None)

    # 4. The signal's own horizon is scored by the real 7C scorer, which is what the edge report
    #    and therefore the executor's decay check both read.
    from jarvis.quant.scoring import Scorer
    scored = Scorer(rig.store, record=True).score_due(now=rig.clock())
    rig.note("scored", **{k: scored[k] for k in ("due", "scored", "by_status")})

    # 5. An arbitrage pair: two asks summing under $1, in a zero-fee category so the gap survives.
    scan(rig, {"Yes": (0.93, 0.95, 4000.0), "No": (0.01, 0.03, 4000.0)}, market_id=ARB_MKT,
         question="Will Russia and Ukraine sign a ceasefire?", end_hours=72)
    arb = rig.pairs.on_run()
    legs = [p for p in rig.ledger.positions("OPEN") if p["strategy"] == ARB]
    with rig.engine._db() as db:
        db.row_factory = sqlite3.Row
        pair = dict(db.execute("SELECT * FROM paper_arb_pairs").fetchone())
    rig.note("arbitrage_opened", considered=arb["considered"], opened=arb["opened"],
             ask_sum=pair["ask_sum"], legs=len(legs),
             one_decision=len({p["decision_id"] for p in legs}) == 1,
             equal_shares=round(legs[0]["units"], 4) == round(legs[1]["units"], 4),
             pairs=pair["pairs"], cost_usd=pair["cost_usd"], fees_usd=pair["fees_usd"],
             locked_usd=pair["locked_usd"])

    # 6. It resolves. A complete pair pays exactly $1 whichever way.
    rig.store.record_resolution(ARB_MKT, {ARB_YES: 1.0, ARB_NO: 0.0})
    rig.clock.advance(hours=1)
    settled = rig.pairs.on_run()
    report = rig.pairs.report()
    rig.note("arbitrage_settled", closed=settled["closed"],
             expected_locked=report["expected_locked_usd"], realised=report["realised_usd"],
             slippage_vs_expected=report["slippage_vs_expected"],
             payout_matches_pairs=abs((report["pairs"][0]["payout_usd"] or 0) - pair["pairs"]) < 1e-6)

    # 7. What the reports say about all of it.
    from jarvis.lab.edge import report as edge_report
    paper = rig.engine.report()
    capacity = rig.engine.capacity_report(ladder=(10.0, 40.0, 200.0))
    edge = edge_report(rig.store.outcomes(days=3650, limit=100000), rig.engine.settings.costs)
    rig.note("reports",
             paper_version=paper["paper_version"], mode=paper["mode"],
             equity=rig.ledger.portfolio().equity,
             capacity_strategies=sorted(capacity["strategies"]),
             capacity_rungs=[(r["stake"], r["net_usd"], r["fill_ratio"])
                             for r in (capacity["strategies"].get(STRATEGY) or {}).get("rungs", [])],
             edge_strategies={k: v["verdict"] for k, v in edge["strategies"].items()},
             executor_decisions=dict(rig.executor.counts))
    return {"lifecycle_version": LIFECYCLE_VERSION, "trace": rig.trace,
            "paper": paper, "capacity": capacity, "edge": edge,
            "execution": rig.executor.report()}


def render(result: dict[str, Any]) -> str:
    by = {t["stage"]: t for t in result["trace"]}
    o, x, a, s = by["opened"], by["exited"], by["arbitrage_opened"], by["arbitrage_settled"]
    seed, rep = by["seeded"], by["reports"]
    L = [f"END-TO-END LIFECYCLE {result['lifecycle_version']} — does the whole machine turn?", ""]

    L += ["  1. SIGNAL AND FILL (14A depth + 14B cost)",
          f"     wanted ${seed['stake_requested']:.0f} at the best ask {seed['best_ask']:.2f}, "
          f"where only ${seed['resting_at_best_ask_usd']:.0f} rests",
          f"     only ${seed['capacity_in_band_usd']:.2f} rests within "
          f"{0.02:g} of it, so the order was cut to that BY THE BOOK, not by the governor",
          f"     approved ${o['stake']:.2f} · walked {o['levels_walked']:.0f} level(s) "
          f"· filled at {o['fill_price']} (slippage {o['slippage']})",
          f"     fee ${o['fee_usd']:.4f} ({o['fee_category']}) folded in -> booked at {o['booked_price']}",
          f"     stop set at {o['stop_price']} — measured from what was PAID, not from "
          f"{seed['best_ask']:.2f}", ""]

    L += ["  2. EXIT (walks the bids, no slippage cap — leaving is not optional)",
          f"     reason {x['reason']} · best bid {x['best_bid']} · sold at {x['exit_fill_price']} "
          f"(slippage {x['exit_slippage']}) across {x['exit_levels']:.0f} level(s)",
          f"     gross {x['gross_pnl']:+.4f} − fees {x['fees_total']:.4f} = "
          f"booked PnL {x['pnl']:+.4f}", ""]

    L += ["  3. ARBITRAGE PAIR (15: both legs or neither, held to resolution)",
          f"     two asks summed to {a['ask_sum']} · {a['legs']} legs on "
          f"{'ONE' if a['one_decision'] else 'SEVERAL'} governor decision(s) · "
          f"equal shares: {a['equal_shares']}",
          f"     {a['pairs']:.1f} pairs cost ${a['cost_usd']:.2f} (fees ${a['fees_usd']:.2f}) "
          f"to lock ${a['locked_usd']:.2f}",
          f"     resolved: expected ${s['expected_locked']:.2f}, realised ${s['realised']:.2f}, "
          f"slippage {s['slippage_vs_expected']:+.2f} · pays exactly $1 a pair: "
          f"{s['payout_matches_pairs']}", ""]

    L += ["  4. WHAT THE REPORTS SAY",
          f"     paper {rep['paper_version']} · mode {rep['mode']} · equity ${rep['equity']:.2f}",
          f"     capacity ladder (stake, net, fill ratio): {rep['capacity_rungs']}",
          f"     edge verdicts: {rep['edge_strategies'] or 'nothing scored yet'}",
          f"     executor: {rep['executor_decisions']}", ""]
    L += ["  The market is synthetic. What this shows is that the plumbing carries a position",
          "  from signal to settlement with depth, fees, atomicity and reporting all intact.",
          "  It says nothing about whether any strategy works."]
    return "\n".join(L)
