"""
FRIDAY paper trading CLI.

    python -m friday.paper report                         the paper record and each forward test's standing
    python -m friday.paper trades [--limit 30]            closed paper trades, newest first
    python -m friday.paper promote --confirm PROMOTE      approve the Bollinger forward test, if it has SUCCEEDED
    python -m friday.paper revoke [--reason TEXT]         withdraw that approval

Risk store: --db PATH, else $FRIDAY_RISK_DB, else ~/friday/data/risk.db.
Paper money only. Nothing here can place an order anywhere. `promote` and `revoke`
take effect in the running service within ten minutes (the gate is cached).
"""

import argparse
import os
import sys

from friday.risk import DEFAULT_RISK_DB, PaperLedger, RiskSettings

from .engine import PaperEngine
from .sources import NAME, BollingerResting


def render_report(engine: PaperEngine, source: BollingerResting) -> str:
    rep, s, v = engine.report(), engine.ledger.summary(), source.standing()
    lines = [f"FRIDAY PAPER TRADING {rep['paper_version']} — paper money only; live trading LOCKED",
             f"account : equity ${s['equity']:.2f} of ${s['bankroll']:g} · P&L ${s['total_pnl']:+.2f} · "
             f"drawdown {s['drawdown']:.1%} · open {rep['open']} · closed {rep['closed']}", ""]
    for r in rep["strategies"]:
        o = r.get("resting") or {}
        orders = (f" · orders {o.get('placed', 0)} placed, {o.get('filled') or 0} filled, {o.get('expired') or 0} expired, "
                  f"{o.get('pending') or 0} waiting") if o else ""
        if r["trades"]:
            lines.append(f"{r['strategy']:<20} {r['trades']} trades on {r['days']} days · {r['wins']} wins · P&L ${r['pnl']:+.2f} · "
                         f"{r['avg_per_share'] * 100:+.2f}c a share · {r['avg_r']}R · exits {r['exits']}{orders}")
        else:
            lines.append(f"{r['strategy']:<20} no closed trades yet{orders}")
    if not rep["strategies"]:
        lines.append("no paper orders or trades yet")
    lines += ["", f"forward test {NAME}: {v['status']} — {v['reason']}"]
    if v["interval"]:
        lines.append(f"  profit per share {v['avg_per_share'] * 100:+.2f}c, 95% interval {v['interval'][0] * 100:+.2f}c to "
                     f"{v['interval'][1] * 100:+.2f}c · P&L ${v['pnl']:+.2f} (test loss limit ${v['loss_limit']:g})")
    if v["status"] == "SUCCEEDED":
        lines.append("  to approve it for paper trading:  python -m friday.paper promote --confirm PROMOTE")
    for x in rep["refused"][:6]:
        lines.append(f"  refused {x['n']:>4}: {x['reason']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m friday.paper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["report", "trades", "promote", "revoke"])
    parser.add_argument("--db", default=os.getenv("FRIDAY_RISK_DB", "").strip() or DEFAULT_RISK_DB)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--reason", default="revoked by the owner")
    a = parser.parse_args(argv)
    path = os.path.expanduser(a.db)
    if not os.path.exists(path):
        print(f"no risk store at {path}")
        return 2
    try:
        bankroll = RiskSettings.from_environment().bankroll
    except ValueError:
        bankroll = RiskSettings().bankroll
    engine = PaperEngine(None, PaperLedger(path, bankroll), None)      # reads and records only: it cannot trade
    source = BollingerResting(engine, None, enabled=True)
    if a.command == "trades":
        for t in engine.trades(a.limit):
            print(f"{str(t['closed_at'])[:16]} {t['strategy']:<18} {t['close_reason']:<9} in {t['entry_price']:.3f} out {t['exit_price']:.3f} "
                  f"${t['pnl']:+.2f} {t['r']}R  {(t['label'] or t['market_id'])[:60]}")
        return 0
    if a.command == "promote":
        try:
            v = source.promote(a.confirm, "cli")
        except (PermissionError, ValueError) as exc:
            print(f"REFUSED: {exc}")
            return 3
        print(f"PROMOTED: {NAME} is approved for PAPER trading. {v['reason']}")
        return 0
    if a.command == "revoke":
        v = source.revoke(a.reason)
        print(f"{NAME}: {v['status']} — {v['reason']}")
        return 0
    print(render_report(engine, source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
