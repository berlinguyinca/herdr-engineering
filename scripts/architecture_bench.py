#!/usr/bin/env python3
"""Measure the architecture analyzer and query path on a real checkout. Reports actual numbers only.

    python3 scripts/architecture_bench.py [--repo .] [--rev HEAD] [--runs 5] [--out report.json]
"""
import argparse
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herdr_engineering.architecture import analyze, identity, runner  # noqa: E402
from herdr_engineering.architecture.service import ArchitectureService  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="."); ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--repository-id", default="github.com/berlinguyinca/herdr-engineering")
    ap.add_argument("--runs", type=int, default=5); ap.add_argument("--out")
    a = ap.parse_args()
    errors = 0
    cold = []
    for _ in range(a.runs):                       # isolated child process = what a host actually pays
        t = time.perf_counter()
        try:
            res = runner.run_isolated(a.repo, a.rev, "bench", a.repository_id)
        except runner.AnalysisError:
            errors += 1; continue
        cold.append(time.perf_counter() - t)
    child_peak_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss   # largest child, KiB on Linux
    warm = []
    for _ in range(a.runs):                       # in-process: page cache + imports warm, no spawn
        t = time.perf_counter()
        analyze.analyze_commit(a.repo, a.rev, "bench", a.repository_id)
        warm.append(time.perf_counter() - t)
    snap, graph = res["snapshot"], res["graph"]
    svc = ArchitectureService(ControlPlaneRepo(), enabled=True)
    t = time.perf_counter(); svc.publish(res); publish_s = time.perf_counter() - t
    sid = snap["snapshot_id"]
    root_pkgs = [n["node_id"] for n in graph["nodes"] if n["kind"] == "package" and n["parent_id"] == "repo:root"]
    mods = [n["node_id"] for n in graph["nodes"] if n["kind"] == "module"]
    lat = {"children": [], "search": [], "expand": [], "node": []}
    for i in range(200):
        t = time.perf_counter(); svc.children(sid, root_pkgs[i % len(root_pkgs)] if root_pkgs else None); lat["children"].append(time.perf_counter() - t)
        t = time.perf_counter(); svc.search(sid, "api"); lat["search"].append(time.perf_counter() - t)
        m = mods[i % len(mods)]
        t = time.perf_counter(); svc.expand(sid, m); lat["expand"].append(time.perf_counter() - t)
        t = time.perf_counter(); svc.node(sid, m); lat["node"].append(time.perf_counter() - t)
    report = {
        "repo": os.path.realpath(a.repo), "rev": snap["commit_sha"], "runs": a.runs, "errors": errors,
        "graph": {"nodes": snap["counts"]["nodes"], "edges": snap["counts"]["edges"], "python_files": snap["counts"]["python_files"],
                  "canonical_json_bytes": len(identity.canonical_bytes(graph)), "completeness": snap["completeness"]},
        "analysis_seconds": {"cold_isolated_median": statistics.median(cold), "cold_isolated_max": max(cold),
                             "warm_in_process_median": statistics.median(warm)},
        "peak_memory_mib_largest_child": round(child_peak_kib / 1024, 1),
        "publish_validate_store_seconds": publish_s,
        "query_latency_ms": {k: {"p50": round(statistics.median(v) * 1000, 3), "p95": round(pct(v, .95) * 1000, 3),
                                 "max": round(max(v) * 1000, 3)} for k, v in lat.items()},
        "incremental": "not implemented: every analysis is a full baseline; no parity or speedup measured",
        "network_isolated": res["sandbox"]["network_isolated"],
    }
    print(json.dumps(report, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=2))


main()
