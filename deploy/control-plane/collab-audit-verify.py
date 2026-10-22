#!/usr/bin/env python3
"""Check the hash chain of the audit logs the collab workers write (~/herdr-agents/audit/NAME.log).

Each line carries the hash of the line before it, so a line that was edited, removed or inserted later breaks
the chain from that point. Prints one line per file and exits 1 if any chain is broken. This detects tampering
by anything that can't rewrite the whole file; a worker can only append (the sandbox hides the file).

  collab-audit-verify.py [FILE...]      default: every log under ~/herdr-agents/audit/
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def check(path: Path) -> tuple[bool, str]:
    prev, n = "0" * 64, 0
    for n, raw in enumerate(path.read_bytes().splitlines(), 1):
        try:
            rec = json.loads(raw)
            claimed = rec.pop("hash")
        except (ValueError, KeyError, AttributeError):
            return False, f"line {n}: not a log line"
        if rec.get("prev") != prev:
            return False, f"line {n}: does not follow the line before it (a line was removed or inserted)"
        if hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != claimed:
            return False, f"line {n}: was changed after it was written"
        prev = claimed
    return True, f"{n} lines, chain intact"


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted((Path.home() / "herdr-agents" / "audit").glob("*.log"))
    bad = 0
    for f in files:
        ok, msg = check(f)
        bad += not ok
        print(f"{'ok  ' if ok else 'BAD '}{f}: {msg}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
