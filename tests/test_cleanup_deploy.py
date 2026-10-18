"""Deployment contracts for policy-preserving fleet cleanup updates."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def cleanup_block():
    path = ROOT / "ansible/tasks/build-cleanup.yml"
    assert path.exists(), "fleet updates must deploy the cleanup companion"
    return yaml.safe_load(path.read_text())[0]


@pytest.mark.parametrize("filename", ["fleet-sync.yml", "site.yml"])
def test_cleanup_deployed_after_package_provisioning(filename):
    playbook = yaml.safe_load((ROOT / "ansible/playbooks" / filename).read_text())[0]
    includes = [t.get("ansible.builtin.include_tasks") for t in playbook["tasks"]]
    assert "../tasks/build-cleanup.yml" in includes
    assert includes.index("../tasks/fabric-agent.yml") < includes.index("../tasks/build-cleanup.yml")


def test_cleanup_is_linux_user_service_and_never_overwrites_policy():
    block = cleanup_block()
    assert block["when"] == "ansible_facts['system'] == 'Linux'"
    assert block["become"] is False
    tasks = block["block"]
    writes = [t["ansible.builtin.copy"] for t in tasks if "ansible.builtin.copy" in t]
    assert len(writes) == 1
    assert writes[0]["dest"].endswith("/systemd/user/herdr-eng-build-cleanup.service")
    services = [t["ansible.builtin.systemd_service"] for t in tasks if "ansible.builtin.systemd_service" in t]
    assert len(services) == 1
    assert services[0]["scope"] == "user"
    assert services[0]["daemon_reload"] is True
    assert "restarted" in services[0]["state"]
    assert "stopped" in services[0]["state"]
    assert "build_cleanup_policy.stdout" in services[0]["enabled"]
    assert block["environment"]["XDG_RUNTIME_DIR"] == "/run/user/{{ ansible_user_uid }}"


@pytest.mark.parametrize("policy,expected", [
    ({}, False),
    ({"enabled": True}, False),
    ({"enabled": False, "roots": ["/projects"]}, False),
    ({"enabled": "false", "roots": ["/projects"]}, False),
    ({"enabled": True, "roots": "/projects"}, False),
    ({"enabled": True, "roots": [""]}, False),
    ({"enabled": True, "roots": ["/projects"]}, True),
])
def test_deployment_policy_uses_machine_config_without_mutating_it(tmp_path, policy, expected):
    tasks = cleanup_block()["block"]
    probe = next(t["ansible.builtin.command"] for t in tasks if t.get("register") == "build_cleanup_policy")
    assert probe["argv"][0].endswith("/.venvs/herdr-engineering/bin/python")
    config = tmp_path / ".config/herdr-engineering/config.yaml"
    config.parent.mkdir(parents=True)
    original = yaml.safe_dump({"build_cleanup": policy, "unrelated": "preserved"})
    config.write_text(original)
    result = subprocess.run([sys.executable, "-c", probe["argv"][2]],
                            env={**os.environ, "HOME": str(tmp_path)},
                            capture_output=True, text=True, check=True, cwd=ROOT)
    assert json.loads(result.stdout) is expected
    assert config.read_text() == original


@pytest.mark.parametrize("filename", ["fleet-sync.yml", "fleet-status.yml"])
def test_fingerprint_reports_revision_and_cleanup_state(filename):
    playbook = yaml.safe_load((ROOT / "ansible/playbooks" / filename).read_text())[0]
    report = playbook["tasks"][-1]["ansible.builtin.debug"]["msg"]
    assert "revision=" in report and "fp_revision" in report
    assert "pi-revision=" in report and "pi-path=" in report
    assert "cleanup=" in report and "fp_cleanup.stdout" in report


def test_fabric_installer_runs_from_remote_checkout():
    # Linux installer resolves its unit relative to its own scripts directory.
    content = (ROOT / 'ansible/tasks/fabric-agent.yml').read_text()
    assert "fabric-agent.sh" in content and "/scripts/" in content
    assert 'fabric-agent-macos.sh' in content


def test_deployment_checkout_can_be_separate_from_active_development():
    fabric = (ROOT / 'ansible/tasks/fabric-agent.yml').read_text()
    fingerprint = (ROOT / 'ansible/tasks/fleet-fingerprint.yml').read_text()
    assert 'HERDR_ENGINEERING_CHECKOUT' in fabric
    assert 'herdr_engineering_checkout' in fabric
    assert '/herdr/releases/{{ herdr_engineering_revision }}' in fabric
    assert 'stage-release.py' in fabric
    assert '--describe' in fingerprint
    assert "fp_release.rc == 0" in fingerprint
    assert "fp_pi_release.rc == 0" in fingerprint
    assert 'pull --ff-only' not in fabric
