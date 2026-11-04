"""Offline `docker compose config` checks of the `memory` profile in deploy/control-plane/compose.yaml.
Read-only (`config` never builds, pulls or starts anything); dummy generated values in tmp dirs only."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering import fleet_memory as fm
from herdr_engineering.fleet_config import load_fleet_config

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
COMPOSE = CP / "compose.yaml"


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
MEMORY_SERVICES = {"hindsight-db", "hindsight-api"}
REAL_DOCKER_CONFIG = os.environ.get("DOCKER_CONFIG") or str(Path.home() / ".docker")   # CLI plugins live there


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _setup(tmp_path, memory=True, **kw):
    central = tmp_path / "fleet" / "fleet-config.yaml"
    fcc.init_config(central, dict(ANSWERS))
    if memory:
        fm.init_memory(central, enable=True, endpoint="https://memory.dev.lan", model_dir=str(tmp_path / "model"), **kw)
    envdir = tmp_path / "env"
    envdir.mkdir(mode=0o700)
    envfile = envdir / "compose.env"
    fcc.render_compose(load_fleet_config(central), envfile)
    return central, envfile


def _config(central, envfile, *, profile, fmt="json", env_file=True):
    cmd = ["docker", "compose"] + (["--env-file", str(envfile)] if env_file else []) + ["-p", "t", "-f", str(COMPOSE)]
    cmd += (["--profile", "memory"] if profile else []) + ["config"] + (["--format", fmt] if fmt else ["-q"])
    d = central.parent
    env = {**os.environ, "DOCKER_CONFIG": REAL_DOCKER_CONFIG, "HERDR_CP_FLEET_DIR": str(d), "HERDR_LLM_SLICE_DIR": str(d), "HERDR_CP_IMAGE_TAG": "t",
           "HERDR_CP_GIT_COMMIT": "t", "HERDR_CP_GITHUB_APP_DIR": str(d), "HERDR_CP_APPS_DIR": str(d),
           "HERDR_CP_PIPELINES_DIR": str(d), "HERDR_SHARES_DIR": str(d), "HERDR_SHARE_EVENTS_DIR": str(d),
           "HERDR_COLLAB_MIRROR_DIR": str(d), "HERDR_COLLAB_OUTBOX_DIR": str(d), "HERDR_COLLAB_SPOOL_DIR": str(d)}
    for k in list(env):
        if k.startswith(("HERDR_MEMORY_", "HERDR_CP_PG", "HERDR_CP_MINIO", "GATEWAY_")):
            del env[k]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def _model(central, envfile, profile=True, **kw):
    r = _config(central, envfile, profile=profile, **kw)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _mem(central):
    return yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]


def test_profile_is_inert_unless_enabled_and_changes_nothing_else(tmp_path):
    central, envfile = _setup(tmp_path)
    off = _model(central, envfile, profile=False)
    on = _model(central, envfile, profile=True)
    assert not MEMORY_SERVICES & set(off["services"])
    assert MEMORY_SERVICES <= set(on["services"])
    for name, svc in off["services"].items():              # every pre-existing service is byte-for-byte the same
        assert on["services"][name] == svc, name


def test_memory_off_in_config_renders_no_memory_variables_and_compose_still_validates(tmp_path):
    central, envfile = _setup(tmp_path, memory=False)
    assert "HERDR_MEMORY" not in envfile.read_text()
    assert _config(central, envfile, profile=False, fmt=None).returncode == 0
    assert _config(central, envfile, profile=True, fmt=None).returncode == 0     # inert defaults, nothing to start


def test_database_is_internal_only_pinned_by_digest_with_a_named_volume(tmp_path):
    central, envfile = _setup(tmp_path)
    m = _model(central, envfile)
    db = m["services"]["hindsight-db"]
    assert "@sha256:" in db["image"] and db["image"].startswith("pgvector/pgvector@")
    assert not db.get("ports")
    assert list(db["networks"]) == ["hindsight-internal"]
    assert m["networks"]["hindsight-internal"]["internal"] is True
    assert [(v["type"], v["source"]) for v in db["volumes"]] == [("volume", "herdr-hindsight-db")]
    assert db["mem_limit"] == "4294967296"
    assert db["healthcheck"]["test"][0] == "CMD-SHELL"


def test_api_is_loopback_only_hardened_and_reads_rendered_variables(tmp_path):
    central, envfile = _setup(tmp_path, port=8899)
    api = _model(central, envfile)["services"]["hindsight-api"]
    assert [(p["host_ip"], int(p["published"]), p["target"]) for p in api["ports"]] == [("127.0.0.1", 8899, 8888)]
    assert api["security_opt"] == ["no-new-privileges:true"] and api["cap_drop"] == ["ALL"]
    assert api["pids_limit"] == 512 and api["mem_limit"] == "6442450944" and float(api["cpus"]) == 4.0
    assert api["image"] == f"herdr-memory-hindsight:{_mem(central)['image_tag']}"
    assert set(api["networks"]) == {"hindsight-internal", "hindsight-access"}
    model_mount = [v for v in api["volumes"] if v["target"] == "/models"]
    assert model_mount[0]["source"] == str(tmp_path / "model") and model_mount[0]["read_only"] is True
    mem = _mem(central)
    users = api["environment"]["HINDSIGHT_API_TENANT_USERS"].split(",")
    assert users == [f"{t}:{mem['tenants'][t]}" for t in fm.TENANTS]
    assert api["environment"]["HINDSIGHT_API_DATABASE_URL"] == \
        f"postgresql://hindsight:{mem['db_password']}@hindsight-db:5432/hindsight"
    assert api["depends_on"]["hindsight-db"]["condition"] == "service_healthy"
    assert api["environment"]["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    assert api["environment"]["HF_HUB_OFFLINE"] == "1"


def test_no_other_service_joins_the_memory_networks_and_nothing_publishes_publicly(tmp_path):
    central, envfile = _setup(tmp_path)
    m = _model(central, envfile)
    for name, svc in m["services"].items():
        if name not in MEMORY_SERVICES:
            assert not {"hindsight-internal", "hindsight-access"} & set(svc.get("networks") or {}), name
    for name in MEMORY_SERVICES:
        for p in m["services"][name].get("ports") or []:
            assert p["host_ip"] == "127.0.0.1"
    assert "internal" not in m["networks"]["hindsight-access"] or not m["networks"]["hindsight-access"]["internal"]


def test_personal_tenant_has_its_own_key_in_the_tenant_list(tmp_path):
    central, envfile = _setup(tmp_path)
    users = dict(p.split(":") for p in
                 _model(central, envfile)["services"]["hindsight-api"]["environment"]["HINDSIGHT_API_TENANT_USERS"].split(","))
    assert set(users) == set(fm.TENANTS) and len(set(users.values())) == len(fm.TENANTS)


def test_no_secret_is_written_in_compose_yaml_and_none_is_inlined(tmp_path):
    text = COMPOSE.read_text()
    assert "HINDSIGHT_API_TENANT_USERS: \"${HERDR_MEMORY_TENANT_USERS:-}\"" in text
    assert "POSTGRES_PASSWORD: \"${HERDR_MEMORY_DB_PASSWORD:-}\"" in text
    central, _ = _setup(tmp_path)
    mem = _mem(central)
    assert not any(v in text for v in [mem["db_password"], *mem["tenants"].values()])


@pytest.mark.parametrize("var,path", [
    ("HERDR_MEMORY_DB_PASSWORD", lambda s: s["hindsight-db"]["environment"]["POSTGRES_PASSWORD"]),
    ("HERDR_MEMORY_TENANT_USERS", lambda s: s["hindsight-api"]["environment"]["HINDSIGHT_API_TENANT_USERS"]),
])
def test_every_secret_variable_is_required_and_an_empty_value_fails_closed(tmp_path, var, path):
    """`:?` would break deploys that do not use memory (compose interpolates inactive profiles), so a missing
    variable resolves to EMPTY - which the images refuse (postgres: no password; tenant extension: no keys) - while
    render-compose and deploy.sh --with-memory refuse earlier, by name. Prove all three layers."""
    central, envfile = _setup(tmp_path)
    kept = [ln for ln in envfile.read_text().splitlines() if not ln.startswith(var + "=")]
    envfile.write_text("\n".join(kept) + "\n")
    assert path(_model(central, envfile)["services"]) in ("", "postgresql://hindsight:@hindsight-db:5432/hindsight")
    # layer 2 + 3: the lib refuses a rendered file without it, naming the variable
    lib = CP / "memory-deploy.sh"
    r = subprocess.run(["bash", "-c", f'. "{lib}"; ENV_FILE="{envfile}"; CENTRAL=c; DRY=0; uname() {{ echo x86_64; }}; '
                        'memory_preflight'], capture_output=True, text=True)
    assert r.returncode == 1 and var in r.stderr


def test_model_dir_default_never_mounts_a_real_host_path(tmp_path):
    central, envfile = _setup(tmp_path)
    kept = [ln for ln in envfile.read_text().splitlines() if not ln.startswith("HERDR_MEMORY_MODEL_DIR=")]
    envfile.write_text("\n".join(kept) + "\n")
    api = _model(central, envfile)["services"]["hindsight-api"]
    assert [v["source"] for v in api["volumes"]] == ["/nonexistent-herdr-memory-model-dir"]
