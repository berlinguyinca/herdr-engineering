"""Real local control plane for the Build cache page test (micro-spec 10).

``python build-cache-server.py <mode>`` prints "PORT=<n>" once ready, then runs until killed. Modes:
  populated  capability on, 120 builds (completed, failed, running, hostile strings), measured and unmeasured figures
  nolog      capability on, policy on, no builds.jsonl yet
  policyoff  capability on, fabric_build_cache.enabled false, a few builds
  unmeasured capability on, builds that carry no sccache counters or write figures
  off        build_cache not configured: no capability, the routes answer 404
  keyed      populated, but api_keys are required: k-op = operator, k-view = viewer (anything else: 401)
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'
SECTION = {"enabled": True, "local": {"capacity_gib": 1}, "eviction": {"hard_cap_gib": 1, "max_entries_per_run": 50},
           "cleanup": {"min_idle_hours": 1, "max_evict_per_pass_gib": 1}}


def _rows(mode):
    rows = []
    n = 120 if mode in ("populated", "keyed") else 3
    for i in range(n):
        bid = f"b{i:03d}"
        hostile = i == n - 1
        started = {"event": "build_started", "ts": f"2026-11-04T00:00:{i % 60:02d}Z", "build_id": bid,
                   "host": HOSTILE if hostile else "h1", "slot": 0, "cmd_kind": "cargo build", "cache": {"applied": True}}
        if i == n - 2 and mode in ("populated", "keyed"):          # still running: no finish line
            rows.append(started)
            continue
        fin = dict(started, event="build_finished", exit_code=1 if i % 17 == 3 else 0, elapsed_s=1.5 + i,
                   unavailable=[], cache={"applied": i % 5 != 0})
        if hostile:
            fin["cmd_kind"] = "cargo " + HOSTILE
            fin["trace"] = {"session": HOSTILE, "mission": "m-" + HOSTILE}
        elif i % 4 == 3:
            fin["trace"] = {"session": f"sess-{i}", "mission": f"msn-{i}"}
        if mode == "unmeasured":
            fin.update(sccache_stats=None, unavailable=["sccache_stats", "host_disk_write_bytes_delta"])
        else:
            fin.update(sccache_stats={"cache_hits.counts.Rust": 3 * (i % 4 + 1), "cache_misses.counts.Rust": 1 + i % 3},
                       proc_io_write_bytes=1_000_000 * (i + 1), host_disk_write_bytes_delta=(2_000_000 * (i + 1)) if i % 2 == 0 else None,
                       host_disk_scope="sda")
        if mode == "populated" and i == 2:
            fin.update(sccache_stats=None, unavailable=["sccache_stats"])
        rows.extend([started, fin])
    return rows


def main(mode):
    tmp = Path(tempfile.mkdtemp(prefix="herdr-build-cache-ui-"))
    state = tmp / "state"
    state.mkdir()
    if mode != "nolog" and mode != "off":
        with open(state / "builds.jsonl", "w") as f:
            for r in _rows(mode):
                f.write(json.dumps(r) + "\n")
    section = dict(SECTION, enabled=(mode != "policyoff"))
    cfg = None if mode == "off" else {"enabled": True, "state_dir": str(state), "fabric_build_cache": section}
    keys = {"k-op": "operator", "k-view": "viewer"} if mode == "keyed" else None
    server = ControlPlaneServer(ControlPlaneRepo(), web_root=ROOT / "packages", api_keys=keys, build_cache=cfg)
    server.ci_monitor.close()
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    print(f"PORT={server.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "populated")
