"""Managed Claude Code workflow hooks. Real temp HOMEs only; never the real one."""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from herdr_engineering import workflow_hooks as wh
from herdr_engineering.cli import main

CMD = ["/opt/herdr-eng"]


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


def inst(home):
    return wh.WorkflowInstaller(home, command=CMD)


def settings(home):
    return home / ".claude" / "settings.json"


def test_install_creates_managed_entries(home):
    res = inst(home).install()
    assert res.ok and res.action == "create"
    hooks = json.loads(settings(home).read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "PreCompact", "PreToolUse"}
    cmd = hooks["PreCompact"][0]["hooks"][0]["command"]
    assert wh.MARKER in cmd and cmd.startswith("[ -x /opt/herdr-eng ] && ") and cmd.count("|| true") == 1
    assert "workflow hook PreCompact" in cmd


def test_second_install_is_noop(home):
    inst(home).install()
    before, mtime = settings(home).read_bytes(), settings(home).stat().st_mtime_ns
    res = inst(home).install()
    assert res.action == "noop"
    assert settings(home).read_bytes() == before and settings(home).stat().st_mtime_ns == mtime


def test_foreign_hooks_preserved_and_uninstall_removes_only_managed(home):
    user = {"model": "x", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "mine"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "memory # herdr-memory:managed"}]}]}}
    settings(home).write_text(json.dumps(user, indent=2))
    inst(home).install()
    data = json.loads(settings(home).read_text())
    assert data["hooks"]["Stop"] == user["hooks"]["Stop"]
    assert len(data["hooks"]["SessionStart"]) == 2
    inst(home).uninstall()
    assert json.loads(settings(home).read_text()) == user


def test_doctor_reports_drift_and_repair_fixes(home):
    assert inst(home).doctor()["overall"] == "absent"
    inst(home).install()
    assert inst(home).doctor()["ok"]
    data = json.loads(settings(home).read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f"old # {wh.MARKER}"
    settings(home).write_text(json.dumps(data))
    d = inst(home).doctor()
    assert not d["ok"] and d["overall"] == "stale"
    inst(home).install()
    assert inst(home).doctor()["ok"]


def test_dry_run_writes_nothing_and_invalid_json_untouched(home):
    res = inst(home).install(dry_run=True)
    assert res.action == "create" and not settings(home).exists()
    settings(home).write_text("{not json")
    res = inst(home).install()
    assert not res.ok and res.action == "invalid"
    assert settings(home).read_text() == "{not json"


def test_skips_when_claude_absent(tmp_path):
    h = tmp_path / "bare"
    h.mkdir()
    res = wh.WorkflowInstaller(h, command=CMD, which=lambda n: None).install()
    assert res.action == "skip" and not (h / ".claude").exists()


def run_hook(event, stdin="{}"):
    out = io.StringIO()
    assert wh.run_hook(event, io.StringIO(stdin), out) == 0
    return json.loads(out.getvalue())


def test_session_start_reminder_is_short_and_covers_defaults():
    ctx = run_hook("SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert len(ctx.splitlines()) <= 15
    for needle in ("sonnet", "verify", "never push main", "never force-push", "/tmp", "woodpecker", "/handoff"):
        assert needle in ctx.lower()
    assert run_hook("SessionStart")["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_start_says_not_to_ask_about_settled_choices():
    ctx = run_hook("SessionStart")["hookSpecificOutput"]["additionalContext"].lower()
    assert "do not ask the user about choices these defaults already settle" in ctx
    assert "run /handoff yourself, without asking" in ctx


def test_precompact_instructs_agent_to_handoff_without_asking():
    msg = run_hook("PreCompact")["systemMessage"]
    assert "Run /handoff yourself" in msg and "without asking" in msg
    assert "background subagents" in msg and "herdr-handoff create" not in msg


def test_hook_never_fails_on_bad_input():
    out = io.StringIO()
    assert wh.run_hook("Bogus", io.StringIO("not json"), out) == 0 and out.getvalue() == ""


def test_cli_install_doctor_uninstall(home, capsys):
    assert main(["workflow", "install", "--home", str(home)]) == 0
    assert main(["workflow", "doctor", "--home", str(home)]) == 0
    assert main(["workflow", "uninstall", "--home", str(home)]) == 0
    assert not json.loads(settings(home).read_text()).get("hooks")
    assert main(["workflow", "doctor", "--home", str(home)]) == 1


def test_default_command_uses_installing_interpreter(home):
    cmd = wh.WorkflowInstaller(home).command
    assert cmd == [sys.executable, "-m", "herdr_engineering.cli"]


def test_pristine_backup_survives_install_repair_uninstall(home):
    original = '{"theme": "dark"}\n'
    settings(home).write_text(original)
    i = inst(home)
    i.install()
    settings(home).write_text(settings(home).read_text().replace("workflow hook", "workflow hook "))
    i.repair()
    i.uninstall()
    backups = home / ".local" / "share" / "herdr-engineering" / "workflow" / "backups"
    assert (backups / "settings.json.bak").read_text() == original
    assert len(list(backups.iterdir())) >= 3


def _hook_commands(home, command):
    wh.WorkflowInstaller(home, command=command).install()
    hooks = json.loads(settings(home).read_text())["hooks"]
    return {ev: v[0]["hooks"][0]["command"] for ev, v in hooks.items()}


def test_generated_command_executes_via_sh(home):
    cmds = _hook_commands(home, [sys.executable, "-m", "herdr_engineering.cli"])
    done = subprocess.run(["sh", "-c", cmds["SessionStart"]], input="{}", capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "/handoff" in out["hookSpecificOutput"]["additionalContext"]


def test_generated_command_with_missing_executable_is_silent_noop(home):
    cmds = _hook_commands(home, ["/nonexistent/python", "-m", "herdr_engineering.cli"])
    done = subprocess.run(["sh", "-c", cmds["SessionStart"]], input="{}", capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout == ""


def test_install_adds_cleanup_allow_rule_and_reminder_mentions_command(home):
    settings(home).write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    inst(home).install()
    assert json.loads(settings(home).read_text())["permissions"]["allow"] == [
        "Bash(ls:*)", "Bash(herdr-eng cleanup:*)"]
    assert inst(home).install().action == "noop"
    ctx = run_hook("SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "herdr-eng cleanup" in ctx and "--print-run-args" in ctx
    inst(home).uninstall()
    assert json.loads(settings(home).read_text()) == {"permissions": {"allow": ["Bash(ls:*)"]}}


def test_installed_rule_is_exactly_the_cleanup_command(home):
    inst(home).install()
    assert json.loads(settings(home).read_text())["permissions"]["allow"] == ["Bash(herdr-eng cleanup:*)"]


def test_removed_allow_rule_is_not_drift_and_never_re_added(home):
    inst(home).install()
    data = json.loads(settings(home).read_text())
    del data["permissions"]
    settings(home).write_text(json.dumps(data, indent=2))
    before = settings(home).read_bytes()
    assert inst(home).status().state == "current"
    assert inst(home).install().action == "noop"
    assert inst(home).repair().action == "noop"
    assert settings(home).read_bytes() == before
    # a genuine hook drift is repaired, still without resurrecting the permission
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "tampered # " + wh.MARKER
    settings(home).write_text(json.dumps(data, indent=2))
    assert inst(home).status().state == "stale"
    inst(home).repair()
    assert inst(home).status().state == "current"
    assert "permissions" not in json.loads(settings(home).read_text())
