"""Host metrics core sampler tests."""
import os
import platform
from pathlib import Path

from herdr_engineering import host_metrics
from herdr_engineering.host_metrics import HostSampler, parse_meminfo, parse_proc_stat

FX = Path(__file__).parent / "fixtures" / "host_metrics"


def _fx(name):
    return (FX / name).read_text()


def test_parse_helpers():
    assert parse_proc_stat(_fx("proc_stat_1.txt")) == (200, 1000)
    assert parse_meminfo(_fx("meminfo.txt"))["MemTotal"] == 16 * 1024**3


def test_cpu_percent_from_two_proc_stat_readings(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    s = HostSampler()
    files = {"/proc/stat": _fx("proc_stat_1.txt")}
    monkeypatch.setattr(s, "_read", lambda p: files[p])
    first = s.sample()
    assert "cpu" not in first and "net_rx_bps" not in first
    files["/proc/stat"] = _fx("proc_stat_2.txt")
    assert s.sample()["cpu"] == 50.0


def test_meminfo_percent_uses_memavailable(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    s = HostSampler()
    monkeypatch.setattr(s, "_read", lambda p: _fx("meminfo.txt") if p == "/proc/meminfo" else (_ for _ in ()).throw(OSError()))
    out = s.sample()
    assert out["mem"] == 75.0
    assert out["mem_total_b"] == 16 * 1024**3
    assert out["swap"] == 50.0


def test_missing_proc_files_omit_fields(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    s = HostSampler()

    def boom(_p):
        raise OSError("nope")
    monkeypatch.setattr(s, "_read", boom)
    out = s.sample()
    for k in ("cpu", "mem", "swap", "uptime_s", "net_rx_bps", "net_tx_bps", "mounts", "disk"):
        assert k not in out


def test_macos_branch_uses_vm_stat_and_sysctl(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    s = HostSampler()
    boot = int(host_metrics.time.time()) - 3600

    def run(argv):
        if argv[0] == "vm_stat":
            return _fx("vm_stat.txt")
        if argv[:2] == ["sysctl", "-n"]:
            if argv[2] == "hw.memsize":
                return str(16 * 1024**3)
            if argv[2] == "vm.swapusage":
                return _fx("swapusage.txt")
            if argv[2] == "kern.boottime":
                return "{ sec = %d, usec = 0 } Mon" % boot
        raise OSError("unavailable")
    monkeypatch.setattr(s, "_run", run)
    out = s.sample()
    assert 0 < out["mem"] < 100
    assert out["swap"] == 50.0
    assert 3599 <= out["uptime_s"] <= 3700
    assert "cpu" not in out


def test_real_sample_never_raises():
    out = HostSampler().sample()
    assert isinstance(out, dict)
    if platform.system() == "Linux":
        assert "mem" in out and "uptime_s" in out
        assert len(out.get("mounts", [])) <= 8
