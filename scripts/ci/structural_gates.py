#!/usr/bin/env python3
"""Structural gates: cheap, whole-repo integrity checks that the test suite cannot make cheaply.

This is deliberately *not* a subset of the unit suite - `scripts/ci/unit-gates.sh` runs whole suites on purpose and this does not
duplicate any of it. It answers questions that take milliseconds to answer and that nothing else answers before a merge: does every
data file in the repository actually parse, is any tracked file carrying a merge artifact, and do the two places that record the
memory client pin still agree?

Why it exists. `a4a1fe45` (the squash-merge of a memory pin bump) landed `lock/upstreams.yaml` containing a stray ` origin/main`
line - a conflict resolution that resolved to text, not to a marker, so nothing that greps for `<<<<<<<` would notice. It made the
file invalid YAML, broke three tests, and sat on `main` for hours because no pipeline runs on a push to `main`; the breakage
surfaced as red pipelines on unrelated PRs. Every check here is aimed at that shape of failure: valid structure, no leftover
merge text, no half-applied pin.

Run from anywhere: `python3 scripts/ci/structural_gates.py`. Exit 0 means clean; exit 1 prints every failure, not just the first.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Extensions that must parse as data, mapped to the parser that decides it.
YAML_SUFFIXES = (".yaml", ".yml")
JSON_SUFFIXES = (".json",)
TOML_SUFFIXES = (".toml",)

#: A conflict that was never resolved. `=======` is not matched on its own: Markdown section underlines use it legitimately.
CONFLICT_MARKERS = (re.compile(r"^<{7}(?: |$)", re.MULTILINE), re.compile(r"^>{7}(?: |$)", re.MULTILINE))

#: Files whose content is a template or fixture rather than data this repo consumes, so a parse failure is expected. Empty on
#: purpose: every tracked data file parses today, and an entry here must name the reason it stopped being true.
PARSE_ALLOWLIST: frozenset[str] = frozenset()


def tracked_files() -> list[str]:
    """Every path git tracks that is a real file, relative to the repository root.

    `git ls-files` also lists gitlinks (submodules such as `skills/handoff`), which are directories on disk and have no content
    to parse here - the submodule carries its own gates.
    """
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True)
    return sorted(p for p in out.stdout.split("\0") if p and (ROOT / p).is_file())


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def check_parses(files: list[str]) -> list[str]:
    """Every tracked data file parses. A conflict resolved into text is invisible to a marker grep but fatal here."""
    import yaml  # noqa: PLC0415 - imported late so a missing parser is reported as a gate failure, not a traceback

    failures = []
    for rel in files:
        if rel in PARSE_ALLOWLIST:
            continue
        text = _read(rel)
        try:
            if rel.endswith(YAML_SUFFIXES):
                list(yaml.safe_load_all(text))
            elif rel.endswith(JSON_SUFFIXES):
                json.loads(text)
            elif rel.endswith(TOML_SUFFIXES):
                import tomllib  # noqa: PLC0415 - stdlib from 3.11; the gate targets the CI interpreter

                tomllib.loads(text)
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - any parse error is the finding; report and keep going
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            failures.append(f"{rel}: does not parse ({type(exc).__name__}: {first})")
    return failures


def check_no_merge_artifacts(files: list[str]) -> list[str]:
    """No tracked file carries an unresolved merge conflict.

    Only markers are searched for. A bare ref left on a line of its own - the ` origin/main` that broke the lock file - is caught
    by `check_parses` instead, which is both exact and general: it fails because the file stopped being valid YAML, not because a
    line looked suspicious. Greping for refs instead produces false positives on ordinary prose and shell (`CHANGELOG.md`,
    `deploy.sh` and a test fixture all mention `origin/main` or `HEAD` legitimately), and a gate that cries wolf gets ignored.
    """
    failures = []
    for rel in files:
        text = _read(rel)
        if any(marker.search(text) for marker in CONFLICT_MARKERS):
            failures.append(f"{rel}: contains an unresolved merge conflict marker")
    return failures


def check_lock_and_pin_agree(files: list[str]) -> list[str]:
    """`lock/upstreams.yaml` is well-formed and its memory pin equals `memory_install.PINNED_REVISION`.

    The same invariant is asserted by `tests/test_memory_install.py::test_pin_matches_lock`, which is the authority. This repeats
    it because that test needs the package installed and a full environment, and the failure this guards - a bump applied to one
    of the two places - is worth catching in the seconds-long gate too.
    """
    import yaml  # noqa: PLC0415

    if "lock/upstreams.yaml" not in set(files):
        return ["lock/upstreams.yaml is not tracked"]
    failures: list[str] = []
    try:
        lock = yaml.safe_load(_read("lock/upstreams.yaml"))
    except Exception as exc:  # noqa: BLE001
        return [f"lock/upstreams.yaml: does not parse ({exc})"]
    deps = lock.get("dependencies") if isinstance(lock, dict) else None
    if not isinstance(deps, dict) or not deps:
        return ["lock/upstreams.yaml: `dependencies` is missing or empty"]
    for name, entry in deps.items():
        if not isinstance(entry, dict):
            failures.append(f"lock/upstreams.yaml: dependency {name!r} is not a mapping")
            continue
        if not entry.get("source"):
            failures.append(f"lock/upstreams.yaml: dependency {name!r} has no 'source'")
        # `revision: null` is a deliberate choice, not an omission: several dependencies are tracked by source and are not
        # commit-pinned. Only a value that is present must look like a commit or a release.
        revision = entry.get("revision")
        if revision is not None and not re.fullmatch(r"[0-9a-f]{7,40}|[0-9][0-9a-zA-Z.+_-]*", str(revision)):
            failures.append(f"lock/upstreams.yaml: dependency {name!r} revision {revision!r} is neither a hex commit nor a version")
    constant = re.search(r'^PINNED_REVISION\s*=\s*"([0-9a-f]+)"', _read("herdr_engineering/memory_install.py"), re.MULTILINE)
    pinned = deps.get("herdr-memory", {}).get("revision") if isinstance(deps.get("herdr-memory"), dict) else None
    if constant is None:
        failures.append("herdr_engineering/memory_install.py: no PINNED_REVISION constant found")
    elif pinned is None:
        failures.append("lock/upstreams.yaml: no `herdr-memory` dependency to compare the pin against")
    elif constant.group(1) != pinned:
        failures.append(
            f"pin drift: memory_install.PINNED_REVISION is {constant.group(1)} but lock/upstreams.yaml pins {pinned}"
        )
    return failures


def main(files: list[str] | None = None) -> int:
    """Run every check. `files` defaults to what git tracks; tests pass a fixture listing instead."""
    try:
        import yaml  # noqa: F401, PLC0415
    except ImportError:
        print("FAIL pyyaml is required: pip install pyyaml", file=sys.stderr)
        return 2
    files = tracked_files() if files is None else files
    checks = (
        ("data files parse", check_parses),
        ("no merge artifacts", check_no_merge_artifacts),
        ("lock and pin agree", check_lock_and_pin_agree),
    )
    failed = False
    for label, check in checks:
        failures = check(files)
        for failure in failures:
            print(f"FAIL {label}: {failure}")
            failed = True
        print(f"{'FAIL' if failures else 'ok  '} {label}: {len(failures)} problem(s) in {len(files)} tracked files")
    if failed:
        return 1
    print("STRUCTURAL GATES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
