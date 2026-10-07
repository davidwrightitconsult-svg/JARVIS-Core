"""
FRIDAY strategy registry CLI.

    python -m jarvis.strategies list      every registered rule, its source and fixed settings
    python -m jarvis.strategies books     which of the cited books are in JARVIS library
    python -m jarvis.strategies sources   is each rule's idea found in FRIDAY's own copy? (locations only, no book text)
"""

import argparse
import os
import sys

from .registry import STRATEGIES, books_in_library, check_sources, describe


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.strategies", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "books", "sources"])
    a = parser.parse_args(argv)
    if a.command == "list":
        d = describe()
        print(f"FRIDAY strategy registry {d['registry_version']}: {d['registered']} registered "
              f"({d['signals']} signal rules, 1 overlay), 0 tested, 0 approved")
        for s in STRATEGIES:
            print(f"\n{s.name}  [{s.key}]")
            print(f"  source : {s.source}")
            print(f"  rule   : {s.rule}")
            print(f"  adapted: {s.adaptation}")
            if s.exit:
                e = s.exit
                print(f"  exit   : stop {e.stop * 100:g}c, target {'none' if e.target is None else f'{e.target * 100:g}c'}, "
                      f"at most {e.max_hold} bars" + (f"; {e.rule}" if e.rule else ""))
            if s.params:
                print(f"  fixed  : {s.params}")
        return 0
    docs, lib = [], None
    try:
        from jarvis.research.library import DEFAULT_LIBRARY_PATH, ResearchLibrary
        lib = ResearchLibrary(os.getenv("JARVIS_LIBRARY_PATH", "").strip() or DEFAULT_LIBRARY_PATH)
        if lib.initialised:
            docs = lib.documents()
            print(f"library: {lib.root} ({len(docs)} documents)")
        else:
            print(f"library: none at {lib.root}")
            lib = None
    except Exception as exc:                       # no library on this machine is an answer, not a crash
        print(f"library not readable here ({type(exc).__name__}: {str(exc)[:120]})")
        lib = None
    if a.command == "books":
        for row in books_in_library(docs):
            mark = "IN LIBRARY" if row["in_library"] else "NOT IN LIBRARY"
            print(f"{mark:<15} {row['book']}" + (f"  (as: {row['library_title']})" if row["library_title"] else ""))
            print(f"{'':<15} strategies: {', '.join(row['strategies']) or 'none'}" + (f". {row['note']}" if row["note"] else ""))
        return 0
    report = check_sources(lib)
    for r in report["strategies"]:
        print(f"{r['status']:<20} {r['name']}")
        for b in r["books"]:
            if not b["in_library"]:
                print(f"{'':<20}   {b['book']}: not in the library")
                continue
            for f in b["found"]:
                print(f"{'':<20}   {b['book']}: \"{f['terms']}\" in {f['passages']} passages ({'; '.join(f['where'])})")
            for m in b["missing"]:
                print(f"{'':<20}   {b['book']}: \"{m}\" not found")
    print(report["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
