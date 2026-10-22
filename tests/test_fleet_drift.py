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
