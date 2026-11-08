import os

from herdr_engineering.fleet import plan_version_sync

MATRIX = {"components": [
    {"group": "tools", "name": "claude", "reference": "2.1.306", "hosts": {
        "bender": {"version": "2.1.306", "state": "ok"}, "mac": {"version": "2.1.304", "state": "drifted"}}},
    {"group": "plugins", "name": "claude:mempalace", "reference": "3.0.14", "hosts": {
        "mac": {"version": "3.0.14", "state": "ok"}, "beast": {"version": None, "state": "missing"}}},
    {"group": "tools", "name": "node", "reference": "26.7.0", "hosts": {"bender": {"version": "22.23.3", "state": "drifted"}}},
    {"group": "tools", "name": "mystery", "reference": None, "hosts": {"bender": {"version": None, "state": "missing"}}},
]}


def test_plan_lists_only_differences_with_suggested_commands():
    plan = plan_version_sync(MATRIX)
    by = {(e["host"], e["component"]): e for e in plan}
    assert set(by) == {("mac", "claude"), ("beast", "claude:mempalace"), ("bender", "node")}
    assert by[("mac", "claude")]["command"] == "npm install -g @anthropic-ai/claude-code@2.1.306"
    assert by[("beast", "claude:mempalace")]["command"] == "claude plugin install mempalace"
    assert by[("bender", "node")]["command"] is None and "note" in by[("bender", "node")]



def test_rules_has_no_drift_updater_because_it_is_reported_by_fleet_status_only():
    from herdr_engineering.fleet import _UPDATERS
    assert "rules" not in _UPDATERS


def test_fingerprint_rules_field_is_parsed_by_a_dedicated_pattern():
    from herdr_engineering.fleet import _rules_revision
    sha = '"msg": "h | herdr-eng=x | summary=abc | rules=8a92a22"'
    assert _rules_revision(sha) == "8a92a22"
    assert _rules_revision('h | rules=MISSING | other=1') == "MISSING"
    assert _rules_revision('h | rules=FAILED:ref \'v9\' not found as a tag or commit | x=1') == "FAILED:ref 'v9' not found as a tag or commit"
    assert _rules_revision('h | summary=abc"') == "unknown"                # an old fingerprint without the field
    assert _rules_revision('h | xrules=8a92a22') == "unknown"              # not a field boundary


def _fleet_yaml(tmp_path, values, name="fleet.yaml"):
    import yaml
    cfg = tmp_path / name
    cfg.write_text(yaml.safe_dump({"version": 1, "sections": {"control_plane": {"roles": ["control"], "values": values}},
                                   "hosts": {}}))
    cfg.chmod(0o600)
    return cfg


def test_playbook_vars_pass_the_pin_and_say_nothing_when_unpinned(tmp_path, capsys):
    from herdr_engineering.fleet import _playbook_vars
    cfg = _fleet_yaml(tmp_path, {"agent_rules": {"repo_url": "git@github.com:a/b.git", "ref": "v1"}})
    assert _playbook_vars(cfg) == {"agent_rules_repo_url": "git@github.com:a/b.git", "agent_rules_ref": "v1"}
    assert capsys.readouterr().err == ""
    assert _playbook_vars(tmp_path / "absent.yaml") == {}
    assert "no agent-rules pin" in capsys.readouterr().err
    assert _playbook_vars(_fleet_yaml(tmp_path, {}, "none.yaml")) == {}
    assert "no agent-rules pin" in capsys.readouterr().err


def test_playbook_vars_fail_loud_on_a_broken_pin_or_config(tmp_path):
    import pytest

    from herdr_engineering.control_plane.agent_rules_config import AgentRulesConfigError
    from herdr_engineering.fleet import _playbook_vars
    bad = _fleet_yaml(tmp_path, {"agent_rules": {"repo_url": "git@github.com:a/b.git"}}, "bad.yaml")   # no ref
    with pytest.raises(AgentRulesConfigError):
        _playbook_vars(bad)
    garbage = tmp_path / "garbage.yaml"
    garbage.write_text("::: not yaml [")
    with pytest.raises(AgentRulesConfigError):
        _playbook_vars(garbage)
    unreadable = _fleet_yaml(tmp_path, {}, "locked.yaml")
    unreadable.chmod(0)
    try:
        if not os.access(unreadable, os.R_OK):          # root ignores mode bits
            with pytest.raises(AgentRulesConfigError):
                _playbook_vars(unreadable)
    finally:
        unreadable.chmod(0o600)


def test_fleet_update_refuses_to_run_with_a_broken_pin(tmp_path, monkeypatch):
    from herdr_engineering import fleet
    bad = _fleet_yaml(tmp_path, {"agent_rules": {"repo_url": "git@github.com:a/b.git", "ref": "-x"}}, "bad.yaml")
    report = fleet.fleet_update(fleet_config=bad, pull_controller=False, reinstall_local=False, inventory=tmp_path / "nope.yml")
    assert report["ok"] is False and "agent_rules" in report["error"]


def test_fleet_join_with_a_broken_pin_skips_provisioning_and_fails(tmp_path):
    from herdr_engineering import fleet
    bad = _fleet_yaml(tmp_path, {"agent_rules": {"repo_url": "git@github.com:a/b.git"}}, "bad.yaml")
    inv = tmp_path / "hosts.yml"
    report = fleet.fleet_join(ssh_target="h", attach=False, provision=True, inventory=inv, host_vars_dir=tmp_path / "hv",
                              agent_rules_config=bad)
    assert report["ok"] is False and any("agent_rules" in e for e in report["errors"])
    assert "provision" not in report["steps"] or report["steps"]["provision"]["ok"] is False
