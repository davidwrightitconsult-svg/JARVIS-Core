"""
FRIDAY vault CLI.

    python -m friday.knowledge init        [--vault PATH]   create structure + seed (never overwrites)
    python -m friday.knowledge check       [--vault PATH]   read-only structure report
    python -m friday.knowledge sync-status [--vault PATH]   read-only git report (Phase 4C)
    python -m friday.knowledge sync        [--vault PATH] [--push]
                                                            commit FRIDAY/ notes now; push with --push
    python -m friday.knowledge replay --job-type RESEARCH_DIGEST [--latest 1]
                                                            re-write notes of finished jobs from
                                                            their saved results (running service;
                                                            no Claude call, no tokens)
    python -m friday.knowledge recall "question" [--vault PATH]
                                                            show what Claude would be given

Default vault path: $FRIDAY_VAULT_PATH or ~/friday/FRIDAY-Knowledge-Vault
"""

import argparse
import json
import os
import sys

from .recall import KnowledgeRecall
from .sync import VaultSync, VaultSyncError
from .vault import KnowledgeVault

DEFAULT_VAULT = os.getenv("FRIDAY_VAULT_PATH", "~/friday/FRIDAY-Knowledge-Vault")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m friday.knowledge")
    parser.add_argument("command", choices=["init", "check", "sync-status", "sync", "recall", "replay"])
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--push", action="store_true", help="sync: also push (fast-forward only)")
    parser.add_argument("--branch", default=os.getenv("FRIDAY_VAULT_BRANCH", "main"))
    parser.add_argument("--job-type", default="RESEARCH_DIGEST")
    parser.add_argument("--latest", type=int, default=1)
    parser.add_argument("--api", default=os.getenv("FRIDAY_API", "http://127.0.0.1:8000"))
    args = parser.parse_args(argv)
    vault = KnowledgeVault(args.vault)

    if args.command == "sync-status":
        report = VaultSync(vault.root, branch=args.branch).inspect()
        print(json.dumps(report, indent=2))
        return 0 if report.get("git_repo") else 1
    if args.command == "sync":
        try:
            result = VaultSync(vault.root, push=args.push, branch=args.branch).sync()
        except VaultSyncError as exc:
            print(json.dumps({"status": "ERROR", "reason": str(exc)}, indent=2))
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in ("NO_CHANGES", "COMMITTED", "PUSHED") else 1
    if args.command == "replay":
        import urllib.error
        import urllib.request
        try:
            jobs = json.load(urllib.request.urlopen(f"{args.api}/api/hermes/jobs?limit=100", timeout=15))
        except urllib.error.URLError as exc:
            print(f"ERROR: FRIDAY is not reachable at {args.api} ({exc})")
            return 1
        chosen = [j for j in jobs if j["job_type"] == args.job_type.upper() and j["status"] == "SUCCEEDED"]
        chosen = list(reversed(chosen[: max(1, args.latest)]))        # oldest first
        if not chosen:
            print(f"no finished {args.job_type} jobs found")
            return 1
        for job in chosen:
            req = urllib.request.Request(f"{args.api}/api/knowledge/replay",
                                         data=json.dumps({"job_id": job["id"]}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                out = json.load(urllib.request.urlopen(req, timeout=30))
            except urllib.error.HTTPError as exc:
                print(f"ERROR {job['id'][:8]}: HTTP {exc.code} {exc.read().decode()[:200]}")
                return 1
            print(f"replayed {job['job_type']} {job['id'][:8]} ({job['created_at'][:19]}): "
                  f"{out['notes_written']} note write(s), {out['write_failures']} failure(s)"
                  f"{(' — ' + out['last_error']) if out['write_failures'] else ''}")
        return 0

    if args.command == "recall":
        block = KnowledgeRecall(vault).context_for(args.query)
        print(json.dumps(block, indent=2, ensure_ascii=False))
        return 0

    if args.command == "init":
        created = vault.init()
        print(f"Vault: {vault.root}")
        print("Created:" if created else "Nothing to create (vault already initialised).")
        for rel in created:
            print(f"  + {rel}")
    report = vault.check()
    print(json.dumps(report, indent=2))
    ok = report["exists"] and not report["missing_folders"] and all(report["seed_files_present"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
