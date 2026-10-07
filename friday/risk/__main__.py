"""
FRIDAY Risk Governor CLI.

    python -m friday.risk status
    python -m friday.risk decisions [--limit 20]
    python -m friday.risk kill [--reason TEXT]
    python -m friday.risk resume --confirm RESUME
    python -m friday.risk drill          (stress drills on a temporary store)

Risk store: --db PATH, else $FRIDAY_RISK_DB, else ~/friday/data/risk.db.
`status` shows settings, the kill switch and halts; the validation gate is
read by the running service (GET /api/risk/status), not here.
"""

import argparse
import json
import os
import sys

from .governor import RiskGovernor, RiskSettings
from .state import DEFAULT_RISK_DB, RiskState


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m friday.risk", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "decisions", "kill", "resume", "drill"])
    parser.add_argument("--db", default=os.getenv("FRIDAY_RISK_DB", "").strip() or DEFAULT_RISK_DB)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--reason", default="")
    parser.add_argument("--confirm", default="")
    a = parser.parse_args(argv)
    if a.command == "drill":                 # never touches the real risk store
        from .drills import render_drills, run_drills
        try:
            report = run_drills(RiskSettings.from_environment())
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        print(render_drills(report))
        return 0 if report["passed"] else 1
    state = RiskState(a.db)
    if a.command == "kill":
        print(json.dumps(state.engage_kill_switch(a.reason, "cli"), indent=2))
        return 0
    if a.command == "resume":
        try:
            print(json.dumps(state.resume(a.confirm, "cli"), indent=2))
        except PermissionError as exc:
            print(f"REFUSED: {exc}")
            return 3
        return 0
    if a.command == "decisions":
        for d in state.decisions(a.limit):
            print(f"{d['at']} {d['verdict']:<9} {d['strategy']:<22} {d['market_id']:<14} asked {d['requested_stake']} "
                  f"-> {d['approved_stake']}  {d['reason']}")
        return 0
    try:
        settings = RiskSettings.from_environment()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(RiskGovernor(settings, state, None).status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
