"""Offline `docker compose config` validation of compose.yaml against a rendered
env file. Read-only: `config` never builds, pulls or starts anything. Everything
lives in tmp dirs with generated, throwaway values."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import load_fleet_config

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"


def _compose_ok():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _compose_ok(), reason="docker compose not available")

ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
    "gateway_tailnet_ip": "100.64.0.7", "hosts": {"bender": ["builder"]},
}


def _render(tmp_path, mutate=None):
    central = tmp_path / "fleet-config.yaml"
    fcc.init_config(central, dict(ANSWERS))
    if mutate:
        data = yaml.safe_load(central.read_text())
        mutate(data["sections"]["control_plane"]["values"]["compose"])
        central.unlink()
        fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    envdir = tmp_path / "env"
    envdir.mkdir(mode=0o700)
    envfile = envdir / "compose.env"
    fcc.render_compose(load_fleet_config(central), envfile)
    return central, envfile


def _config(central, envfile, *files, fmt=None, extra_env=None):
    cmd = ["docker", "compose", "--env-file", str(envfile), "-p", "t"]
    for f in files:
        cmd += ["-f", str(CP / f)]
    cmd += ["config"] + (["--format", fmt] if fmt else ["-q"])
    env = dict(os.environ, HERDR_CP_FLEET_DIR=str(central.parent),
               HERDR_LLM_SLICE_DIR=str(central.parent / "llm-proxy"), HERDR_CP_IMAGE_TAG="t",
               HERDR_CP_GIT_COMMIT="t", HERDR_CP_GITHUB_APP_DIR=str(central.parent / "github-app"),
               HERDR_CP_APPS_DIR=str(central.parent / "apps"),
               HERDR_CP_PIPELINES_DIR=str(central.parent / "pipelines"),
               HERDR_SHARES_DIR=str(central.parent / "shares"),
               HERDR_SHARE_EVENTS_DIR=str(central.parent / "share-events"), **(extra_env or {}))
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def test_rendered_env_validates_against_compose_yaml(tmp_path):
    central, envfile = _render(tmp_path)
    r = _config(central, envfile, "compose.yaml")
    assert r.returncode == 0, r.stderr


def test_rendered_env_validates_with_the_collab_overlay(tmp_path):
    # Updated in the origin/main merge: the overlay no longer overrides control-plane-web
    # (collab moved to its own collab-public container), which reads only the
    # render-collab slice; docker-level collab vars come from control_plane.compose.
    central, envfile = _render(tmp_path, lambda c: c.update(HERDR_COLLAB_CERT_DIR=str(tmp_path / "acme"),
                                                            HERDR_COLLAB_OPERATOR_PORT="9555"))
    data = yaml.safe_load(central.read_text())
    data["sections"]["control_plane"]["values"]["collab"] = {"cognito_pool": "us-west-2_TEST",
                                                             "operator_user": "owner-test"}
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    fcc.render_compose(load_fleet_config(central), envfile)
    slice_ = tmp_path / "collab" / "collab-config.json"
    fcc.render_collab(load_fleet_config(central), slice_)
    extra = {"HERDR_COLLAB_CONFIG_DIR": str(slice_.parent), "HERDR_COLLAB_DATA_DIR": str(tmp_path / "data"),
             "HERDR_COLLAB_SYNC_DIR": str(tmp_path / "sync"),
             "HERDR_SHARE_CONFIG_DIR": str(tmp_path / "share"),
             "HERDR_COLLAB_EDGE_CONF": str(CP / "collab-edge" / "nginx.conf")}
    r = _config(central, envfile, "compose.yaml", "compose.collab.yaml", extra_env=extra)
    assert r.returncode == 0, r.stderr
    model = json.loads(_config(central, envfile, "compose.yaml", "compose.collab.yaml", fmt="json",
                               extra_env=extra).stdout)
    cmd = model["services"]["control-plane-web"]["command"]
    assert cmd[cmd.index("--fleet-config") + 1] == "/etc/herdr/fleet/fleet-config.yaml"
    # #111: the dashboard proxies collab to the operator listener on the tailnet bind
    assert [a for a in cmd if a.startswith("--collab")] == ["--collab-operator-url"]
    assert cmd[cmd.index("--collab-operator-url") + 1] == "http://127.0.0.1:9555"
    pub = model["services"]["collab-public"]
    assert "--collab-operator-user" not in pub["command"]
    assert pub["command"][pub["command"].index("--collab-operator-host") + 1] == "127.0.0.1:9555"
    assert pub["command"][pub["command"].index("--fleet-config") + 1] == "/etc/herdr/collab/collab-config.json"
    sources = {v["source"] for v in pub["volumes"]}
    assert str(slice_.parent) in sources and str(central.parent) not in sources
    assert not pub.get("environment")
    edge = model["services"]["collab-edge"]
    assert any(v["source"] == str(tmp_path / "acme") for v in edge["volumes"])
    assert any(p.get("host_ip") == "127.0.0.1" and str(p.get("published")) == "9555"
               and p.get("target") == 8082 for p in edge["ports"])


def test_gateway_bind_single_for_loopback_and_pair_for_tailnet(tmp_path):
    central, envfile = _render(tmp_path)
    cmd = json.loads(_config(central, envfile, "compose.yaml", fmt="json").stdout)["services"]["fabric-gateway"]["command"]
    assert cmd[cmd.index("--bind") + 1] == "127.0.0.1,100.64.0.7"
    c2, e2 = _render(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path,
                     lambda c: c.update(GATEWAY_TAILNET_IP="127.0.0.1"))
    cmd = json.loads(_config(c2, e2, "compose.yaml", fmt="json").stdout)["services"]["fabric-gateway"]["command"]
    assert cmd[cmd.index("--bind") + 1] == "127.0.0.1"


HOSTILE = ["pa$$word", "it's \"quoted\"", "line1\nline2", "trailing\\", "${HOME}x", "a b#c=d"]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_secret_round_trips_through_rendered_file_and_compose(tmp_path, value):
    central, envfile = _render(tmp_path, lambda c: c.update(HERDR_CP_PG_PASSWORD=value,
                                                           HERDR_CP_MINIO_PASSWORD=value))
    r = _config(central, envfile, "compose.yaml", fmt="json")
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]

    def real(v):  # `config` re-escapes a literal $ as $$ so its output can be re-read
        return v.replace("$$", "$")
    assert real(svc["postgres"]["environment"]["POSTGRES_PASSWORD"]) == value
    assert real(svc["rustfs"]["environment"]["MINIO_ROOT_PASSWORD"]) == value
