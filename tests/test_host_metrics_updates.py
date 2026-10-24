"""Host metrics: updates, failed units, versions."""
import os
import platform
import shutil
from pathlib import Path

from herdr_engineering import host_metrics
from herdr_engineering.host_metrics import (
    parse_apt_simulate,
    parse_brew_outdated,
    read_updates,
    read_versions,
)

FX = Path(__file__).parent / "fixtures" / "host_metrics"


def _fx(name):
    return (FX / name).read_text()


def _env(monkeypatch, system, tools, outputs, flag=None):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(shutil, "which", lambda t: f"/usr/bin/{t}" if t in tools else None)
    monkeypatch.setattr(host_metrics, "_REBOOT_REQUIRED", str(flag) if flag else "/nonexistent")
    monkeypatch.setattr(host_metrics, "_run",
                        lambda cmd, timeout=5.0, env=None: outputs.get(os.path.basename(cmd[0])))


def test_parse_apt_counts_security():
    r = parse_apt_simulate(_fx("apt_simulate.txt"))
    assert r["outdated"] == 3 and r["security"] == 1
    assert r["packages"][0] == {"name": "curl", "from": "7.81.0-1ubuntu1.15",
                                "to": "7.81.0-1ubuntu1.16"}


def test_parse_apt_caps_packages_at_50():
    text = "".join(f"Inst p{i} [1] (2 x/jammy-updates [amd64])\n" for i in range(80))
    r = parse_apt_simulate(text)
    assert r["outdated"] == 80 and len(r["packages"]) == 50


def test_parse_brew():
    r = parse_brew_outdated(_fx("brew_outdated.json"))
    assert r["outdated"] == 3 and r["security"] is None
    assert {"name": "git", "from": "2.44.0", "to": "2.45.1"} in r["packages"]


def test_read_updates_linux_apt_reboot_and_failed(monkeypatch, tmp_path):
    flag = tmp_path / "reboot-required"
    flag.write_text("*** System restart required ***\n")
    _env(monkeypatch, "Linux", {"apt-get", "systemctl"},
         {"apt-get": _fx("apt_simulate.txt"), "systemctl": _fx("systemctl_failed.txt")}, flag)
    r = read_updates()
    assert r["manager"] == "apt" and r["outdated"] == 3 and r["security"] == 1
    assert r["reboot_required"] is True
    assert r["failed_units"] == ["foo.service"]


def test_reboot_not_required_is_false(monkeypatch):
    _env(monkeypatch, "Linux", {"apt-get"}, {"apt-get": _fx("apt_simulate.txt")})
    assert read_updates()["reboot_required"] is False


def test_unknown_package_manager_is_none_not_zero(monkeypatch):
    _env(monkeypatch, "Linux", set(), {})
    r = read_updates()
    assert r["manager"] is None and r["outdated"] is None and r["security"] is None
    assert r["packages"] == [] and r["failed_units"] == []


def test_apt_command_failure_is_unknown(monkeypatch):
    _env(monkeypatch, "Linux", {"apt-get"}, {})
    r = read_updates()
    assert r["manager"] == "apt" and r["outdated"] is None


def test_apt_uses_longer_timeout(monkeypatch):
    seen = {}
    _env(monkeypatch, "Linux", {"apt-get"}, {})
    monkeypatch.setattr(host_metrics, "_run",
                        lambda cmd, timeout=5.0, env=None: seen.setdefault(cmd[0], timeout) and None)
    read_updates()
    assert seen["apt-get"] >= 120


def test_read_updates_macos_brew(monkeypatch):
    _env(monkeypatch, "Darwin", {"brew"}, {"brew": _fx("brew_outdated.json")})
    r = read_updates()
    assert r["manager"] == "brew" and r["outdated"] == 3
    assert r["reboot_required"] is None and r["failed_units"] == []


def test_versions_missing_tool_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda t: None)
    monkeypatch.setattr(host_metrics, "_TOOL_DIRS", (str(tmp_path / "none"),))
    monkeypatch.setattr(host_metrics, "_run", lambda cmd, timeout=5.0, env=None: None)
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(tmp_path / "nope"))
    v = read_versions()
    assert v["pi"] is None and v["autospec"] is None and v["tailscale"] is None
    assert v["herdr"] is None and v["herdr_eng"]
    assert v["pi_extensions"] == {}
    assert set(v) == {"herdr", "herdr_eng", "pi", "pi_extensions", "autospec", "tailscale"}


def test_versions_parsed(monkeypatch, tmp_path):
    ext = tmp_path / "ext"
    (ext / "pkg").mkdir(parents=True)
    (ext / "pkg" / "package.json").write_text('{"version": "1.2.3"}')
    (ext / "build-gate.ts").write_text("// x")
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(ext))
    out = {"herdr": "herdr 0.9.1\n", "pi": "0.9.1\n", "autospec": "autospec 2.0\n",
           "tailscale": "1.76.0\n  tailscale commit: abc\n"}
    monkeypatch.setattr(shutil, "which", lambda t: f"/bin/{t}")
    monkeypatch.setattr(host_metrics, "_run",
                        lambda cmd, timeout=5.0, env=None: out.get(os.path.basename(cmd[0])))
    v = read_versions()
    assert v["pi"] == "0.9.1" and v["tailscale"] == "1.76.0"
    assert v["autospec"] == "2.0"
    assert v["pi_extensions"] == {"build-gate.ts": None, "pkg": "1.2.3"}
    assert v["herdr"] == "0.9.1"
    assert v["herdr_eng"]


def test_version_token():
    from herdr_engineering.host_metrics import _version
    assert _version("herdr 0.9.1") == "0.9.1"
    assert _version("pi v1.4.0-rc.1 (build abc)") == "1.4.0-rc.1"
    assert _version("2.1.306 (Claude Code)") == "2.1.306"
    assert _version("autospec dev") is None
    assert _version("") is None and _version(None) is None


def test_failed_unit_name_starting_with_x(monkeypatch):
    _env(monkeypatch, "Linux", {"systemctl"}, {"systemctl": "x11-foo.service loaded failed failed X\n"})
    assert read_updates()["failed_units"] == ["x11-foo.service"]


def test_apt_empty_and_garbage_are_unknown():
    for text in ("", "W: proxy said no\nLocale: cs_CZ\n"):
        r = parse_apt_simulate(text)
        assert r["manager"] == "apt" and r["outdated"] is None
        assert r["security"] is None and r["packages"] == []


def test_apt_zero_upgraded_is_zero():
    r = parse_apt_simulate("Reading package lists...\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n")
    assert r["outdated"] == 0 and r["security"] == 0


def test_security_suite_token_is_tight():
    r = parse_apt_simulate("Inst a [1] (2 x/jammy-security-extras [amd64])\n"
                           "Inst b [1] (2 x/jammy-security [amd64])\n")
    assert r["security"] == 1


def test_reboot_none_when_not_apt(monkeypatch, tmp_path):
    flag = tmp_path / "rr"
    flag.write_text("x")
    _env(monkeypatch, "Linux", {"dnf"}, {}, flag)
    assert read_updates()["reboot_required"] is None


def test_brew_found_via_fallback_path(monkeypatch):
    _env(monkeypatch, "Darwin", set(), {"brew": _fx("brew_outdated.json")})
    monkeypatch.setattr(host_metrics, "_BREW_PATHS", ("/opt/homebrew/bin/brew",))
    monkeypatch.setattr(os, "access", lambda p, m: p == "/opt/homebrew/bin/brew")
    r = read_updates()
    assert r["manager"] == "brew" and r["outdated"] == 3


def test_brew_non_json_and_timeout_are_unknown(monkeypatch):
    for out in ({"brew": "Error: not json"}, {}):
        _env(monkeypatch, "Darwin", {"brew"}, out)
        r = read_updates()
        assert r["manager"] == "brew" and r["outdated"] is None and r["packages"] == []


def test_versions_find_tools_in_local_bin_without_path(monkeypatch, tmp_path):
    """systemd user units have no ~/.local/bin on PATH: still found there."""
    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    for name, line in (("herdr", "herdr 0.9.1"), ("pi", "pi 1.4.0"), ("autospec", "autospec v2.3.1")):
        stub = bindir / name
        stub.write_text(f"#!/bin/sh\necho '{line}'\n")
        stub.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(host_metrics, "_TOOL_DIRS", ("~/.local/bin", str(tmp_path / "usr-local-bin")))
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(tmp_path / "nope"))
    v = read_versions()
    assert v["herdr"] == "0.9.1" and v["pi"] == "1.4.0" and v["autospec"] == "2.3.1"


def test_package_scan_timeout_is_two_minutes():
    assert host_metrics._PKG_TIMEOUT == 120.0


def test_apt_runs_with_c_locale_keeping_path(monkeypatch):
    seen = {}
    _env(monkeypatch, "Linux", {"apt-get"}, {})

    def fake(cmd, timeout=5.0, env=None):
        seen[os.path.basename(cmd[0])] = env
    monkeypatch.setattr(host_metrics, "_run", fake)
    monkeypatch.setenv("PATH", "/custom/bin:/usr/bin")
    read_updates()
    env = seen["apt-get"]
    assert env["LC_ALL"] == "C"
    assert env["PATH"] == "/custom/bin:/usr/bin"  # the rest of the environment is kept


def test_run_passes_env_to_subprocess(monkeypatch):
    import subprocess
    got = {}

    def fake_run(cmd, **kw):
        got.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert host_metrics._run(["x"], env={"LC_ALL": "C"}) == "ok"
    assert got["env"] == {"LC_ALL": "C"}


def test_failed_units_are_capped(monkeypatch):
    out = "".join(f"u{i}.service loaded failed failed U{i}\n" for i in range(200))
    _env(monkeypatch, "Linux", {"systemctl"}, {"systemctl": out})
    units = read_updates()["failed_units"]
    assert len(units) == host_metrics.MAX_PACKAGES
    assert units[0] == "u0.service"


def test_pi_extensions_are_capped_and_names_truncated(monkeypatch, tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    for i in range(80):
        (ext / f"e{i:03d}.ts").write_text("")
    long = ext / ("x" * 200)
    long.mkdir()
    (long / "package.json").write_text('{"version": "' + "9" * 300 + '"}')
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(ext))
    got = host_metrics._pi_extensions()
    assert len(got) == host_metrics.MAX_PI_EXTENSIONS == 50
    assert all(len(k) <= 64 for k in got)
    assert all(v is None or len(v) <= 64 for v in got.values())


def test_worst_case_slow_payload_fits_the_control_plane_limit(monkeypatch, tmp_path):
    """Every slow field is bounded so ``clean_state`` (16 KiB) never rejects it."""
    import json

    from herdr_engineering.control_plane.host_metrics_store import MAX_STATE_BYTES
    long = "p" * 200
    apt = "".join(f"Inst {long}{i} [{long}] ({long} x/jammy-security [amd64])\n"
                  for i in range(300))
    units = "".join(f"{long}{i}.service loaded failed failed X\n" for i in range(300))
    _env(monkeypatch, "Linux", {"apt-get", "systemctl"}, {"apt-get": apt, "systemctl": units})
    u = read_updates()
    assert len(json.dumps(u)) <= MAX_STATE_BYTES
    ext = tmp_path / "ext"
    ext.mkdir()
    for i in range(300):
        (ext / f"{long}{i}").mkdir()
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(ext))
    assert len(json.dumps(host_metrics._pi_extensions())) <= MAX_STATE_BYTES
