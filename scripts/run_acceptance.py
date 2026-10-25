#!/usr/bin/env python3
"""One command for the T16 end-to-end acceptance suite.

    scripts/run-acceptance.sh [--build] [--regression] [--only s2,s5] [--results FILE]

Everything it needs is read from tests/e2e/acceptance.yaml (or acceptance.local.yaml) and tests/e2e/manifest.yaml.
There are NO environment variables to set. It runs
  1. the Herdr-side pytest suite (tests/e2e, marker `e2e`) against real control-plane processes on loopback;
  2. every Pi-side file test/e2e/*.e2e.test.ts of the Pi checkout, ONE FILE PER PROCESS and without
     --test-force-exit (which can drop the tail of a file);
and FAILS (non-zero) unless every suite ran at least the number of tests the manifest expects, nothing was skipped
(other than strict xfails, which are the documented gaps), and nothing failed. A suite that reports zero tests is a
failure, never a pass.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.e2e import harness  # noqa: E402


def sh(cmd, cwd, timeout=1800):
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr, time.time() - t0


def run_pytest(selection: str | None, regression: bool) -> dict:
    junit_dir = Path(tempfile.mkdtemp(prefix="t16-junit-"))
    junit = junit_dir / "junit.xml"
    cmd = [sys.executable, "-m", "pytest", "tests/e2e", "-q", "-p", "no:cacheprovider",
           "-o", "xfail_strict=true", "-m", "e2e or e2e_unit",              # the e2e marker is opt-in: pyproject addopts deselects it by default
            f"--junitxml={junit}", "-rxX"]
    if selection:
        cmd += ["-k", selection]
    code, out, secs = sh(cmd, ROOT)
    per_file: dict[str, dict] = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            # classname is the dotted module path (tests.e2e.test_x[.Class]): normalise to a file name
            mod = case.get("classname", "").split(".")
            fname = next((m + ".py" for m in mod if m.startswith("test_")), case.get("classname", "?"))
            row = per_file.setdefault(fname, {"tests": 0, "passed": 0, "failed": 0, "xfailed": 0, "skipped": 0, "gaps": [],
                                              "seconds": 0.0})
            row["tests"] += 1
            row["seconds"] = round(row["seconds"] + float(case.get("time") or 0), 1)
            skip = case.find("skipped")
            if case.find("failure") is not None or case.find("error") is not None:
                row["failed"] += 1
            elif skip is not None:
                if skip.get("type") == "pytest.xfail":
                    row["xfailed"] += 1
                    row["gaps"].append(f"{case.get('name')}: {skip.get('message', '')[:200]}")
                else:
                    row["skipped"] += 1
            else:
                row["passed"] += 1
    shutil.rmtree(junit_dir, ignore_errors=True)       # the runner's own temp dir, created above
    result = {"runner": "pytest", "exit": code, "seconds": round(secs, 1), "files": per_file, "tail": out[-1500:]}
    if regression:
        code2, out2, secs2 = sh([sys.executable, "-m", "pytest", "tests/control_plane", "tests/test_central_config_servers.py", "-q",
                                 "-p", "no:cacheprovider"], ROOT, 3600)
        result["regression"] = {"exit": code2, "seconds": round(secs2, 1), "tail": out2[-600:]}
    return result


def run_node_file(node: str, pi: Path, rel: str) -> dict:
    code, out, secs = sh([node, "--test", "--import", "./test/support/isolatedState.ts", rel], pi, 900)

    def num(label):
        m = re.search(rf"^# {label} (\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    gaps = re.findall(r"^\s*ok \d+ - (GAP: .*?)(?: #|\s*$)", out, re.M)
    return {"exit": code, "seconds": round(secs, 1), "tests": num("tests"), "passed": num("pass"), "failed": num("fail"),
            "skipped": num("skipped"), "todo": num("todo"), "cancelled": num("cancelled"), "gaps": gaps,
            "tail": out[-800:] if code else ""}


NOTHING_SELECTED = "nothing selected on the herdr side (pytest exit 5): --only picked only Pi-side scenarios, so no herdr test was expected"


def herdr_side_empty(manifest: dict, tags: set | None) -> bool:
    """True when `--only` selects scenarios that have no herdr pytest file, so pytest legitimately collects nothing (exit 5)."""
    return bool(tags) and not any(spec["scenario"] in tags for spec in manifest["herdr"].values())


def judge(manifest: dict, res: dict, pi_results: dict, tags: set | None, pi_files_on_disk: list[str]) -> list[str]:
    """Pure verdict over the collected results (unit-tested in tests/e2e/test_runner_guards.py)."""
    failures: list[str] = []
    for fname, spec in manifest["herdr"].items():
        if tags and spec["scenario"] not in tags:
            continue
        row = res["files"].get(Path(fname).name)
        if not row or row["tests"] == 0:
            failures.append(f"herdr {fname}: ran ZERO tests")
            continue
        if row["tests"] < spec["min_tests"]:
            failures.append(f"herdr {fname}: ran {row['tests']} tests, expected at least {spec['min_tests']}")
        if row["failed"] or row["skipped"]:
            failures.append(f"herdr {fname}: {row['failed']} failed, {row['skipped']} skipped (only strict xfail may skip)")
        if row["xfailed"] > spec.get("max_xfail", 0):
            failures.append(f"herdr {fname}: {row['xfailed']} xfail, manifest allows {spec.get('max_xfail', 0)}")
    unknown = sorted(set(res["files"]) - {Path(f).name for f in manifest["herdr"]})
    if unknown and not tags:
        failures.append(f"herdr test files not in manifest.yaml: {unknown}")
    if res["exit"] == 5 and herdr_side_empty(manifest, tags) and any(s["scenario"] in tags for s in manifest["pi"].values()):
        pass                                            # expected: the Pi side is what was selected (main() prints NOTHING_SELECTED)
    elif res["exit"] != 0:
        failures.append(f"herdr pytest exit {res['exit']}")
    if res.get("regression", {}).get("exit", 0):
        failures.append("herdr regression suite failed")
    for rel in pi_files_on_disk:
        if rel not in manifest["pi"] and not tags:
            failures.append(f"pi test file not in manifest.yaml: {rel}")
    for rel, spec in manifest["pi"].items():
        if tags and spec["scenario"] not in tags:
            continue
        r = pi_results.get(rel)
        if r is None:
            failures.append(f"pi {rel}: file missing")
            continue
        if r["tests"] == 0:
            failures.append(f"pi {rel}: ran ZERO tests")
        elif r["tests"] < spec["min_tests"]:
            failures.append(f"pi {rel}: ran {r['tests']} tests, expected at least {spec['min_tests']}")
        if len(r.get("gaps", [])) > spec.get("max_gap", 0):
            failures.append(f"pi {rel}: {len(r['gaps'])} tests named GAP, manifest allows {spec.get('max_gap', 0)} "
                            "(a failing test must not be renamed GAP)")
        if r["exit"] or r["failed"] or r["cancelled"] or r["skipped"] or r["todo"]:
            failures.append(f"pi {rel}: exit {r['exit']}, {r['failed']} failed, {r['cancelled']} cancelled, "
                            f"{r['skipped']} skipped, {r['todo']} todo\n{r['tail']}")
    return failures


def xfail_violations(sources: dict[str, str]) -> list[str]:
    """Every xfail in the Herdr e2e suite must be the `pytest.mark.xfail(..., strict=True)` call form (so an XPASS fails the
    run). Rejected: a bare `@pytest.mark.xfail` or `pytestmark = pytest.mark.xfail` (non-strict), a `strict` that is not the
    literal True, `**kwargs` that could hide it, and the imperative `pytest.xfail()`. Skips are caught at run time: any skip
    fails the run. `sources` maps file name -> python source text."""
    bad: list[str] = []
    for name, text in sources.items():
        tree = ast.parse(text)
        called = {id(n.func): n for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "xfail" and isinstance(node.ctx, ast.Load):
                bad.append(f"{name}:{node.lineno}: bare `xfail` name (use pytest.mark.xfail(strict=True))")
                continue
            if not (isinstance(node, ast.Attribute) and node.attr == "xfail"):
                continue
            is_marker = isinstance(node.value, ast.Attribute) and node.value.attr == "mark"
            call = called.get(id(node))
            if not is_marker:
                bad.append(f"{name}:{node.lineno}: imperative pytest.xfail() is not allowed (use the strict marker)")
            elif call is None:
                bad.append(f"{name}:{node.lineno}: xfail marker used without a call, so it is not strict (needs strict=True)")
            elif any(k.arg is None for k in call.keywords):
                bad.append(f"{name}:{node.lineno}: xfail(**kwargs) can hide strict=True; spell it out")
            else:
                strict = next((k.value for k in call.keywords if k.arg == "strict"), None)
                if not (isinstance(strict, ast.Constant) and strict.value is True):
                    bad.append(f"{name}:{node.lineno}: xfail without strict=True")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="cargo build -j 4 the autospec binary first")
    ap.add_argument("--regression", action="store_true", help="also run the existing control-plane regression suite")
    ap.add_argument("--only", default=None, help="comma list of scenario tags (s1..s7, harness) to run")
    ap.add_argument("--results", default=str(harness.RUN_DIR / "results.json"))
    args = ap.parse_args()
    cfg = harness.load_acceptance()
    manifest = yaml.safe_load((harness.E2E / "manifest.yaml").read_text())
    pi = harness.resolve(cfg, "pi_repo")
    autospec_repo = harness.resolve(cfg, "autospec_repo")
    bin_path = harness.resolve(cfg, "autospec_bin")
    tags = set(args.only.split(",")) if args.only else None
    failures: list[str] = []

    if args.build or not bin_path.exists():
        code, out, _ = sh(["cargo", "build", "-j", "4", "-p", "autospec-cli"], autospec_repo, 3600)
        print(f"[build] cargo build -j 4 -p autospec-cli -> exit {code}")
        if code:
            failures.append("autospec build failed:\n" + out[-800:])
    if not bin_path.exists():
        failures.append(f"autospec binary missing at {bin_path}")

    report: dict = {"herdr": None, "pi": {}}
    selection = None
    if tags:
        wanted = [f for f, spec in manifest["herdr"].items() if spec["scenario"] in tags]
        selection = " or ".join(Path(f).stem for f in wanted) or "nothing_selected"
    res = run_pytest(selection, args.regression)
    report["herdr"] = res
    if res["exit"] == 5 and herdr_side_empty(manifest, tags):
        print(f"[herdr] {NOTHING_SELECTED}")
    node = str(cfg.get("node", "node"))
    on_disk = sorted(p.relative_to(pi).as_posix() for p in (pi / "test/e2e").glob("*.e2e.test.ts"))
    for rel, spec in manifest["pi"].items():
        if tags and spec["scenario"] not in tags:
            continue
        report["pi"][rel] = run_node_file(node, pi, rel) if (pi / rel).exists() else None
    failures += judge(manifest, res, report["pi"], tags, on_disk)
    failures += xfail_violations({p.name: p.read_text() for p in sorted((harness.E2E).glob("test_*.py"))})

    # ---- table: pass / strict-xfail / GAP are separate columns; a GAP test passes by asserting the CURRENT, incomplete behaviour
    print("\nscenario  runner  file                                              tests  pass  xfail  GAP  seconds")
    tot = {"pass": 0, "xfail": 0, "gap": 0}
    for fname, spec in manifest["herdr"].items():
        row = res["files"].get(Path(fname).name)
        if row:
            tot["pass"] += row["passed"]
            tot["xfail"] += row["xfailed"]
            print(f"{spec['scenario']:<9} pytest  {fname:<48} {row['tests']:>5} {row['passed']:>5} {row['xfailed']:>6} {'-':>4} {row['seconds']:>8}")
    for rel, r in report["pi"].items():
        if r is None:
            continue
        g = len(r["gaps"])
        tot["pass"] += r["passed"] - g
        tot["gap"] += g
        print(f"{manifest['pi'][rel]['scenario']:<9} node    {rel:<48} {r['tests']:>5} {r['passed'] - g:>5} {'-':>6} {g:>4} {r['seconds']:>8}")
    print(f"\nTOTAL: {tot['pass']} pass, {tot['xfail']} strict-xfail, {tot['gap']} GAP")
    out = Path(args.results)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"failures": failures, **report}, indent=1))
    print(f"\nresults written to {out}")
    if failures:
        print("\nACCEPTANCE RUN FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    known = tot["xfail"] + tot["gap"]
    print(f"\nACCEPTANCE RUN COMPLETE: no failures, every suite ran its expected tests. {known} KNOWN GAPS remain "
          f"({tot['xfail']} strict-xfail + {tot['gap']} GAP-named tests): those mark features that are NOT wired, "
          "not features that work; see docs/operations/acceptance-evidence.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
