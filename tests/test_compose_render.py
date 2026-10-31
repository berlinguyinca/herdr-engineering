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
               HERDR_SHARE_EVENTS_DIR=str(central.parent / "share-events"),
               HERDR_COLLAB_MIRROR_DIR=str(central.parent / "collab-mirror"),
               HERDR_COLLAB_OUTBOX_DIR=str(central.parent / "collab-outbox"),
               HERDR_COLLAB_SPOOL_DIR=str(central.parent / "collab-spool"), **(extra_env or {}))
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


def _collab_model(tmp_path):
    central, envfile = _render(tmp_path, lambda c: c.update(HERDR_COLLAB_CERT_DIR=str(tmp_path / "acme")))
    data = yaml.safe_load(central.read_text())
    data["sections"]["control_plane"]["values"]["collab"] = {"cognito_pool": "us-west-2_TEST",
                                                             "operator_user": "owner-test"}
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    slice_ = tmp_path / "collab" / "collab-config.json"
    fcc.render_collab(load_fleet_config(central), slice_)
    extra = {"HERDR_COLLAB_CONFIG_DIR": str(slice_.parent), "HERDR_COLLAB_DATA_DIR": str(tmp_path / "data"),
             "HERDR_COLLAB_SYNC_DIR": str(tmp_path / "sync"), "HERDR_SHARE_CONFIG_DIR": str(tmp_path / "share"),
             "HERDR_COLLAB_EDGE_CONF": str(CP / "collab-edge" / "nginx.conf")}
    r = _config(central, envfile, "compose.yaml", "compose.collab.yaml", fmt="json", extra_env=extra)
    assert r.returncode == 0, r.stderr
    return central, json.loads(r.stdout)


def _mounts(svc):
    return {v["target"]: (v["source"], bool(v.get("read_only"))) for v in svc["volumes"]}


def test_bridge_mounts_follow_the_trust_zones(tmp_path):
    central, model = _collab_model(tmp_path)
    worker = _mounts(model["services"]["control-plane-worker"])
    pub = model["services"]["collab-public"]
    pubm = _mounts(pub)
    d = central.parent
    assert worker["/collab-mirror"] == (str(d / "collab-mirror"), False)
    assert worker["/collab-outbox"] == (str(d / "collab-outbox"), False)
    assert worker["/collab-spool"] == (str(d / "collab-spool"), False)
    assert pubm["/collab-mirror"] == (str(d / "collab-mirror"), True)
    assert pubm["/collab-outbox"] == (str(d / "collab-outbox"), False)
    # the spool (assembled uploads) is the worker's alone
    assert "/collab-spool" not in pubm and str(d / "collab-spool") not in {s for s, _ in pubm.values()}
    # collab-public stays an island: no env_file, no Postgres/RustFS network, no capabilities
    assert "env_file" not in pub and not pub.get("environment")
    assert set(pub["networks"]) == {"collab"}
    assert pub["cap_drop"] == ["ALL"] and not pub.get("cap_add") and not pub.get("privileged")
    assert not any(k in pub for k in ("depends_on", "ports"))
    # the worker runs as root (no user:) so it can write the mirror and unlink in a uid-owned outbox
    assert "user" not in model["services"]["control-plane-worker"]
    # the web service sees none of the bridge directories
    assert not {"/collab-mirror", "/collab-outbox", "/collab-spool"} & set(_mounts(model["services"]["control-plane-web"]))
    for name in ("postgres", "rustfs", "llm-proxy", "share-public", "collab-edge"):
        assert not {"/collab-mirror", "/collab-outbox", "/collab-spool"} & set(_mounts(model["services"][name])), name


def test_collab_public_rw_mount_sources_are_pairwise_disjoint(tmp_path):
    """A compromised collab-public must not be able to hardlink or rename across trust zones: its
    writable mounts are separate sources and none is nested inside another (the outbox is its own
    bind mount, not a subdirectory of the data volume or the mirror)."""
    _, model = _collab_model(tmp_path)
    pub = model["services"]["collab-public"]
    rw = [Path(v["source"]) for v in pub["volumes"] if not v.get("read_only")]
    assert Path(_mounts(pub)["/collab-outbox"][0]) in rw and len(rw) >= 2
    every = [Path(v["source"]) for v in pub["volumes"]]
    assert len(set(every)) == len(every)
    for a in rw:
        for b in every:
            if a != b:
                assert a not in b.parents and b not in a.parents, (a, b)


def test_collab_public_command_carries_the_two_static_bridge_flags(tmp_path):
    _, model = _collab_model(tmp_path)
    cmd = model["services"]["collab-public"]["command"]
    assert cmd[cmd.index("--collab-artifacts-mirror") + 1] == "/collab-mirror"
    assert cmd[cmd.index("--collab-artifacts-outbox") + 1] == "/collab-outbox"


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


# The compose.yaml of the release before the apps mount, reduced to the part that matters: it still REQUIRES
# HERDR_CP_GOOGLE_DIR. Rolling back builds this file with compose_run's environment. Remove with the transition.
_PREVIOUS_COMPOSE = """
services:
  control-plane-web:
    image: x
    volumes:
      - ${HERDR_CP_GITHUB_APP_DIR:?run deploy.sh}:/etc/herdr/github-app:ro
      - ${HERDR_CP_GOOGLE_DIR:?run deploy.sh}:/etc/herdr/google:ro
"""


def test_a_rollback_to_the_previous_compose_still_renders_with_compose_runs_environment(tmp_path):
    import re
    script = (CP / "deploy.sh").read_text()
    body = script[script.index("compose_run() {"):]
    body = body[:body.index("\n}\n")]
    assert 'HERDR_CP_GOOGLE_DIR="$APPS_DIR"' in body and "HERDR_CP_APPS_DIR=" in body
    m = re.search(r'HERDR_CP_GOOGLE_DIR="\$APPS_DIR"', body)
    assert m
    apps = tmp_path / "apps"
    apps.mkdir()
    old = tmp_path / "compose.yaml"
    old.write_text(_PREVIOUS_COMPOSE)
    env = dict(os.environ, HERDR_CP_GITHUB_APP_DIR=str(tmp_path), HERDR_CP_GOOGLE_DIR=str(apps))
    r = subprocess.run(["docker", "compose", "-p", "t", "-f", str(old), "config", "-q"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
