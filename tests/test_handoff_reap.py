"""The periodic `herdr-handoff reap --apply` job: policy from YAML, the exact command it runs, the units that
schedule it, and the real herdr-handoff CLI against a real (temporary) shared store."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import handoff_reap
from herdr_engineering.handoff_reap import Policy, load_policy, reap_argv

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INSTALLER = SCRIPTS / "install-handoff-reap.sh"
FORBIDDEN = "--include-unrecorded"
HANDOFF = shutil.which("herdr-handoff")


def write_config(home: Path, body: str) -> None:
    path = home / ".config" / "herdr-engineering" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_disabled_by_default_with_a_fifteen_minute_interval(tmp_path: Path) -> None:
    policy = load_policy(home=tmp_path)
    assert policy == Policy(enabled=False, interval_minutes=15, legacy_roots=())


def test_policy_comes_from_the_user_yaml(tmp_path: Path) -> None:
    write_config(tmp_path, "handoff_reap:\n  enabled: true\n  interval_minutes: 30\n  legacy_roots: [~/a, /b]\n")
    policy = load_policy(home=tmp_path)
    assert policy.enabled and policy.interval_minutes == 30
    assert policy.legacy_roots == (tmp_path / "a", Path("/b"))


@pytest.mark.parametrize("body", [
    "handoff_reap:\n  enabled: yes please\n",
    "handoff_reap:\n  interval_minutes: 7\n",        # must divide the hour: the timer is calendar based
    "handoff_reap:\n  interval_minutes: 0\n",
    "handoff_reap:\n  enable: true\n",                # a misspelled key must not be ignored
    "handoff_reap:\n  legacy_roots: nope\n",
    "handoff_reap: [1]\n",
])
def test_a_malformed_section_fails_closed_and_disabled(tmp_path: Path, body: str) -> None:
    write_config(tmp_path, body)
    policy = load_policy(home=tmp_path)
    assert not policy.enabled and policy.config_error


def test_the_command_never_includes_unrecorded() -> None:
    assert reap_argv("herdr-handoff") == ["herdr-handoff", "reap", "--apply"]
    assert FORBIDDEN not in " ".join(reap_argv("x"))
    source = Path(handoff_reap.__file__).read_text()
    assert source.count(FORBIDDEN) <= 1  # only the explanatory comment names it; nothing builds it
    assert f'"{FORBIDDEN}"' not in source and f"'{FORBIDDEN}'" not in source


def test_no_shipped_unit_or_script_passes_include_unrecorded() -> None:
    for name in ("herdr-eng-handoff-reap.service", "herdr-eng-handoff-reap.timer",
                 "com.herdr-engineering.handoff-reap.plist", "install-handoff-reap.sh"):
        assert FORBIDDEN not in (SCRIPTS / name).read_text(), name
    assert FORBIDDEN not in (ROOT / "ansible/tasks/handoff-reap.yml").read_text()


def test_disabled_policy_runs_nothing(tmp_path: Path) -> None:
    report = handoff_reap.run(load_policy(home=tmp_path), home=tmp_path)
    assert report["status"] == "disabled"


def test_a_missing_cli_is_reported_not_raised(tmp_path: Path) -> None:
    write_config(tmp_path, "handoff_reap:\n  enabled: true\n")
    report = handoff_reap.run(load_policy(home=tmp_path), home=tmp_path, handoff_bin="/nonexistent/herdr-handoff")
    assert report["status"] == "error" and "herdr-handoff" in report["reason"]


@pytest.mark.skipif(HANDOFF is None, reason="herdr-handoff is not installed")
def test_enabled_run_applies_reap_against_the_shared_store_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real CLI, with HOME moved: it must read <home>/.local/share/herdr-handoff/store and ignore a
    HERDR_HANDOFF_DIR / HERDR_HANDOFF_FILE inherited from the pane that started us."""
    write_config(tmp_path, "handoff_reap:\n  enabled: true\n")
    decoy = tmp_path / "decoy"
    monkeypatch.setenv("HERDR_HANDOFF_DIR", str(decoy))
    monkeypatch.setenv("HERDR_HANDOFF_FILE", str(decoy / "outbox" / "x.json"))
    monkeypatch.delenv("HERDR_HANDOFF_STORE", raising=False)
    report = handoff_reap.run(load_policy(home=tmp_path), home=tmp_path)
    assert report["status"] == "ok", report
    assert report["reap"]["mode"] == "apply"
    assert Path(report["reap"]["root"]) == tmp_path / ".local/share/herdr-handoff/store"
    assert not decoy.exists()


@pytest.mark.skipif(HANDOFF is None, reason="herdr-handoff is not installed")
def test_legacy_roots_are_imported_into_the_shared_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "proj" / ".herdr" / "handoffs"
    legacy.mkdir(parents=True)
    write_config(tmp_path, f"handoff_reap:\n  enabled: true\n  legacy_roots: [{legacy}]\n")
    for name in ("HERDR_HANDOFF_DIR", "HERDR_HANDOFF_FILE", "HERDR_HANDOFF_STORE"):
        monkeypatch.delenv(name, raising=False)
    report = handoff_reap.run(load_policy(home=tmp_path), home=tmp_path)
    assert report["status"] == "ok", report
    assert report["imports"][0]["root"] == str(legacy)
    assert "imported" in report["imports"][0]["result"]


def test_cli_status_prints_the_policy(tmp_path: Path) -> None:
    write_config(tmp_path, "handoff_reap:\n  interval_minutes: 20\n")
    out = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff-reap", "status"],
                         capture_output=True, text=True, check=True, cwd=ROOT,
                         env={**os.environ, "HOME": str(tmp_path)}).stdout
    assert json.loads(out)["policy"] == {"enabled": False, "interval_minutes": 20, "legacy_roots": []}


def test_cli_run_when_disabled_exits_zero(tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff-reap", "run"],
                          capture_output=True, text=True, cwd=ROOT, env={**os.environ, "HOME": str(tmp_path)})
    assert proc.returncode == 0 and json.loads(proc.stdout)["status"] == "disabled"


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    """A real herdr-eng entry point (this checkout's CLI), because the installer reads the policy through it."""
    path = tmp_path / "bin" / "herdr-eng"
    path.parent.mkdir()
    path.write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -m herdr_engineering.cli "$@"\n')
    path.chmod(0o755)
    return path


def run_installer(tmp_path: Path, launcher: Path, *args: str, config: str | None = None) -> str:
    if config is not None:
        write_config(tmp_path, config)
    return subprocess.run(["bash", str(INSTALLER), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "HERDR_ENG": str(launcher), "HOME": str(tmp_path),
                               "XDG_CONFIG_HOME": str(tmp_path / "xdg")}).stdout


linux_only = pytest.mark.skipif(os.uname().sysname != "Linux", reason="systemd rendering is Linux-only")


@linux_only
def test_installer_renders_systemd_units_from_the_yaml_interval(tmp_path: Path, launcher: Path) -> None:
    out = run_installer(tmp_path, launcher, "install", "--dry-run",
                        config="handoff_reap:\n  enabled: true\n  interval_minutes: 20\n")
    assert "herdr-eng-handoff-reap.service" in out and "herdr-eng-handoff-reap.timer" in out
    assert "OnCalendar=*:0/20" in out
    assert f'ExecStart="{launcher}" handoff-reap run' in out
    assert "Environment" not in out               # YAML config only
    assert FORBIDDEN not in out
    assert "enable --now" not in out and not (tmp_path / "xdg").exists()  # a dry run writes nothing


@linux_only
def test_installer_default_interval_is_fifteen_minutes(tmp_path: Path, launcher: Path) -> None:
    assert "OnCalendar=*:0/15" in run_installer(tmp_path, launcher, "install", "--dry-run")


@linux_only
def test_installer_writes_units_but_reports_the_timer_off_without_a_yaml_opt_in(tmp_path: Path, launcher: Path) -> None:
    """A real install with --no-load: files land in the XDG dir, and the policy it read says disabled."""
    out = run_installer(tmp_path, launcher, "install", "--no-load")
    assert (tmp_path / "xdg/systemd/user/herdr-eng-handoff-reap.timer").is_file()
    assert "policy enabled=false" in out
    out = run_installer(tmp_path, launcher, "install", "--no-load", config="handoff_reap:\n  enabled: true\n")
    assert "policy enabled=true" in out


@linux_only
def test_malformed_yaml_installs_disabled_not_enabled(tmp_path: Path, launcher: Path) -> None:
    out = run_installer(tmp_path, launcher, "install", "--no-load", config="handoff_reap:\n  enabled: maybe\n")
    assert "policy enabled=false" in out


def test_plist_template_is_valid_and_runs_the_same_command() -> None:
    text = (SCRIPTS / "com.herdr-engineering.handoff-reap.plist").read_text()
    rendered = text.replace("__HERDR_ENG__", "/bin/echo").replace("__HOME__", "/h").replace("__INTERVAL_SECONDS__", "900")
    plist = plistlib.loads(rendered.encode())
    assert plist["ProgramArguments"] == ["/bin/echo", "handoff-reap", "run"]
    assert plist["StartInterval"] == 900 and "EnvironmentVariables" not in plist


def test_ansible_task_is_wired_into_both_playbooks_and_gated_on_the_yaml() -> None:
    task = (ROOT / "ansible/tasks/handoff-reap.yml").read_text()
    assert "- handoff-reap\n          - status" in task and "herdr-eng-handoff-reap.timer" in task
    for playbook in ("site.yml", "fleet-sync.yml"):
        assert "../tasks/handoff-reap.yml" in (ROOT / "ansible/playbooks" / playbook).read_text()
