#!/usr/bin/env python3
"""Standalone, stdlib-only entry for the corrections scan, so a host without a herdr-eng install can run it.

    corrections-scan.py scan [--recent N]   scan this host's newest N session records per harness (needs `enabled`)
    corrections-scan.py status              counts by harness and kind
    corrections-scan.py enable|disable|forget-all

Run from a directory holding the `herdr_engineering` package (or with it on PYTHONPATH). Same log, same rules as
`herdr-eng corrections`: read-only on transcripts, records stay on this host, private to the user.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herdr_engineering import corrections as cx  # noqa: E402


def main(argv: list[str]) -> int:
    op = argv[0] if argv else "status"
    log = cx.CorrectionLog.default()
    if op == "enable":
        log.enable()
    elif op == "disable":
        log.disable()
    elif op == "forget-all":
        print(json.dumps({"removed": log.forget(everything=True, tombstone=False)}))
        return 0
    elif op == "scan":
        n = int(argv[argv.index("--recent") + 1]) if "--recent" in argv else 40
        try:
            print(json.dumps({"scan": cx.scan_recent(log, limit=n), "status": log.stats()}, sort_keys=True))
        except PermissionError as error:
            print(json.dumps({"error": str(error)}))
            return 1
        return 0
    elif op != "status":
        print(json.dumps({"error": f"unknown operation {op}"}))
        return 2
    print(json.dumps({"status": log.stats()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
