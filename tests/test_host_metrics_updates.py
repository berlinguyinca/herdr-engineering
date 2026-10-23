"""Host metrics: updates, failed units, versions."""
import os
import platform
import shutil
from pathlib import Path

from herdr_engineering import host_metrics
from herdr_engineering.host_metrics import (
    parse_apt_simulate, parse_brew_outdated, read_updates, read_versions)

FX = Path(__file__).parent / "fixtures" / "host_metrics"


def _fx(name):
    return (FX / name).read_text()


def _env(monkeypatch, system, tools, outputs, flag=None):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(shutil, "which", lambda t: f"/usr/bin/{t}" if t in tools else None)
    monkeypatch.setattr(host_metrics, "_REBOOT_REQUIRED", str(flag) if flag else "/nonexistent")
    monkeypatch.setattr(host_metrics, "_run",
                        lambda cmd, timeout=5.0: outputs.get(os.path.basename(cmd[0])))


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
                        lambda cmd, timeout=5.0: seen.setdefault(cmd[0], timeout) and None)
    read_updates()
    assert seen["apt-get"] >= 30


def test_read_updates_macos_brew(monkeypatch):
    _env(monkeypatch, "Darwin", {"brew"}, {"brew": _fx("brew_outdated.json")})
    r = read_updates()
    assert r["manager"] == "brew" and r["outdated"] == 3
    assert r["reboot_required"] is None and r["failed_units"] == []


def test_versions_missing_tool_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda t: None)
    monkeypatch.setattr(host_metrics, "_run", lambda cmd, timeout=5.0: None)
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(tmp_path / "nope"))
    v = read_versions()
    assert v["pi"] is None and v["autospec"] is None and v["tailscale"] is None
    assert v["pi_extensions"] == {}
    assert set(v) == {"herdr", "pi", "pi_extensions", "autospec", "tailscale"}


def test_versions_parsed(monkeypatch, tmp_path):
    ext = tmp_path / "ext"
    (ext / "pkg").mkdir(parents=True)
    (ext / "pkg" / "package.json").write_text('{"version": "1.2.3"}')
    (ext / "build-gate.ts").write_text("// x")
    monkeypatch.setattr(host_metrics, "_PI_EXT_DIR", str(ext))
    out = {"pi": "0.9.1\n", "autospec": "autospec 2.0\n",
           "tailscale": "1.76.0\n  tailscale commit: abc\n"}
    monkeypatch.setattr(shutil, "which", lambda t: f"/bin/{t}")
    monkeypatch.setattr(host_metrics, "_run", lambda cmd, timeout=5.0: out.get(cmd[0]))
    v = read_versions()
    assert v["pi"] == "0.9.1" and v["tailscale"] == "1.76.0"
    assert v["autospec"] == "autospec 2.0"
    assert v["pi_extensions"] == {"build-gate.ts": None, "pkg": "1.2.3"}
    assert v["herdr"]


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
