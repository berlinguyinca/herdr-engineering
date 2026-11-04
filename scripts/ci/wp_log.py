#!/usr/bin/env python3
"""Read a Woodpecker pipeline's step logs after the fact, decoded and searchable.

The server keeps every step log in its database with no expiry, but the API hands them back as base64 lines with
some empty entries, so they are awkward to read. This turns them into text:

    python3 scripts/ci/wp_log.py berlinguyinca/herdr-engineering 148            # steps and their state
    python3 scripts/ci/wp_log.py berlinguyinca/herdr-engineering 148 --failures # what failed, with context
    python3 scripts/ci/wp_log.py berlinguyinca/herdr-engineering 148 --step unit-gates --grep 'not ok'
    python3 scripts/ci/wp_log.py berlinguyinca/herdr-engineering 148 --save ci-logs/148   # keep a copy

The API token is read from a file (default ~/.woodpecker-api-token), never from the command line or the environment.
A step that was cancelled or killed keeps only what it had written by then; this cannot recover the rest.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_SERVER = "https://ci.metabolomics.us"
DEFAULT_TOKEN_FILE = "~/.woodpecker-api-token"
# Lines that open a failure in the suites this repo runs (node --test, pytest) and in shell steps. A bare traceback is not one:
# passing runs print those from background threads too.
FAILURE = re.compile(r"^(not ok |(FAILED|ERROR) |=+ FAILURES =+|\+ exit [1-9])")


def decode_log(entries: list[dict]) -> list[str]:
    """Text lines of one step, in order. Entries without data (progress markers) are skipped."""
    lines = []
    for entry in sorted((e for e in entries if isinstance(e, dict) and e.get("data")), key=lambda e: e.get("line", 0)):
        text = base64.b64decode(entry["data"]).decode("utf-8", "replace")
        lines.extend(text.rstrip("\n").split("\n"))
    return lines


def failure_blocks(lines: list[str], before: int = 1, after: int = 14) -> list[list[str]]:
    """Each run of lines around a failure marker, merged when they overlap."""
    keep: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if FAILURE.match(line):
            lo, hi = max(0, i - before), min(len(lines), i + after + 1)
            if keep and lo <= keep[-1][1]:
                keep[-1] = (keep[-1][0], max(keep[-1][1], hi))
            else:
                keep.append((lo, hi))
    return [lines[lo:hi] for lo, hi in keep]


class Client:
    def __init__(self, server: str, token: str):
        self.server, self.token = server.rstrip("/"), token

    def get(self, path: str):
        request = urllib.request.Request(f"{self.server}/api{path}", headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)


def steps_of(pipeline: dict) -> list[dict]:
    return [dict(step, workflow=wf["name"]) for wf in pipeline.get("workflows") or [] for step in wf.get("children") or []]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="owner/name, or the numeric repository id")
    ap.add_argument("pipeline", type=int)
    ap.add_argument("--step", help="only steps with this name")
    ap.add_argument("--failures", action="store_true", help="print only the lines around failures, for failed steps")
    ap.add_argument("--grep", help="print only lines matching this regular expression")
    ap.add_argument("--save", metavar="DIR", help="write every step's full log to DIR/<workflow>__<step>.log")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    args = ap.parse_args(argv)

    token = Path(args.token_file).expanduser().read_text().strip()
    client = Client(args.server, token)
    try:
        repo_id = int(args.repo) if args.repo.isdigit() else client.get(f"/repos/lookup/{urllib.parse.quote(args.repo, safe='/')}")["id"]
        pipeline = client.get(f"/repos/{repo_id}/pipelines/{args.pipeline}")
    except urllib.error.HTTPError as err:
        print(f"{err.code} {err.reason}: no such repository or pipeline, or the token cannot read it", file=sys.stderr)
        return 2
    steps = [s for s in steps_of(pipeline) if not args.step or s["name"] == args.step]
    print(f"pipeline {pipeline['number']}: {pipeline['status']}  {str(pipeline.get('message') or '').splitlines()[:1]}")
    if args.save:
        Path(args.save).mkdir(parents=True, exist_ok=True)
    wanted = args.step or args.failures or args.grep or args.save
    for step in steps:
        mark = f"{step['workflow']}/{step['name']}: {step['state']} exit {step.get('exit_code')}"
        if not wanted:
            print(f"  {mark}")
            continue
        if args.failures and step["state"] not in ("failure", "error", "killed") and not args.step:
            continue
        lines = decode_log(client.get(f"/repos/{repo_id}/logs/{args.pipeline}/{step['id']}"))
        if args.save:
            (Path(args.save) / f"{step['workflow']}__{step['name']}.log").write_text("\n".join(lines) + "\n")
            print(f"  saved {mark} ({len(lines)} lines)")
            continue
        print(f"== {mark} ({len(lines)} lines)")
        if args.grep:
            pattern = re.compile(args.grep)
            print("\n".join(line for line in lines if pattern.search(line)))
        elif args.failures:
            blocks = failure_blocks(lines)
            print("\n--\n".join("\n".join(b) for b in blocks) if blocks else "(no failure marker found; the last lines are)\n" + "\n".join(lines[-20:]))
        else:
            print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
