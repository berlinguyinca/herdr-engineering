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


def test_rules_component_drift_suggests_a_fleet_update():
    matrix = {"components": [{"group": "tools", "name": "rules", "reference": "8a92a22", "hosts": {
        "bender": {"version": "8a92a22", "state": "ok"},
        "mac": {"version": "1111111", "state": "drifted"},
        "beast": {"version": None, "state": "missing"}}}]}
    by = {e["host"]: e for e in plan_version_sync(matrix)}
    assert set(by) == {"mac", "beast"}
    assert by["mac"]["command"] == "herdr-eng fleet update --host mac"
    assert by["beast"]["command"] == "herdr-eng fleet update --host beast"


def test_fingerprint_rules_field_is_parsed_with_a_safe_default():

    from herdr_engineering.fleet import _rules_revision
    assert _rules_revision(["h", "x", "rules=8a92a22"]) == "8a92a22"
    assert _rules_revision(["h", "x", "rules=MISSING"]) == "MISSING"
    assert _rules_revision(["h", "summary=abc"]) == "unknown"        # an old fingerprint without the field


def test_fleet_update_and_join_pass_the_pinned_rules_as_extra_vars(tmp_path):
    import yaml

    from herdr_engineering.fleet import _playbook_vars
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text(yaml.safe_dump({"version": 1, "sections": {"control_plane": {"roles": ["control"], "values": {
        "agent_rules": {"repo_url": "git@github.com:a/b.git", "ref": "v1"}}}}, "hosts": {}}))
    cfg.chmod(0o600)
    assert _playbook_vars(cfg) == {"agent_rules_repo_url": "git@github.com:a/b.git", "agent_rules_ref": "v1"}
    assert _playbook_vars(tmp_path / "absent.yaml") == {}
