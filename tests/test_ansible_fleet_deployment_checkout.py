"""Structural guards for ansible/tasks/fleet-deployment-checkout.yml (the herdr-engineering
deployment checkout + pointer file the fleet-watch rollout rows require; first live rollout
2026-11-23 failed closed on every host because nothing provisioned them)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "ansible" / "tasks" / "fleet-deployment-checkout.yml"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "fleet-sync.yml"
POINTER = ".config/herdr-engineering/deployment-checkout"
DEFAULT_CHECKOUT = "/.local/share/herdr-engineering/fleet-checkout"


def _tasks():
    return yaml.safe_load(TASKS.read_text())


def _play_includes():
    play = yaml.safe_load(PLAYBOOK.read_text())
    return [t for t in play[0]["tasks"] if t.get("ansible.builtin.include_tasks")]


def test_task_file_is_a_list_of_tasks():
    tasks = _tasks()
    assert isinstance(tasks, list) and tasks
    for task in tasks:
        assert "name" in task, task


def test_included_into_fleet_sync_before_the_watcher_unit():
    includes = [Path(t["ansible.builtin.include_tasks"]).name for t in _play_includes()]
    assert "fleet-deployment-checkout.yml" in includes
    # the watcher's rollout rows read the pointer file the watcher runs: provision before the unit
    assert includes.index("fleet-deployment-checkout.yml") < includes.index("fleet-watch.yml")


def test_resolves_its_own_variable_with_the_dedicated_default():
    first = next(t for t in _tasks() if t.get("ansible.builtin.set_fact"))
    fact = first["ansible.builtin.set_fact"]
    assert list(fact) == ["fleet_deployment_checkout"]
    assert re.search(r"fleet_deployment_checkout\s*\|\s*default\(fleet_home\s*~\s*"
                     + re.escape("'" + DEFAULT_CHECKOUT + "'"), fact["fleet_deployment_checkout"])


def test_clones_the_pinned_repo_ff_only_to_main():
    git_task = next(t for t in _tasks() if t.get("ansible.builtin.git"))
    spec = git_task["ansible.builtin.git"]
    assert spec["repo"] == "ssh://git@github.com/berlinguyinca/herdr-engineering.git"
    assert spec["version"] == "main"
    assert spec["update"] is True and spec["force"] is False and spec["accept_hostkey"] is True
    assert "fleet_deployment_checkout" in spec["dest"] and "{{" in spec["dest"]


def test_writes_the_one_line_pointer_file_private():
    copy_task = next(t for t in _tasks() if t.get("ansible.builtin.copy"))
    spec = copy_task["ansible.builtin.copy"]
    assert spec["dest"].endswith(POINTER) and "fleet_home" in spec["dest"]
    assert str(spec["mode"]).lstrip("0") == "600"
    assert spec["content"] == "{{ fleet_deployment_checkout }}\n"


def test_never_uses_the_fabric_agent_repurposed_variable():
    # fabric-agent.yml set_fact's `herdr_engineering_checkout` to the immutable per-revision release
    # dirs (pinned to a sha, not on main - the D6 gate refuses it); this task must keep its own.
    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from walk(v)
        elif isinstance(value, list):
            for v in value:
                yield from walk(v)
        elif isinstance(value, str):
            yield value

    for template in walk(_tasks()):
        assert "herdr_engineering_checkout" not in template, template
