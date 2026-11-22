#!/usr/bin/env python3
"""Deterministic test-file sharding for the unit gate (.woodpecker/unit-gates-shard-N.yml, scripts/ci/unit-gates.sh).

    unit-shard.py files <i>/<n>        the test files of shard i of n, one per line (1-based)
    unit-shard.py slow-files <i>/<n>   the files of that shard that contain a `slow`-marked test (they get the serial tail)
    unit-shard.py check <n>            prove the n shards partition the suite and that the slow tail still exists; exit 1 if not

A test file belongs to shard `int(sha256(path)[:8], 16) % n + 1`. The rule is a pure function of the path, so every shard computes the
same split on its own machine with no shared state, a new test file is covered without editing anything, and no file can fall between
shards. Files, not tests, are the unit because the gate runs `--dist=loadfile` (a file's tests share a worker and its module state).
Discovery follows pytest's defaults for `testpaths = ["tests"]`: `test_*.py` and `*_test.py` under tests/. Marker deselection (e2e,
integration, herdr_live) still happens inside pytest, exactly as in the unsharded gate. Exit status 2 = usage error.
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

PATTERNS = ("test_*.py", "*_test.py")


def _is_slow_mark(node: ast.AST) -> bool:
    """`pytest.mark.slow` or `pytest.mark.slow(...)` as an expression (a decorator or a pytestmark value)."""
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "slow" and isinstance(node.value, ast.Attribute) and node.value.attr == "mark"


def has_slow_marker(source: str) -> bool:
    """True when the module really marks something slow. Parsed, not grepped: tests about the slow tail mention the marker in strings."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(_is_slow_mark(d) for d in node.decorator_list):
            return True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            if any(_is_slow_mark(n) for n in ast.walk(node.value)):
                return True
    return False


def parse_spec(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)/(\d+)", spec)
    if not m or int(m[2]) < 1 or not 1 <= int(m[1]) <= int(m[2]):
        raise ValueError(f"bad shard '{spec}': expected <index>/<total> with 1 <= index <= total")
    return int(m[1]), int(m[2])


def discover(root: Path) -> list[str]:
    tests = root / "tests"
    if not tests.is_dir():
        raise ValueError(f"no tests/ directory under {root}: refusing to run an empty shard")
    found = {p.relative_to(root).as_posix() for pat in PATTERNS for p in tests.rglob(pat) if p.is_file()}
    return sorted(found)


def shard_of(path: str, total: int) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest()[:8], 16) % total + 1


def files_for(root: Path, index: int, total: int) -> list[str]:
    return [f for f in discover(root) if shard_of(f, total) == index]


def slow_files_for(root: Path, index: int, total: int) -> list[str]:
    return [f for f in files_for(root, index, total) if has_slow_marker((root / f).read_text(errors="replace"))]


def check(root: Path, total: int) -> list[str]:
    problems: list[str] = []
    every = discover(root)
    shards = [files_for(root, i, total) for i in range(1, total + 1)]
    flat = [f for s in shards for f in s]
    if sorted(flat) != every or len(flat) != len(set(flat)):
        problems.append("the shards are not a partition of the test files")
    for i, s in enumerate(shards, 1):
        if not s:
            problems.append(f"shard {i}/{total} is empty")
    owners = [i for i in range(1, total + 1) if slow_files_for(root, i, total)]
    if len(owners) != 1:
        problems.append(f"the slow tail must live in exactly one shard, found {owners or 'none'} (a vanished tail would drop its coverage)")
    return problems


def main(argv: list[str]) -> int:
    root = Path.cwd()
    try:
        if len(argv) == 3 and argv[1] in ("files", "slow-files"):
            index, total = parse_spec(argv[2])
            fn = files_for if argv[1] == "files" else slow_files_for
            print("\n".join(fn(root, index, total)))
            return 0
        if len(argv) == 3 and argv[1] == "check" and argv[2].isdigit() and int(argv[2]) >= 1:
            problems = check(root, int(argv[2]))
            for p in problems:
                print(f"unit-shard: {p}", file=sys.stderr)
            if not problems:
                print(f"unit-shard: partition ok: {len(discover(root))} test files in {argv[2]} shards")
            return 1 if problems else 0
        print(__doc__, file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"unit-shard: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
