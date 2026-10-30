"""The ai-sync feed unit is a plain user unit, converged by the fleet playbooks like the other herdr-eng units."""
import shlex
from pathlib import Path

import pytest
import yaml

from herdr_engineering import ai_sync_feed
from tests.test_deploy_units import parse_unit

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "scripts" / "herdr-eng-ai-sync-feed.service"
TASKS = ROOT / "ansible" / "tasks" / "ai-sync-feed.yml"


def test_unit_runs_the_feeder_loop_with_no_environment():
    sections = parse_unit(UNIT)
    service = dict(sections["Service"])
    assert "Environment" not in service and "EnvironmentFile" not in service
    argv = shlex.split(service["ExecStart"])
    assert argv[0].endswith("/.venvs/herdr-engineering/bin/python") and argv[1:3] == ["-m", "herdr_engineering.ai_sync_feed"]
    loop = float(argv[argv.index("--loop") + 1])
    assert 30 <= loop <= 3600
    assert service["Restart"] == "on-failure" and service["NoNewPrivileges"] == "true" and service["UMask"] == "0077"
    assert dict(sections["Install"])["WantedBy"] == "default.target"
    assert not {"pull", "push"} & set(argv)


def test_feeder_rejects_a_loop_the_unit_could_not_use():
    with pytest.raises(SystemExit):
        ai_sync_feed.main(["--loop", "5"])


def block():
    return yaml.safe_load(TASKS.read_text())[0]


def test_tasks_install_user_unit_and_enable_only_where_ai_sync_exists():
    b = block()
    assert b["when"] == "ansible_facts['system'] == 'Linux'" and b["become"] is False
    assert b["environment"]["XDG_RUNTIME_DIR"] == "/run/user/{{ ansible_user_uid }}"
    tasks = b["block"]
    copies = [t["ansible.builtin.copy"] for t in tasks if "ansible.builtin.copy" in t]
    assert len(copies) == 1 and copies[0]["dest"].endswith("/systemd/user/herdr-eng-ai-sync-feed.service")
    probe = next(t for t in tasks if t.get("register") == "ai_sync_present")
    assert "ai-sync" in str(probe) and probe["changed_when"] is False
    (service,) = [t["ansible.builtin.systemd_service"] for t in tasks if "ansible.builtin.systemd_service" in t]
    assert service["name"] == "herdr-eng-ai-sync-feed.service" and service["scope"] == "user"
    assert service["daemon_reload"] is True and "ai_sync_present" in service["enabled"]
    assert "restarted" in service["state"] and "stopped" in service["state"]


@pytest.mark.parametrize("playbook", ["fleet-sync.yml", "site.yml"])
def test_playbooks_converge_the_feed_after_package_provisioning(playbook):
    tasks = yaml.safe_load((ROOT / "ansible/playbooks" / playbook).read_text())[0]["tasks"]
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert "../tasks/ai-sync-feed.yml" in includes
    assert includes.index("../tasks/fabric-agent.yml") < includes.index("../tasks/ai-sync-feed.yml")
