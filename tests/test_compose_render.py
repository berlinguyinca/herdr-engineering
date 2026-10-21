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
    "llm_upstream_key": "llm-secret-key", "ci_url": "https://ci.example.test",
    "ci_token": "ci-secret-token", "web_bind": "127.0.0.1", "web_port": "8080",
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


def _config(central, envfile, *files, fmt=None):
    cmd = ["docker", "compose", "--env-file", str(envfile), "-p", "t"]
    for f in files:
        cmd += ["-f", str(CP / f)]
    cmd += ["config"] + (["--format", fmt] if fmt else ["-q"])
    env = dict(os.environ, HERDR_CP_FLEET_CONFIG_FILE=str(central), HERDR_CP_IMAGE_TAG="t",
               HERDR_CP_GIT_COMMIT="t")
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def test_rendered_env_validates_against_compose_yaml(tmp_path):
    central, envfile = _render(tmp_path)
    r = _config(central, envfile, "compose.yaml")
    assert r.returncode == 0, r.stderr


def test_rendered_env_validates_with_the_collab_overlay(tmp_path):
    central, envfile = _render(tmp_path)
    r = _config(central, envfile, "compose.yaml", "compose.collab.yaml")
    assert r.returncode == 0, r.stderr
    model = json.loads(_config(central, envfile, "compose.yaml", "compose.collab.yaml", fmt="json").stdout)
    cmd = model["services"]["control-plane-web"]["command"]
    assert cmd[cmd.index("--fleet-config") + 1] == "/etc/herdr/fleet-config.yaml"


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
