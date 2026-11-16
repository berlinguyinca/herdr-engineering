"""Fabric-trust listener deploy wiring (SPEC 6a): the config helper deploy.sh calls, and the compose overlay rendered
by the real `docker compose config` (read-only; nothing is started)."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import load_fleet_config
from tests.test_compose_render import ANSWERS, _compose_ok, _config

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
TRUST = {"enabled": True, "tailnet_suffix": "tail1234.ts.net", "machines": ["beast"],
         "listen_host": "100.64.0.7", "listen_port": 8484, "whois_socket": "/run/fabric-whois/whois.sock"}


def _central(tmp_path, trust):
    central = tmp_path / "fleet-config.yaml"
    fcc.init_config(central, dict(ANSWERS))
    data = yaml.safe_load(central.read_text())
    values = data["sections"]["control_plane"]["values"]
    values["compose"]["HERDR_COLLAB_CERT_DIR"] = str(tmp_path / "acme")
    values["collab"] = {"cognito_pool": "us-west-2_TEST", "operator_user": "owner-test"}
    if trust is not None:
        values["collab"]["fabric_trust"] = trust
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    return central


def test_fabric_listener_helper_returns_the_validated_address(tmp_path):
    assert fcc.fabric_listener(load_fleet_config(_central(tmp_path, dict(TRUST)))) == ("100.64.0.7", 8484)


def test_fabric_listener_helper_is_none_when_unconfigured(tmp_path):
    assert fcc.fabric_listener(load_fleet_config(_central(tmp_path, None))) is None
    plain = {k: v for k, v in TRUST.items() if not k.startswith("listen_")}
    (tmp_path / "b").mkdir()
    assert fcc.fabric_listener(load_fleet_config(_central(tmp_path / "b", plain))) is None


def test_fabric_listener_helper_refuses_a_non_tailnet_host(tmp_path):
    with pytest.raises(Exception, match="tailnet|100.64|fd7a"):
        fcc.fabric_listener(load_fleet_config(_central(tmp_path, dict(TRUST, listen_host="192.168.1.5"))))


def test_cli_prints_host_and_port(tmp_path):
    central = _central(tmp_path, dict(TRUST))
    r = subprocess.run(["python3", "-m", "herdr_engineering.cli", "fleet", "config", "fabric-listener",
                        "--path", str(central)], cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["100.64.0.7", "8484"]


def test_cli_prints_nothing_when_unconfigured(tmp_path):
    central = _central(tmp_path, None)
    r = subprocess.run(["python3", "-m", "herdr_engineering.cli", "fleet", "config", "fabric-listener",
                        "--path", str(central)], cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_overlay_is_static_and_isolated():
    doc = yaml.safe_load((CP / "compose.collab-fabric.yaml").read_text())
    side = doc["services"]["fabric-whois"]
    assert side["read_only"] is True and side["cap_drop"] == ["ALL"] and side["network_mode"] == "none"
    assert "no-new-privileges:true" in side["security_opt"]
    assert "environment" not in side and "env_file" not in side
    assert side["user"].startswith("${HERDR_COLLAB_CERT_UID")      # never root
    # the tailscaled socket directory is read-only; the only writable mount is the sidecar's own socket dir
    rw = [v for v in side["volumes"] if not v.endswith(":ro")]
    assert len(rw) == 1 and rw[0].endswith(":/run/fabric-whois")
    assert any(v.endswith("/var/run/tailscale:ro") for v in side["volumes"])
    pub = doc["services"]["collab-public"]
    assert set(pub) == {"volumes", "ports", "networks"}            # no capability, environment or command change
    assert all("tailscale" not in v for v in pub["volumes"])       # collab-public never sees tailscaled
    assert pub["volumes"] == ["${HERDR_COLLAB_FABRIC_WHOIS_DIR:?run deploy.sh}:/run/fabric-whois:ro"]
    assert len(pub["ports"]) == 1 and pub["ports"][0].startswith("${HERDR_COLLAB_FABRIC_BIND:?")
    assert doc["networks"]["fabric"]["driver_opts"]["com.docker.network.bridge.enable_ip_masquerade"] == "false"


def test_fabric_network_has_a_fixed_subnet_and_the_comments_do_not_claim_no_route():
    text = (CP / "compose.collab-fabric.yaml").read_text()
    doc = yaml.safe_load(text)
    net = doc["networks"]["fabric"]
    assert net["ipam"]["config"][0]["subnet"] == "172.30.213.0/24"
    assert "internal" not in net or net["internal"] is False          # a published port needs a non-internal network
    low = text.lower()
    assert "no outbound route" not in low and "adds no route" not in low
    assert "collab-fabric-firewall.sh" in text


def test_the_boot_order_drop_in_waits_for_the_tailnet_address():
    drop = (CP / "docker-after-tailscale.conf").read_text()
    assert "After=tailscaled.service" in drop and "Wants=tailscaled.service" in drop
    assert "ExecStartPre=" in drop and "tailscale ip" in drop


def test_deploy_script_warns_about_the_firewall_and_the_drop_in():
    sh = (CP / "deploy.sh").read_text()
    assert "collab-fabric-firewall.sh" in sh and "--check" in sh
    assert "/etc/systemd/system/docker.service.d/after-tailscale.conf" in sh


def test_base_collab_command_carries_the_inert_bind_flag():
    cmd = yaml.safe_load((CP / "compose.collab.yaml").read_text())["services"]["collab-public"]["command"]
    assert cmd[cmd.index("--collab-fabric-bind-host") + 1] == "0.0.0.0"


def test_deploy_script_layers_the_overlay_only_for_a_configured_listener():
    sh = (CP / "deploy.sh").read_text()
    assert "fleet config fabric-listener" in sh
    assert "compose.collab-fabric.yaml" in sh
    assert 'if [ "$FABRIC" = 1 ]' in sh and "stop_fabric_whois" in sh
    assert subprocess.run(["bash", "-n", str(CP / "deploy.sh")]).returncode == 0


@pytest.mark.skipif(not _compose_ok(), reason="docker compose not available")
def test_overlay_renders_with_the_real_compose(tmp_path):
    central = _central(tmp_path, dict(TRUST))
    envfile = tmp_path / "compose.env"
    fcc.render_compose(load_fleet_config(central), envfile)
    slice_ = tmp_path / "collab" / "collab-config.json"
    fcc.render_collab(load_fleet_config(central), slice_)
    extra = {"HERDR_COLLAB_CONFIG_DIR": str(slice_.parent), "HERDR_COLLAB_DATA_DIR": str(tmp_path / "data"),
             "HERDR_COLLAB_SYNC_DIR": str(tmp_path / "sync"), "HERDR_SHARE_CONFIG_DIR": str(tmp_path / "share"),
             "HERDR_COLLAB_EDGE_CONF": str(CP / "collab-edge" / "nginx.conf"),
             "HERDR_COLLAB_FABRIC_BIND": "100.64.0.7", "HERDR_COLLAB_FABRIC_PORT": "8484",
             "HERDR_COLLAB_FABRIC_WHOIS_DIR": str(tmp_path / "whois")}
    r = _config(central, envfile, "compose.yaml", "compose.collab.yaml", "compose.collab-fabric.yaml",
                fmt="json", extra_env=extra)
    assert r.returncode == 0, r.stderr
    model = json.loads(r.stdout)
    pub = model["services"]["collab-public"]
    assert {"collab", "fabric"} == set(pub["networks"])
    assert any(p.get("host_ip") == "100.64.0.7" and str(p.get("published")) == "8484" and p.get("target") == 8484
               for p in pub["ports"])
    assert any(v["target"] == "/run/fabric-whois" and v.get("read_only") for v in pub["volumes"])
    assert not any("tailscale" in v["source"] for v in pub["volumes"])
    assert pub["cap_drop"] == ["ALL"] and "--collab-fabric-bind-host" in pub["command"]
    side = model["services"]["fabric-whois"]
    assert side["network_mode"] == "none" and side["read_only"] is True
    assert side["command"][:2] == ["fabric", "whois-proxy"]
    # the sidecar's socket is exactly where collab-public's config says to look
    assert TRUST["whois_socket"] == "/run/fabric-whois/whois.sock"
    assert side["command"][side["command"].index("--listen-socket") + 1] == TRUST["whois_socket"]
    # without the overlay nothing is published or added
    plain = json.loads(_config(central, envfile, "compose.yaml", "compose.collab.yaml", fmt="json",
                               extra_env=extra).stdout)
    assert "fabric-whois" not in plain["services"]
    assert not any(p.get("target") == 8484 for p in plain["services"]["collab-public"].get("ports", []))
