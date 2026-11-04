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
    "scripts/fabric-offboard.sh",
]


def test_fabric_install_paths_do_not_use_the_env_file():
    for rel in FILES:
        text = (ROOT / rel).read_text()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert "fabric.env" not in line, (rel, line)
            assert "HERDR_ENGINEERING_FABRIC_URL" not in line, (rel, line)


def test_offboard_resolves_gateway_via_the_agents_resolver():
    text = (ROOT / "scripts/fabric-offboard.sh").read_text()
    assert "gateway_url_from_config" in text


def test_agent_resolves_fabric_url_from_yaml(monkeypatch):
    from herdr_engineering import host_config
    from herdr_engineering.fabric import gateway_url_from_config
    monkeypatch.delenv("HERDR_ENGINEERING_FABRIC_URL", raising=False)
    monkeypatch.setattr(host_config, "host_value",
                        lambda key, default=None: "gw.example" if key == "fabric.url" else default)
    cfg = {"fabric": {"gateway_url": "ignored.example", "control_port": 1234}}
    assert gateway_url_from_config(cfg) == "http://gw.example:1234"
    monkeypatch.setattr(host_config, "host_value", lambda key, default=None: default)
    assert gateway_url_from_config(cfg) == "http://ignored.example:1234"
    monkeypatch.setenv("HERDR_ENGINEERING_FABRIC_URL", "http://env.example:1")
    assert gateway_url_from_config(cfg) == "http://ignored.example:1234"


def test_fleet_join_gateway_flag_is_loudly_ignored(capsys):
    from herdr_engineering.fleet import warn_gateway_ignored
    assert warn_gateway_ignored(None) is False
    assert capsys.readouterr().err == ""
    assert warn_gateway_ignored("http://x:1") is True
    err = capsys.readouterr().err
    assert "IGNORED" in err and "fabric.url" in err and "fleet-config.yaml" in err


def test_fleet_fingerprint_probes_the_service_names_the_installers_actually_use():
    """`agent=inactive` on a Mac whose agent was running, forever.

    The fleet fingerprint asks launchd for `herdr-eng-fabric-agent` - the *systemd* unit name -
    while scripts/fabric-agent-macos.sh registers the LaunchAgent as
    `com.herdr-engineering.fabric-agent`. `launchctl print` finds nothing, the probe answers
    `inactive`, `_parity()` counts the host invalid, and every `herdr-eng fleet update` - including
    the unattended nightly one - reports the fleet not at parity because of a host that is fine.
    The probe has carried the wrong name since 57615222, so no mac has ever passed it.
    """
    probe = (ROOT / "ansible/tasks/fleet-fingerprint.yml").read_text()
    installer = (ROOT / "scripts/fabric-agent-macos.sh").read_text()
    linux_unit = (ROOT / "scripts/herdr-eng-fabric-agent.service").name

    label = next(line.split('="', 1)[1].rstrip('"')
                 for line in installer.splitlines() if line.startswith("LABEL="))
    assert label != linux_unit, "the two platforms do not share a service name"
    assert f"gui/$(id -u)/{label}" in probe, (
        f"the launchctl probe must ask for the label the macOS installer writes ({label})")
    assert linux_unit in probe, "the systemd probe must ask for the unit the Linux installer writes"
