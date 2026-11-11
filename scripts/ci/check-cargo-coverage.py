"""Enforce per-module statement, branch, and function coverage for Cargo adapters."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: Path) -> int:
    data = json.loads(path.read_text())
    failed = False
    for name, result in data["files"].items():
        summary = result["summary"]
        statements = summary["covered_lines"] / max(1, summary["num_statements"])
        branches = summary["covered_branches"] / max(1, summary["num_branches"])
        functions = [item["summary"] for key, item in result.get("functions", {}).items() if key]
        functions_hit = sum(item["covered_lines"] > 0 for item in functions) / max(1, len(functions))
        print(f"{name}: statements={statements:.1%} branches={branches:.1%} functions={functions_hit:.1%}")
        failed |= min(statements, branches, functions_hit) < .8
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
