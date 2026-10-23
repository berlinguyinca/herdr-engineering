"""Static guards for the ansible tailscale role (no tailnet needed)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "ansible"
ROLE = ROOT / "roles" / "tailscale"


def _tasks(items):
    for t in items:
        yield t
        for key in ("block", "rescue", "always"):
            if key in t:
                yield from _tasks(t[key])


def _all_tasks():
    return list(_tasks(yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())))


def test_defaults_have_no_key_literal():
    d = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert d["tailscale_auth_key"] == ""
    assert d["tailscale_tags"] == ["tag:fleet"]
    for f in ROLE.rglob("*.yml"):
        assert "tskey-" not in f.read_text()


def test_key_touching_tasks_are_no_log():
    tasks = _all_tasks()
    ups = [t for t in tasks if "ansible.builtin.command" in t and "up" in str(t["ansible.builtin.command"])]
    assert ups and all(t.get("no_log") is True for t in ups)
    for t in tasks:
        if "tailscale_auth_key" in str(t) and "ansible.builtin.fail" not in t and "when" not in t.get("ansible.builtin.debug", {}):
            if "ansible.builtin.copy" in t or "ansible.builtin.command" in t:
                assert t.get("no_log") is True


def test_key_not_on_command_line():
    text = (ROLE / "tasks" / "main.yml").read_text()
    assert "--auth-key=file:" in text
    assert "--auth-key={{" not in text


def test_probe_runs_in_check_mode():
    probe = next(t for t in _all_tasks() if "ansible.builtin.command" in t and "status --json" in str(t["ansible.builtin.command"]))
    assert probe["check_mode"] is False


def test_role_included_before_platform_role():
    text = (ROOT / "playbooks" / "site.yml").read_text()
    assert text.index("name: tailscale") < text.index("herdr_linux")
