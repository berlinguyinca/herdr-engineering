"""Host metrics: GPU, top consumers, agent count."""
import subprocess
from pathlib import Path

from herdr_engineering import host_metrics
from herdr_engineering.host_metrics import (
    HostSampler, parse_apple_gpu, parse_nvidia_smi, parse_ps, parse_rocm_smi)

FX = Path(__file__).parent / "fixtures" / "host_metrics"


def _fx(name):
    return (FX / name).read_text()


def test_parse_nvidia_two_cards():
    g = parse_nvidia_smi(_fx("nvidia_smi_two.csv"))
    assert len(g) == 2
    assert g[0] == {"name": "NVIDIA GeForce RTX 4090", "util_pct": 37.0,
                    "vram_used_b": 10240 * 1024**2, "vram_total_b": 24564 * 1024**2,
                    "temp_c": 61.0}
    assert g[1]["util_pct"] is None and g[1]["temp_c"] == 48.0


def test_parse_rocm():
    g = parse_rocm_smi(_fx("rocm_smi.json"))
    assert [x["name"] for x in g] == ["AMD Radeon RX 7900 XTX"] * 2
    assert g[0]["util_pct"] == 12.0 and g[0]["temp_c"] == 45.0
    assert g[0]["vram_used_b"] == 1073741824 and g[0]["vram_total_b"] == 25753026560


def test_parse_apple_has_name_and_no_util():
    g = parse_apple_gpu(_fx("system_profiler_apple_gpu.json"))
    assert g[0]["name"] == "Apple M2 Pro" and g[0]["cores"] == 19
    assert g[0]["util_pct"] is None and g[0]["temp_c"] is None


def test_parse_ps_top_five_by_cpu_and_mem():
    rows = "\n".join(f"{i} u{i} {i}.0 {100 - i}.0 cmd{i}" for i in range(1, 11))
    t = parse_ps(rows)
    assert [e["pid"] for e in t["cpu"]] == [10, 9, 8, 7, 6]
    assert [e["pid"] for e in t["mem"]] == [1, 2, 3, 4, 5]
    assert set(t["cpu"][0]) == {"pid", "command", "user", "cpu_pct", "mem_pct", "owner"}


def test_parse_ps_fixture_with_spaces_in_command():
    t = parse_ps(_fx("ps_output.txt"))
    assert t["cpu"][0]["command"] == "node"
    assert t["mem"][0]["command"] == "python3"
    assert any(e["command"] == "Web Content" for e in t["cpu"])


def test_owner_comes_from_agent_lookup(monkeypatch):
    s = HostSampler(agent_lookup=lambda pid: "pi:mission-1" if pid == 42 else None,
                    agents_running=lambda: 3)
    monkeypatch.setattr(s, "_run", lambda argv: _fx("ps_output.txt") if argv[0] == "ps"
                        else (_ for _ in ()).throw(FileNotFoundError(argv[0])))
    out = s.sample()
    top = {e["pid"]: e for e in out["top"]["cpu"]}
    assert top[42]["owner"] == "pi:mission-1"
    assert top[1]["owner"] is None
    assert out["agents_running"] == 3


def test_gpu_tool_missing_omits_key(monkeypatch):
    s = HostSampler()

    def run(argv):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(s, "_run", run)
    out = s.sample()
    assert "gpus" not in out and "top" not in out
    assert out["agents_running"] == 0


def test_gpu_command_timeout_omits_key_and_sample_still_returns(monkeypatch):
    s = HostSampler()

    def run(argv):
        raise subprocess.TimeoutExpired(argv, 5)
    monkeypatch.setattr(s, "_run", run)
    out = s.sample()
    assert "gpus" not in out
    assert "uptime_s" in out or "load1" in out


def test_huge_ps_output_is_capped_at_five():
    rows = "\n".join(f"{i} u {i % 97}.5 {i % 89}.5 c{i}" for i in range(1, 5001))
    t = parse_ps(rows)
    assert len(t["cpu"]) == 5 and len(t["mem"]) == 5


def test_gpus_capped_at_eight(monkeypatch):
    s = HostSampler()
    csv = "\n".join("G, 1, 1, 2, 3" for _ in range(20))
    monkeypatch.setattr(s, "_run", lambda argv: csv if argv[0] == "nvidia-smi"
                        else (_ for _ in ()).throw(FileNotFoundError()))
    assert len(s._gpus()["gpus"]) == 8


def test_module_run_returns_none_on_failure():
    assert host_metrics._run(["definitely-not-a-binary-xyz"]) is None
    assert host_metrics._run(["sleep", "2"], timeout=0.1) is None
    assert host_metrics._run(["echo", "hi"]).strip() == "hi"


def test_apple_util_only_when_sudo_available(monkeypatch):
    monkeypatch.setattr(host_metrics.platform, "system", lambda: "Darwin")
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "system_profiler":
            return _fx("system_profiler_apple_gpu.json")
        if argv[:3] == ["sudo", "-n", "true"]:
            raise subprocess.CalledProcessError(1, argv)
        raise AssertionError(argv)
    s = HostSampler()
    monkeypatch.setattr(s, "_run", run)
    g = s._gpus()["gpus"]
    assert g[0]["util_pct"] is None
    assert not any("powermetrics" in a for c in calls for a in c)

    def run2(argv):
        if argv[0] == "system_profiler":
            return _fx("system_profiler_apple_gpu.json")
        if argv[:3] == ["sudo", "-n", "true"]:
            return ""
        return "GPU HW active residency:  23.5% (338 MHz: 1.2%)\n"
    monkeypatch.setattr(s, "_run", run2)
    assert s._gpus()["gpus"][0]["util_pct"] == 23.5
