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
    for k in ("cpu", "mem", "swap", "uptime_s", "net_rx_bps", "net_tx_bps", "mounts"):
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


def _linux(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    s = HostSampler()
    return s


def test_disk_independent_of_proc_mounts_and_falls_back_to_ancestor(monkeypatch, tmp_path):
    s = _linux(monkeypatch)
    monkeypatch.setattr(host_metrics, "_herdr_data_dir", lambda: str(tmp_path / "missing" / "deeper"))
    seen = []
    real = os.statvfs

    def statvfs(p):
        seen.append(p)
        return real(p)
    monkeypatch.setattr(s, "_statvfs", statvfs)

    def read(p):
        raise OSError("no")
    monkeypatch.setattr(s, "_read", read)
    out = s.sample()
    assert "disk" in out and "mounts" not in out
    assert seen[0] == str(tmp_path)


def test_mounts_dedupe_by_device_sorted_and_capped(monkeypatch):
    s = _linux(monkeypatch)
    lines = []
    for i in range(10):
        lines.append(f"/dev/d{i} /m/{i:02d} ext4 rw 0 0")
        lines.append(f"/dev/d{i} /m/{i:02d}/bind ext4 rw 0 0")
    lines.append("/dev/d0 /a-first-bind ext4 rw 0 0")
    monkeypatch.setattr(s, "_read", lambda p: "\n".join(lines) if p == "/proc/mounts" else (_ for _ in ()).throw(OSError()))

    class V:
        f_blocks, f_frsize, f_bfree = 1000, 1, 500
    monkeypatch.setattr(s, "_statvfs", lambda p: V)
    devs = {}

    def st_dev(p):
        key = p.replace("/bind", "").replace("/a-first-bind", "/m/00")
        return devs.setdefault(key, len(devs))
    monkeypatch.setattr(s, "_st_dev", st_dev)
    mounts = s.sample()["mounts"]
    paths = [m["path"] for m in mounts]
    assert len(mounts) == 8
    assert paths == sorted(paths)
    assert "/m/00/bind" not in paths and "/m/01/bind" not in paths
    assert all("_dev" not in m for m in mounts)


def test_macos_cpu_uses_last_top_sample(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    s = HostSampler()
    top = ("CPU usage: 10.0% user, 5.0% sys, 85.0% idle\n"
           "CPU usage: 20.0% user, 10.0% sys, 70.0% idle\n")

    def run(argv):
        if argv[0] == "top":
            assert argv == ["top", "-l", "2", "-n", "0", "-s", "1"]
            return top
        raise OSError()
    monkeypatch.setattr(s, "_run", run)
    assert s.sample()["cpu"] == 30.0


def test_meminfo_without_memavailable_falls_back(monkeypatch):
    s = _linux(monkeypatch)
    txt = ("MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 300 kB\n"
           "SwapTotal: 100 kB\nSwapFree: 50 kB\n")
    monkeypatch.setattr(s, "_read", lambda p: txt if p == "/proc/meminfo" else (_ for _ in ()).throw(OSError()))
    out = s.sample()
    assert out["mem"] == 50.0 and out["swap"] == 50.0


def test_net_rate_and_counter_reset(monkeypatch):
    s = _linux(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(host_metrics.time, "monotonic", lambda: clock[0])
    state = {"rx": 1000, "tx": 500}

    def read(p):
        if p == "/proc/net/dev":
            return ("h1\nh2\n  lo: 999 0 0 0 0 0 0 0 999 0\n"
                    f"eth0: {state['rx']} 0 0 0 0 0 0 0 {state['tx']} 0\n")
        raise OSError()
    monkeypatch.setattr(s, "_read", read)
    assert "net_rx_bps" not in s.sample()
    clock[0], state["rx"], state["tx"] = 102.0, 5000, 1500
    out = s.sample()
    assert out["net_rx_bps"] == 2000 and out["net_tx_bps"] == 500
    clock[0], state["rx"], state["tx"] = 104.0, 10, 10
    out = s.sample()
    assert "net_rx_bps" not in out and "net_tx_bps" not in out
