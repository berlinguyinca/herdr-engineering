#!/usr/bin/env python3
"""Repeatable cold/warm build benchmark using the build_metrics helpers (stdlib only).

    build_bench.py --repeat 3 --dir <crate> -- cargo build --offline
    build_bench.py --repeat 3 --dir <crate> --cold --copy -- cargo build --offline

Runs the command directly (NOT through the build gate), N times, and prints one
JSON document. ``phase`` is "warm" for plain runs (no cache reset is done: the
first run may still be cold if nothing was built before). ``--cold`` requires
``--copy``: the crate is copied (without ``target/``) into a temp dir this
script creates, and only there is ``cargo clean`` run before each cold run.
The script refuses ``--cold`` on any directory it did not create itself.
Unavailable metrics are None and listed in each run's ``unavailable``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr_engineering.build_metrics import run_measured  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--cold", action="store_true",
                    help="cargo clean before each cold run (only inside a private --copy)")
    ap.add_argument("--copy", action="store_true",
                    help="run in a private temp copy of --dir (excluding target/)")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        ap.error("no command given")
    src = Path(a.dir).resolve()
    if not src.is_dir():
        ap.error(f"--dir {src} is not a directory")
    if a.cold and not a.copy:
        print("build_bench: refusing --cold without --copy (never clean a tree "
              "this script did not create)", file=sys.stderr)
        return 2

    tmp: Path | None = None
    work = src
    try:
        if a.copy:
            tmp = Path(tempfile.mkdtemp(prefix="build-bench-"))
            work = tmp / "work"
            shutil.copytree(src, work, ignore=shutil.ignore_patterns("target"))
        runs: list[dict] = []

        def one(phase: str, i: int) -> None:
            m = run_measured(cmd, cwd=str(work), emit=False)
            runs.append(dict(m.finished, phase=phase, iteration=i))

        if a.cold:
            assert tmp is not None and work.is_relative_to(tmp)  # we created it
            for i in range(a.repeat):
                subprocess.run(["cargo", "clean"], cwd=work, check=False,
                               capture_output=True)
                one("cold", i)
        for i in range(a.repeat):
            one("warm", i)
        print(json.dumps({"cmd_kind": runs[0]["cmd_kind"], "dir_copied": a.copy,
                          "runs": runs}, indent=2, sort_keys=True))
        return 0 if all(r["exit_code"] == 0 for r in runs) else 1
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
