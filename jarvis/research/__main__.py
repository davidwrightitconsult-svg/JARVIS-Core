"""
FRIDAY research library CLI.

    python -m jarvis.research init
    python -m jarvis.research add FILE [FILE ...] [--kind book|paper|note|article] [--title T] [--tag X]
    python -m jarvis.research add-url URL [--kind article|paper] [--title T] [--refresh]
    python -m jarvis.research ingest-inbox [--kind ...]      process ~/jarvis/library/inbox/
                                   (inbox/paper, inbox/note, inbox/article set the kind)
    python -m jarvis.research search "query" [--limit 5] [--kind ...]
    python -m jarvis.research list
    python -m jarvis.research status
    python -m jarvis.research remove DOC_ID --confirm
    python -m jarvis.research digest DOC_ID [--sections 6] [--start N]
                                   ask the RUNNING FRIDAY service for a Claude digest
                                   (research budget; note lands in the vault FRIDAY/Research/)
    python -m jarvis.research digest-status [DOC_ID]

Library path: --library PATH, else $JARVIS_LIBRARY_PATH, else ~/jarvis/library
"""

import argparse
import json
import os
import sys
import textwrap

from .library import DEFAULT_LIBRARY_PATH, KINDS, LibraryError, ResearchLibrary, result_dict


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.research")
    parser.add_argument("command", choices=["init", "add", "add-url", "ingest-inbox", "search", "list", "status", "remove",
                                            "digest", "digest-status"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--library", default=os.getenv("JARVIS_LIBRARY_PATH", DEFAULT_LIBRARY_PATH))
    parser.add_argument("--kind", default=None, choices=KINDS)
    parser.add_argument("--title", default=None)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="add-url: replace an existing copy")
    parser.add_argument("--sections", type=int, default=6)
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--api", default=os.getenv("JARVIS_API", "http://127.0.0.1:8000"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    a = parser.parse_args(argv)

    try:
        lib = ResearchLibrary(a.library)
    except LibraryError as exc:
        print(f"ERROR: {exc}")
        return 2
    if a.command == "init":
        lib.init()
        print(json.dumps(lib.status(), indent=2))
        return 0
    if not lib.initialised:
        print(f"ERROR: no library at {lib.root}; run: python -m jarvis.research init")
        return 2

    if a.command in ("add", "add-url", "ingest-inbox"):
        if a.command == "add-url":
            if len(a.args) != 1:
                print("ERROR: give exactly one https:// URL")
                return 2
            results = [lib.add_url(a.args[0], kind=a.kind or "article", title=a.title,
                                   tags=tuple(a.tag), refresh=a.refresh)]
        elif a.command == "add":
            if not a.args:
                print("ERROR: give at least one file")
                return 2
            results = [lib.add(p, kind=a.kind or "book", title=a.title if len(a.args) == 1 else None,
                               tags=tuple(a.tag)) for p in a.args]
        else:
            results = lib.ingest_inbox(kind=a.kind or "book")
        for r in results:
            if a.json:
                print(json.dumps(result_dict(r)))
            else:
                detail = (f"{r.doc_id}  {r.chunks} passage(s)" + (f", {r.withheld_chunks} withheld" if r.withheld_chunks else "")
                          if r.status == "ADDED" else r.reason)
                print(f"{r.status:9} {r.name} — {detail}")
        if not results:
            print("inbox is empty")
        return 0 if all(r.status != "REJECTED" for r in results) else 1

    if a.command == "search":
        query = " ".join(a.args)
        hits = lib.search(query, limit=a.limit, kind=a.kind)
        if a.json:
            print(json.dumps(hits, indent=2, ensure_ascii=False))
            return 0
        if not hits:
            print("no matches")
        for i, h in enumerate(hits, 1):
            where = f", {h['location']}" if h["location"] else ""
            print(f"{i}. {h['title']}{where}  [{h['kind']}, score {h['score']}, doc {h['doc_id']}]")
            print(textwrap.indent(textwrap.fill(h["snippet"], 100), "   "))
        return 0

    if a.command == "list":
        docs = lib.documents()
        if a.json:
            print(json.dumps(docs, indent=2, ensure_ascii=False))
        for d in docs:
            if not a.json:
                print(f"{d['doc_id']}  {d['kind']:7} {d['format']:8} {d['chunks']:5} passages  {d['title']}")
        if not docs:
            print("library is empty")
        return 0

    if a.command == "status":
        print(json.dumps(lib.status(), indent=2))
        return 0

    if a.command == "digest-status":
        docs = [lib.get(a.args[0])] if a.args else lib.documents()
        for d in docs:
            if not d:
                print("no such document")
                return 1
            p = lib.digest_progress(d["doc_id"])
            total = p["total_sections"] or len(lib.sections(d["doc_id"]))
            print(f"{d['doc_id']}  {p['next_section']:>4}/{total:<4} sections  "
                  f"{p['tokens_used']:>8} tokens  {d['title']}")
        return 0

    if a.command == "digest":
        import urllib.error
        import urllib.request
        if len(a.args) != 1:
            print("ERROR: usage: digest DOC_ID [--sections N] [--start N]")
            return 2
        body = {"doc_id": a.args[0], "sections": a.sections}
        if a.start is not None:
            body["start"] = a.start
        req = urllib.request.Request(f"{a.api}/api/research/digest", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            job = json.load(urllib.request.urlopen(req, timeout=960))
        except urllib.error.HTTPError as exc:
            print(f"ERROR: FRIDAY refused: HTTP {exc.code} {exc.read().decode()[:300]}")
            return 1
        except urllib.error.URLError as exc:
            print(f"ERROR: FRIDAY is not reachable at {a.api} ({exc.reason}); is the service running?")
            return 1
        r = job.get("result") or {}
        if job.get("status") != "SUCCEEDED":
            print(f"{job.get('status')}: {job.get('error')}")
            return 1
        done = len(r.get("sections") or [])
        span = (f"sections {r.get('start', 0) + 1}-{r.get('start', 0) + done}" if done
                else f"no section recorded (next is {r.get('start', 0) + 1})")
        print(f"DIGEST {r.get('title')}: {span} of {r.get('total_sections')}, {r.get('tokens_used')} tokens"
              f"{' — COMPLETE' if r.get('complete') else ''}"
              f"{(' — stopped: ' + r['stopped']) if r.get('stopped') else ''}")
        print("Note: vault FRIDAY/Research/ (see the FRIDAY journal for the link)")
        return 0

    if a.command == "remove":
        if not a.args or not a.confirm:
            print("ERROR: usage: remove DOC_ID --confirm")
            return 2
        ok = lib.remove(a.args[0])
        print("removed" if ok else "no such document")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
