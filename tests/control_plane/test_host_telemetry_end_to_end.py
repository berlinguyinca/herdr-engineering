"""Collector -> agent payload -> redaction -> ingest -> repo -> host read, end to end.

The sampler, the slow readers and the fabric agent's envelope are the real code;
only the operating system is replaced by fixtures (``/proc``, ``ps``,
``nvidia-smi``, ``apt-get``, ``statvfs``, clocks), so the result is the same on
any machine. The host + history JSON the page renders is committed under
``tests/fixtures/host_telemetry/`` and the node test
``packages/herdr-web-components/host-view.test.js`` renders it; this test fails
loudly when the pipeline output drifts from that committed copy.
"""
import json
import os
import types
from pathlib import Path

import pytest

import herdr_engineering.version as version_mod
from herdr_engineering import host_metrics
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.redact import redact_ingest_body
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fabric_agent import build_host_report

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "fixtures" / "host_metrics"
GOLDEN = ROOT / "fixtures" / "host_telemetry" / "host_detail.json"
T0 = 1_800_000_000.0
GIB = 1024 ** 3


def _iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat()


def _fx(name):
    return (FX / name).read_text()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _Vfs:
    def __init__(self, total, free):
        self.f_blocks, self.f_bfree, self.f_frsize = total // 4096, free // 4096, 4096


_DISKS = {"/": _Vfs(1000 * GIB, 600 * GIB), "/data": _Vfs(4000 * GIB, 3000 * GIB),
          "/boot/efi": _Vfs(GIB, GIB // 2)}
_VERSIONS = {"herdr": "herdr 0.9.1", "pi": "0.70.2", "autospec": "autospec 2.1.0",
             "tailscale": "1.80.2\n  tailscale commit: abc"}


@pytest.fixture
def linux(monkeypatch, tmp_path):
    """A deterministic Linux host: every OS read comes from a fixture."""
    monkeypatch.setattr(host_metrics.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_metrics.os, "getloadavg", lambda: (1.5, 1.25, 1.0))
    mono = Clock(1000.0)
    monkeypatch.setattr(host_metrics, "time", types.SimpleNamespace(monotonic=mono, time=lambda: T0))
    monkeypatch.setattr(host_metrics, "_herdr_data_dir", lambda: "/")
    # slow readers: apt (clean), systemd (no failed units), tool versions
    tools = {"apt-get", "systemctl", "herdr", "pi", "autospec", "tailscale"}
    monkeypatch.setattr(host_metrics.shutil, "which",
                        lambda t: f"/usr/bin/{t}" if t in tools else None)
    monkeypatch.setattr(host_metrics, "_REBOOT_REQUIRED", str(tmp_path / "no-reboot"))
    outputs = {"apt-get": _fx("apt_simulate_clean.txt"), "systemctl": "", **_VERSIONS}
    monkeypatch.setattr(host_metrics, "_run",
                        lambda cmd, timeout=5.0, env=None: outputs.get(os.path.basename(cmd[0])))
    monkeypatch.setattr(version_mod, "__version__", "0.0.0+e2e")
    ext = tmp_path / "extensions"
    for name, ver in (("auth-helper", "1.2.0"), ("token-tools", "0.3.0")):
        (ext / name).mkdir(parents=True)
        (ext / name / "package.json").write_text(json.dumps({"version": ver}))
    (ext / "build-gate.ts").write_text("")
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(ext))

    files = {"/proc/stat": _fx("proc_stat_1.txt"), "/proc/meminfo": _fx("meminfo.txt"),
             "/proc/uptime": _fx("proc_uptime.txt"), "/proc/net/dev": _fx("proc_net_dev_1.txt"),
             "/proc/mounts": _fx("proc_mounts.txt")}
    runs = {"ps": _fx("ps_agents.txt"), "nvidia-smi": _fx("nvidia_smi_two.csv")}
    s = host_metrics.HostSampler(agent_lookup=lambda pid: "dev:5173" if pid == 501 else None)
    s._read = lambda p: files[p]

    def run(argv):
        if argv[0] not in runs:
            raise FileNotFoundError(argv[0])
        return runs[argv[0]]
    s._run = run
    s._statvfs = lambda p: _DISKS[p]
    s._st_dev = lambda p: p

    def second_reading():
        files["/proc/stat"] = _fx("proc_stat_2.txt")
        files["/proc/net/dev"] = _fx("proc_net_dev_2.txt")
        mono.t += 30.0
    return s, second_reading


def _pipeline(linux):
    sampler, second_reading = linux
    clock = Clock(T0)
    repo = ControlPlaneRepo(now=clock)
    svc = IngestService(repo)
    slow = {"updates": host_metrics.read_updates(), "versions": host_metrics.read_versions()}

    # first reading: no cpu / network rates yet; the slow report rides along
    svc.ingest(redact_ingest_body(build_host_report(
        "beast", "beast", "100.64.0.7", sampler.sample(), slow)))
    clock.t += 30
    second_reading()
    sample = sampler.sample()
    # what the agent sends, through the same redaction as POST /api/v1/ingest
    svc.ingest(redact_ingest_body(build_host_report("beast", "beast", "100.64.0.7", sample, None)))
    # the main host's fleet update reports pins and its last run
    svc.ingest(redact_ingest_body({
        "source": "fleet-update", "entity_type": "host", "entity_id": "beast",
        "event_type": "host.fleet_update",
        "payload": {"maintenance": False,
                    "pins": {"herdr": "0.9.1", "pi": "0.70.2", "autospec": "2.1.0",
                             "pi_extensions": {"auth-helper": "1.2.0", "token-tools": "0.3.0"}},
                    "fleet_update": {"last_updated": _iso(T0 - 3 * 86400),
                                     "deferred_since": None, "result": "updated"}}}))
    return repo, svc, sample, slow


def _norm(v):
    """Drop ``None`` numbers the store omits (``clean_metrics`` keeps only finite numbers)."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items() if x is not None or k in ("owner",)}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def test_every_sampled_key_survives_into_the_host_record(linux):
    repo, _, sample, slow = _pipeline(linux)
    host = repo.get_host("beast")
    latest = host["latest"]
    # the sample is fully populated (a broken fixture would hide a lost key)
    for key in ("cpu", "mem", "mem_used_b", "mem_total_b", "swap", "disk", "load1", "load5",
                "load15", "uptime_s", "net_rx_bps", "net_tx_bps", "mounts", "gpus", "top",
                "agents_running"):
        assert key in sample, key
    assert set(sample) <= set(latest)
    for key, value in sample.items():
        assert _norm(latest[key]) == _norm(value), key
    assert latest["agents_running"] == 3
    assert latest["at"] == host["last_seen"]
    assert host["updates"] == slow["updates"] and host["versions"] == slow["versions"]
    assert host["versions"]["pi_extensions"] == {"auth-helper": "1.2.0", "build-gate.ts": None,
                                                 "token-tools": "0.3.0"}
    assert host["problems"] == [] and host["problem_count"] == 0


def test_history_points_carry_the_series_the_page_draws(linux):
    repo, *_ = _pipeline(linux)
    history = repo.host_history("beast", "1h")
    assert len(history) == 2
    last = history[-1]
    for key in ("cpu", "mem", "disk", "gpu_util_pct", "ts", "at"):
        assert key in last, key
    assert all("ts" in p and "at" in p for p in history)


def test_registered_only_host_is_never_reported(linux):
    repo, svc, *_ = _pipeline(linux)
    svc.ingest({"entity_type": "host", "entity_id": "fresh", "event_type": "host.registered",
                "payload": {"host_name": "fresh", "tailnet_ip": "100.64.0.8"}})
    codes = [p["code"] for p in repo.get_host("fresh")["problems"]]
    assert codes == ["never_reported"]


def test_committed_page_fixture_matches_the_pipeline(linux, tmp_path):
    repo, *_ = _pipeline(linux)
    actual = {"host": repo.get_host("beast"),
              "history": {"samples": repo.host_history("beast", "1h"), "range": "1h"}}
    text = json.dumps(actual, indent=1, sort_keys=True) + "\n"
    if not GOLDEN.exists() or GOLDEN.read_text() != text:
        out = tmp_path / GOLDEN.name
        out.write_text(text)
        pytest.fail(f"pipeline output differs from {GOLDEN.relative_to(ROOT.parent)}; "
                    f"actual output written to {out} (review it, then copy it over the fixture)")
