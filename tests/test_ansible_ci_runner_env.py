"""Guards for the WPCI_CI_DIR runner provisioning play, run with the real local
Ansible (no remote host, no systemd): the drop-in is rendered by the real
template engine and copy module."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
PLAY = REPO / "ansible" / "playbooks" / "ci-runner-env.yml"
CRON = (REPO / ".woodpecker" / "main-cron.yml").read_text()
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible not installed")


def _play():
    return yaml.safe_load(PLAY.read_text())[0]


def test_play_targets_ci_runners_and_carries_no_private_values():
    assert _play()["hosts"] == "ci_runners"
    for banned in ("/home/", "ts.net", "100.64.", "metabolomics"):
        assert banned not in PLAY.read_text()


def test_the_variable_the_pipeline_requires_is_the_one_provisioned():
    assert "WPCI_CI_DIR" in CRON
    dropin = next(t for t in _play()["tasks"] if "Install the WPCI" in t["name"])
    assert "WOODPECKER_ENVIRONMENT=WPCI_CI_DIR:" in dropin["ansible.builtin.copy"]["content"]


@needs_ansible
def test_syntax_check():
    out = subprocess.run(["ansible-playbook", "--syntax-check", "-i", "localhost,", str(PLAY)],
                         capture_output=True, text=True, cwd=REPO / "ansible")
    assert out.returncode == 0, out.stderr


@needs_ansible
def test_missing_vars_fail_before_touching_anything(tmp_path):
    inv = tmp_path / "inv.yml"
    inv.write_text("all:\n  children:\n    ci_runners:\n      hosts:\n        localhost:\n"
                   "          ansible_connection: local\n")
    out = subprocess.run(["ansible-playbook", "-i", str(inv), str(PLAY), "-e", "ansible_become=false",
                          "-e", "wpci_ci_mount=relative"],
                         capture_output=True, text=True, cwd=REPO / "ansible")
    assert out.returncode != 0 and "wpci_ci_store" in out.stdout + out.stderr


@needs_ansible
def test_the_dropin_renders_with_the_given_paths(tmp_path):
    content = next(t for t in _play()["tasks"] if "Install the WPCI" in t["name"])["ansible.builtin.copy"]["content"]
    tpl = tmp_path / "tpl.yml"
    tpl.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "connection": "local",
                                    "tasks": [{"ansible.builtin.copy": {"dest": str(tmp_path / "out.conf"),
                                                                        "content": content}}]}]))
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(tpl),
                          "-e", "wpci_ci_store=/srv/store wpci_ci_mount=/ci"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    text = (tmp_path / "out.conf").read_text()
    assert 'WOODPECKER_ENVIRONMENT=WPCI_CI_DIR:/ci"' in text
    assert 'WOODPECKER_BACKEND_DOCKER_VOLUMES=/srv/store:/ci"' in text
