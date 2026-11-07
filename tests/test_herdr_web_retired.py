"""herdr-web is retired (fleet provisioning, Task 19): no compose service, no CLI command, no module.

The container that may still run on the control host is removed by the control-plane role's adoption,
not by anything in this repository.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from herdr_engineering import cli

ROOT = Path(__file__).resolve().parent.parent


def _commands(parser: argparse.ArgumentParser) -> set[str]:
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return {name for a in actions for name in a.choices}


def test_compose_project_has_no_herdr_web_service():
    compose = yaml.safe_load((ROOT / "deploy" / "control-plane" / "compose.yaml").read_text())
    services = compose["services"]
    assert "herdr-web" not in services
    assert not any(s.get("container_name") == "herdr-web" for s in services.values())
    # the front door the retired service shared its image with is still there
    assert {"coredns", "fabric-gateway", "control-plane-web"} <= set(services)


def test_cli_has_no_web_command():
    commands = _commands(cli._build_parser())
    assert "web" not in commands
    assert {"agent-mine", "control-plane"} <= commands  # neighbours kept


def test_web_module_is_gone():
    # the package directory, not find_spec: an editable install elsewhere can still resolve the old module
    package = Path(cli.__file__).resolve().parent
    assert not (package / "web.py").exists()
    assert not (package / "web").exists()


def test_old_host_unit_and_its_ansible_task_are_gone():
    assert not (ROOT / "scripts" / "herdr-eng-web.service").exists()
    assert not (ROOT / "scripts" / "web-serve.sh").exists()
    assert not (ROOT / "ansible" / "tasks" / "web-serve.yml").exists()
    assert "web-serve.yml" not in (ROOT / "ansible" / "playbooks" / "site.yml").read_text()


def _fabric_stack() -> str:
    return (ROOT / "scripts" / "fabric-stack.sh").read_text()


def _function(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}", start + 1)]


def test_fabric_stack_looks_for_the_left_container_by_type():
    helper = next(line for line in _fabric_stack().splitlines() if line.startswith("left_herdr_web()"))
    assert "docker inspect --type container herdr-web" in helper


def test_fabric_stack_down_leaves_a_left_herdr_web_container_alone_and_says_so():
    down = _function(_fabric_stack(), "down")
    assert '"${COMPOSE[@]}" stop coredns fabric-gateway\n' in down  # the service is not named
    assert "left_herdr_web" in down and "warn " in down
    assert "docker stop" not in down and "docker rm" not in down and " rm " not in down


def test_fabric_stack_up_never_removes_a_left_herdr_web_container():
    up = _function(_fabric_stack(), "up")
    guard = up.index("elif left_herdr_web; then")
    cleared = up.index("orphans=()", guard)
    run = up.index('up -d ${orphans[@]+"${orphans[@]}"}')
    assert cleared < run
    assert "orphans=(--remove-orphans)" not in up[guard:run]  # nothing re-enables it after the guard


def test_install_writes_no_web_block():
    # the dev agent's 8787 exclusion comes from config.py's defaults, so new machine configs need no `web:`
    assert "\nweb:\n" not in (ROOT / "scripts" / "install.sh").read_text()
