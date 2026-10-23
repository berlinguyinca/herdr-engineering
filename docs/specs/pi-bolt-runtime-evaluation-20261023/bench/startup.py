#!/usr/bin/env python3
"""Startup benchmark: wall, CPU seconds, peak RSS per run, arms interleaved to share host noise.

usage: startup.py --scenario version|help|noop-print --reps 10 --out results/x.jsonl
Isolation: every arm gets a fresh PI_CODING_AGENT_DIR (empty settings, no extensions) so no
fleet state, session or credential is touched. Identical env across arms.
"""
import argparse, json, os, resource, statistics, subprocess, sys, tempfile, time, pathlib

SC = {
    "version": ["--version"],
    "help": ["--help"],
}

def run(argv, env):
    t0 = time.perf_counter()
    p = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _, status, ru = os.wait4(p.pid, 0)
    wall = time.perf_counter() - t0
    return {"wall_s": wall, "cpu_s": ru.ru_utime + ru.ru_stime, "maxrss_kib": ru.ru_maxrss, "exit": os.waitstatus_to_exitcode(status)}

def pct(xs, q):
    xs = sorted(xs); k = (len(xs) - 1) * q; f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="version"); ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out", required=True); ap.add_argument("--warmups", type=int, default=2)
    a = ap.parse_args()
    here = pathlib.Path(__file__).parent
    arms = json.loads((here / "runtimes.json").read_text())
    tail = SC[a.scenario]
    host = {"host": os.uname().nodename, "loadavg": os.getloadavg(), "when": time.strftime("%FT%T%z")}
    rows = []
    with tempfile.TemporaryDirectory() as agent:
        pathlib.Path(agent, "settings.json").write_text("{}")
        env = {"PATH": "/usr/bin:/bin", "HOME": agent, "PI_CODING_AGENT_DIR": agent, "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1"}
        # first measured pass of each arm after unpack = 'cold-ish' (page cache not dropped: no root)
        for i in range(a.warmups + a.reps):
            for name, arm in arms.items():
                r = run(arm["argv"] + tail, env)
                phase = "warmup" if i < a.warmups else "measure"
                rows.append({"arm": name, "scenario": a.scenario, "rep": i, "phase": phase, **r, **host})
    pathlib.Path(a.out).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    for name in arms:
        m = [r for r in rows if r["arm"] == name and r["phase"] == "measure"]
        w = [r["wall_s"] * 1000 for r in m]
        print(f'{name:12s} n={len(m)} wall ms median={statistics.median(w):7.1f} p95={pct(w,.95):7.1f} '
              f'cpu s med={statistics.median(r["cpu_s"] for r in m):.3f} rss MiB med={statistics.median(r["maxrss_kib"] for r in m)/1024:.0f} '
              f'exit={sorted({r["exit"] for r in m})}')
main()
