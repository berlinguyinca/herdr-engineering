"""The fabric agent is configured from YAML only: no fabric.env, no env-var gateway."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "ansible/tasks/fabric-agent.yml",
    "ansible/tasks/fabric-systemd.yml",
    "ansible/tasks/herdr-eng-fabric-enroll-sync.service.j2",
    "deploy/mission-feed/herdr-service-metadata.service",
    "scripts/herdr-eng-fabric-agent.service",
    "scripts/fabric-agent.sh",
    "scripts/fabric-agent-macos.sh",
]


def test_fabric_install_paths_do_not_use_the_env_file():
    for rel in FILES:
        text = (ROOT / rel).read_text()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert "fabric.env" not in line, (rel, line)
            assert "HERDR_ENGINEERING_FABRIC_URL" not in line, (rel, line)
