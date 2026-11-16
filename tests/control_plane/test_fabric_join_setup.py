# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Installation sets up /herdr::join: listener resolution without --control-plane, idempotent config entry, opt-in hook, --check."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from herdr_engineering import fabric_join_setup as fj
from tests.control_plane.test_collab_fabric_auth import fabric_agent, fw, lan_ip  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin import aw, lw  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin_cli import identity, ws_on
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401

REPO = str(Path(__file__).resolve().parents[2])
FLEET_REL = ".config/herdr-engineering/fleet/fleet-config.yaml"
CFG_REL = ".config/herdr-engineering/config.yaml"
LISTENER = ("100.104.39.6", 9483)


def cli(home, *argv, env_extra=None, cwd=REPO):
    env = {"PATH": str(home / "nobin"), "HOME": str(home), "PYTHONPATH": REPO, **(env_extra or {})}
    (home / "nobin").mkdir(exist_ok=True)
    code_ = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    p = subprocess.run([sys.executable, "-c", code_, *argv], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def write_fleet(home, host=LISTENER[0], port=LISTENER[1], mode=0o600):
    data = {"version": 1, "sections": {"control_plane": {"roles": ["control-plane"], "values": {"collab": {
        "cognito_pool": "p", "cognito_clients": ["c"], "cognito_domain": "d.example.com", "public_url": "https://x.example.com",
        "operator_user": "alice", "operator_email": "a@example.com",
        "fabric_trust": {"enabled": True, "machines": ["bender"], "tailnet_suffix": "tail1234.ts.net",
                         "listen_host": host, "listen_port": port}}}}}, "hosts": {}}
    path = home / FLEET_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


def test_listener_resolves_from_the_fleet_config_and_from_nothing_else(tmp_path):
    assert fj.resolve_listener(tmp_path) is None
    write_fleet(tmp_path)
    assert fj.resolve_listener(tmp_path) == ("http://100.104.39.6:9483", "fleet-config")


def test_a_group_readable_fleet_config_is_not_used(tmp_path):
    write_fleet(tmp_path, mode=0o644)
    assert fj.resolve_listener(tmp_path) is None


def test_machine_config_wins_over_fleet_and_explicit_url_wins_over_both(tmp_path):
    write_fleet(tmp_path)
    cfg = tmp_path / CFG_REL
    cfg.write_text("fabric_join:\n  listen_host: 100.64.0.9\n  listen_port: 1234\n")
    assert fj.resolve_listener(tmp_path) == ("http://100.64.0.9:1234", "machine-config")
    cfg.write_text("fabric_join:\n  control_plane_url: http://10.0.0.5:7000/\n  listen_host: 100.64.0.9\n  listen_port: 1234\n")
    assert fj.resolve_listener(tmp_path) == ("http://10.0.0.5:7000", "machine-config")


def test_a_non_tailnet_listen_host_is_ignored(tmp_path):
    (tmp_path / CFG_REL).parent.mkdir(parents=True)
    (tmp_path / CFG_REL).write_text("fabric_join:\n  listen_host: 192.168.1.5\n  listen_port: 1234\n")
    assert fj.resolve_listener(tmp_path) is None


def test_install_writes_the_listener_idempotently_and_keeps_existing_config(tmp_path):
    write_fleet(tmp_path)
    cfg = tmp_path / CFG_REL
    cfg.write_text("# mine\nversion: 1\nweb:\n  port: 8787\n")
    assert fj.install_listener(tmp_path).action == "installed"
    text = cfg.read_text()
    assert text.startswith("# mine\nversion: 1\nweb:\n  port: 8787\n")
    block = yaml.safe_load(text)["fabric_join"]
    assert block == {"listen_host": LISTENER[0], "listen_port": LISTENER[1], "auto_join_on_session_start": False}
    assert fj.install_listener(tmp_path).action == "unchanged" and cfg.read_text() == text


def test_install_creates_a_private_config_when_none_exists_and_dry_run_writes_nothing(tmp_path):
    write_fleet(tmp_path)
    assert fj.install_listener(tmp_path, dry_run=True).action == "installed" and not (tmp_path / CFG_REL).exists()
    fj.install_listener(tmp_path)
    assert oct((tmp_path / CFG_REL).stat().st_mode & 0o777) == "0o600"


def test_a_differing_existing_listener_is_reported_and_never_overwritten(tmp_path):
    write_fleet(tmp_path)
    cfg = tmp_path / CFG_REL
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("fabric_join:\n  listen_host: 100.64.0.9\n  listen_port: 1234\n")
    item = fj.install_listener(tmp_path)
    assert item.action == "preserved" and "100.104.39.6" in item.detail
    assert cfg.read_text() == "fabric_join:\n  listen_host: 100.64.0.9\n  listen_port: 1234\n"


def test_install_adds_the_listener_into_an_existing_fabric_join_block(tmp_path):
    write_fleet(tmp_path)
    cfg = tmp_path / CFG_REL
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("fabric_join:\n  auto_join_on_session_start: false\n")
    assert fj.install_listener(tmp_path).action == "installed"
    assert yaml.safe_load(cfg.read_text())["fabric_join"] == {
        "listen_host": LISTENER[0], "listen_port": LISTENER[1], "auto_join_on_session_start": False}


def test_no_fleet_config_means_a_skipped_listener_entry(tmp_path):
    assert fj.install_listener(tmp_path).action == "skipped"


# ------------------------------------------------------------------ hooks
def enable(home, value="true"):
    cfg = home / CFG_REL
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"fabric_join:\n  auto_join_on_session_start: {value}\n")


def test_default_install_adds_no_hook_anywhere(tmp_path):
    rc, out, _ = cli(tmp_path, "summary", "install", "--home", str(tmp_path), "--skill", "herdr-join")
    assert rc == 0, out
    assert not (tmp_path / ".claude/settings.json").exists()
    assert "hook:" not in out
    enable(tmp_path, "false")
    assert fj.reconcile_hooks(tmp_path) == []


def test_enabled_hook_is_appended_to_existing_hooks_idempotent_and_removed_cleanly(tmp_path):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"SessionStart": [mine], "Stop": [mine]}}, indent=2))
    enable(tmp_path)
    first = fj.reconcile_hooks(tmp_path)
    assert first[0].action == "installed"
    assert {i.target for i in first[1:]} == {"hook:codex", "hook:opencode", "hook:pi"} and all(i.action == "unsupported" for i in first[1:])
    data = json.loads(settings.read_text())
    assert data["hooks"]["SessionStart"][0] == mine and len(data["hooks"]["SessionStart"]) == 2 and data["model"] == "x"
    assert "summary join-hook claude" in data["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    again = settings.read_text()
    assert fj.reconcile_hooks(tmp_path)[0].action == "unchanged" and settings.read_text() == again
    assert fj.uninstall_hooks(tmp_path)[0].action == "removed"
    after = json.loads(settings.read_text())
    assert after == {"model": "x", "hooks": {"SessionStart": [mine], "Stop": [mine]}}


def test_turning_the_key_off_removes_only_our_hook(tmp_path):
    enable(tmp_path)
    fj.reconcile_hooks(tmp_path)
    enable(tmp_path, "false")
    assert fj.reconcile_hooks(tmp_path)[0].action == "removed"
    assert json.loads((tmp_path / ".claude/settings.json").read_text()) == {}


def test_an_unreadable_settings_file_is_left_alone(tmp_path):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    enable(tmp_path)
    assert fj.reconcile_hooks(tmp_path)[0].action == "preserved" and settings.read_text() == "{not json"


def test_agent_name_rules():
    assert fj.agent_name("claude", {}) is None
    assert fj.agent_name("claude", {"HERDR_WORKSPACE_ID": "w1", "HERDR_PANE_ID": "P_3"}) == "claude-p_3"
    assert fj.agent_name("codex", {"HERDR_WORKSPACE_ID": "w1", "HERDR_PANE_ID": "a b/c"}) == "codex-a-b-c"


def test_hook_outside_a_herdr_pane_does_nothing_and_never_fails(tmp_path):
    rc, out, err = cli(tmp_path, "summary", "join-hook", "claude")
    assert (rc, out) == (0, "")


def test_hook_joins_inside_a_pane_and_swallows_an_unreachable_listener(lw, tmp_path):
    ws_on(lw)
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / CFG_REL
    cfg.parent.mkdir(parents=True)
    env = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_PANE_ID": "P7"}
    cfg.write_text(f"fabric_join:\n  control_plane_url: http://{lw.lan}:{lw.fabric_port}\n")
    rc, out, _ = cli(home, "summary", "join-hook", "claude", env_extra=env)
    assert rc == 0 and "claude-p7" in out and identity(tmp_path, "claude-p7").is_file()
    cfg.write_text("fabric_join:\n  control_plane_url: http://127.0.0.1:1\n")
    rc, out, _ = cli(home, "summary", "join-hook", "claude", env_extra=env)
    assert (rc, out) == (0, "")


# ------------------------------------------------------------------ bare join + --check
def test_bare_join_fabric_finds_the_listener_from_the_machine_config(lw, tmp_path):
    ws_on(lw)
    home = tmp_path / "home"
    cfg = home / CFG_REL
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"fabric_join:\n  control_plane_url: http://{lw.lan}:{lw.fabric_port}\n")
    nobin = home / "nobin"
    nobin.mkdir()
    env = {"PATH": str(nobin), "HOME": str(home), "PYTHONPATH": REPO, "HERDR_WORKSPACE_ID": "ws-1"}
    code_ = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    p = subprocess.run([sys.executable, "-c", code_, "rooms", "join-fabric", "bare-one", "--json"], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["name"] == "bare-one"


def test_bare_join_without_any_listener_says_how_to_fix_it(tmp_path):
    rc, _, err = cli(tmp_path, "rooms", "join-fabric", "x-1", "--json")
    assert rc == 1 and "herdr-eng summary install" in err and "--control-plane" in err


def test_check_fails_before_install_and_passes_after(lw, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc, out, _ = cli(home, "summary", "install", "--check", "--home", str(home), "--skill", "herdr-join", "--json")
    assert rc == 1
    bad = {r["action"] for r in json.loads(out)["results"]}
    assert "missing" in bad and "unresolved" in bad
    assert cli(home, "summary", "install", "--home", str(home), "--skill", "herdr-join")[0] == 0
    cfg = home / CFG_REL
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"fabric_join:\n  control_plane_url: http://{lw.lan}:{lw.fabric_port}\n")
    rc, out, _ = cli(home, "summary", "install", "--check", "--home", str(home), "--skill", "herdr-join", "--json")
    data = json.loads(out)
    assert rc == 0, out
    assert {"fabric listener"} <= {r["target"] for r in data["results"]}
    assert any(r["target"] == "fabric listener" and r["action"] == "ok" and "answers" in r["detail"] for r in data["results"])
    assert str(lw.fabric_port) in out and "Bearer" not in out


def test_check_reports_a_refused_listener_as_a_failure(tmp_path):
    cfg = tmp_path / CFG_REL
    cfg.parent.mkdir(parents=True)
    cfg.write_text("fabric_join:\n  control_plane_url: http://127.0.0.1:1\n")
    cli(tmp_path, "summary", "install", "--home", str(tmp_path), "--skill", "herdr-join")
    rc, out, _ = cli(tmp_path, "summary", "install", "--check", "--home", str(tmp_path), "--skill", "herdr-join")
    assert rc == 1 and "refused" in out


def test_check_requires_the_hook_when_auto_join_is_on(tmp_path):
    enable(tmp_path)
    assert any(i.action == "missing" for i in fj.check_setup(tmp_path, probe=False))
    fj.reconcile_hooks(tmp_path)
    assert not any(i.action in fj.FAIL_ACTIONS for i in fj.check_setup(tmp_path, probe=False) if i.target.startswith("claude"))


# ---- Codex execpolicy rule: the only way a sandboxed Codex may reach the tailnet listener ---------------------------------------
RULES_REL = ".codex/rules/default.rules"
RULE = 'prefix_rule(pattern=["herdr-eng", "rooms", "join-fabric"], decision="allow")'


def codex_home(home, rules=None):
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    if rules is not None:
        (home / RULES_REL).parent.mkdir(parents=True, exist_ok=True)
        (home / RULES_REL).write_text(rules)


def test_codex_rule_is_appended_once_to_existing_rules_and_nothing_else_changes(tmp_path):
    before = 'prefix_rule(pattern=["go", "test"], decision="allow")\n'
    codex_home(tmp_path, before)
    assert fj.reconcile_codex_rule(tmp_path)[0].action == "installed"
    text = (tmp_path / RULES_REL).read_text()
    assert text.startswith(before) and text.count(RULE) == 1
    assert fj.reconcile_codex_rule(tmp_path)[0].action == "unchanged"
    assert (tmp_path / RULES_REL).read_text() == text


def test_codex_rule_is_not_written_without_codex_and_creates_the_file_when_missing(tmp_path):
    assert fj.reconcile_codex_rule(tmp_path)[0].action == "skipped"
    assert not (tmp_path / ".codex").exists()
    codex_home(tmp_path)
    assert fj.reconcile_codex_rule(tmp_path)[0].action == "installed"
    assert (tmp_path / RULES_REL).read_text().count(RULE) == 1


def test_codex_rule_dry_run_writes_nothing_and_uninstall_removes_only_our_line(tmp_path):
    codex_home(tmp_path, 'prefix_rule(pattern=["go", "test"], decision="allow")\n')
    fj.reconcile_codex_rule(tmp_path, dry_run=True)
    assert RULE not in (tmp_path / RULES_REL).read_text()
    fj.reconcile_codex_rule(tmp_path)
    assert fj.uninstall_codex_rule(tmp_path)[0].action == "removed"
    assert (tmp_path / RULES_REL).read_text() == 'prefix_rule(pattern=["go", "test"], decision="allow")\n'
    assert fj.uninstall_codex_rule(tmp_path) == []


def test_codex_rule_a_hand_added_identical_rule_is_left_alone_on_uninstall(tmp_path):
    codex_home(tmp_path, RULE + "\n")
    assert fj.reconcile_codex_rule(tmp_path)[0].action == "unchanged"
    assert fj.uninstall_codex_rule(tmp_path) == []
    assert RULE in (tmp_path / RULES_REL).read_text()


def test_codex_rule_allows_only_the_join_prefix_per_the_real_codex_engine(tmp_path):
    import shutil
    codex = shutil.which("codex")
    if not codex:
        import pytest
        pytest.skip("codex not installed")
    codex_home(tmp_path)
    fj.reconcile_codex_rule(tmp_path)
    rules = str(tmp_path / RULES_REL)

    def decision(*argv):
        p = subprocess.run([codex, "execpolicy", "check", "--rules", rules, *argv], capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout).get("decision")
    assert decision("herdr-eng", "rooms", "join-fabric", "codex-x", "--json") == "allow"
    assert decision("herdr-eng", "rooms", "list") is None
    assert decision("herdr-eng", "rooms") is None
    assert decision("herdr-eng") is None
    assert decision("curl", "http://100.104.39.6:9483") is None


def test_check_reports_the_codex_rule(tmp_path):
    codex_home(tmp_path)
    assert [i.action for i in fj.check_setup(tmp_path, probe=False) if i.target == "codex execpolicy rule"] == ["missing"]
    fj.reconcile_codex_rule(tmp_path)
    assert [i.action for i in fj.check_setup(tmp_path, probe=False) if i.target == "codex execpolicy rule"] == ["ok"]


def test_cli_install_check_and_uninstall_manage_the_codex_rule(tmp_path):
    write_fleet(tmp_path)
    codex_home(tmp_path)
    rc, out, _ = cli(tmp_path, "summary", "install", "--skill", "herdr-join", "--json")
    assert rc == 0 and RULE in (tmp_path / RULES_REL).read_text()
    rc, out, _ = cli(tmp_path, "summary", "uninstall", "--skill", "herdr-join", "--json")
    assert RULE not in (tmp_path / RULES_REL).read_text()


def test_slow_join_is_left_running_not_killed_and_the_hook_returns_in_time(tmp_path):
    """A listener that accepts and never answers: the hook must return within `wait`, say so, and not hang the session."""
    import io
    import socket
    import time
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    home = tmp_path / "home"
    cfg = home / CFG_REL
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"fabric_join:\n  control_plane_url: http://127.0.0.1:{srv.getsockname()[1]}\n")
    out = io.StringIO()
    env = {**os.environ, "HOME": str(home), "HERDR_WORKSPACE_ID": "ws-1", "HERDR_PANE_ID": "slow1"}
    t0 = time.monotonic()
    try:
        assert fj.run_join_hook("claude", env=env, out=out, wait=1.0) == 0
        assert time.monotonic() - t0 < 6
        assert "claude-slow1" in out.getvalue() and "background" in out.getvalue()
    finally:
        srv.close()
