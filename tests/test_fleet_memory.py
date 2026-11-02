"""control_plane.memory: validation, host-side generation, compose rendering. Everything is a throwaway
value in tmp dirs; the assertions are mostly about what must NOT appear (secrets in stdout/stderr/logs)."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering import fleet_memory as fm
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ROOT = Path(__file__).resolve().parents[1]
ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
    "gateway_tailnet_ip": "100.64.0.7", "hosts": {"bender": ["builder"]},
}


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture(autouse=True)
def _private_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def central(tmp_path):
    return fcc.init_config(tmp_path / "fleet" / "fleet-config.yaml", dict(ANSWERS))


def enable(central, **kw):
    kw.setdefault("endpoint", "https://memory.dev.lan")
    kw.setdefault("model_dir", "/srv/models/e5")
    return fm.init_memory(central, enable=True, **kw)


def secrets_of(central):
    mem = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]
    return [mem["db_password"], *mem["tenants"].values()]


def edit(central, fn):
    data = yaml.safe_load(central.read_text())
    fn(data["sections"]["control_plane"]["values"])
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))


def test_profile_is_off_by_default_nothing_is_rendered(central, tmp_path):
    cfg = load_fleet_config(central)
    assert fm.memory_enabled(cfg) is False
    assert not any(k.startswith("HERDR_MEMORY_") for k in fcc.compose_env(cfg))
    out = tmp_path / "e" / "compose.env"
    fcc.render_compose(cfg, out)
    assert "HERDR_MEMORY" not in out.read_text()


def test_disabled_block_with_secrets_still_renders_nothing(central):
    fm.init_memory(central, endpoint="https://memory.dev.lan", model_dir="/srv/models/e5")   # enabled stays false
    cfg = load_fleet_config(central)
    assert fm.memory_enabled(cfg) is False
    assert not any(k.startswith("HERDR_MEMORY_") for k in fcc.compose_env(cfg))


def test_init_generates_every_secret_separately_and_privately(central):
    made = enable(central)
    assert made == ["db_password", *(f"tenants.{t}" for t in fm.TENANTS)]
    vals = secrets_of(central)
    assert len(set(vals)) == 5 and all(len(v) >= 32 for v in vals)
    assert mode(central) == 0o600
    # the non-secret endpoint is published to every host; nothing else is
    pub = yaml.safe_load(central.read_text())["sections"]["memory"]
    assert pub == {"roles": ["all"], "values": {"url": "https://memory.dev.lan"}}
    cfg = load_fleet_config(central)
    for host in cfg.status()["hosts"]:
        assert cfg.slice_for(host)["memory"] == {"url": "https://memory.dev.lan"}
        assert not any(v in json.dumps(cfg.slice_for(host)) for v in vals)


def test_init_is_idempotent_keeps_secrets_and_rotates_only_named_tenant(central):
    enable(central)
    before = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]
    assert fm.init_memory(central) == []
    assert yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"] == before
    assert fm.init_memory(central, rotate=("personal",)) == ["tenants.personal"]
    after = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]
    assert after["db_password"] == before["db_password"]
    assert after["tenants"]["personal"] != before["tenants"]["personal"]
    assert {k: v for k, v in after["tenants"].items() if k != "personal"} == \
           {k: v for k, v in before["tenants"].items() if k != "personal"}
    with pytest.raises(FleetConfigError, match="unknown tenant"):
        fm.init_memory(central, rotate=("db_password",))


def test_render_compose_emits_the_five_variables_with_0600(central, tmp_path):
    enable(central, port=8899)
    out = tmp_path / "env" / "compose.env"
    fcc.render_compose(load_fleet_config(central), out)
    assert mode(out) == 0o600
    env = {}
    for line in out.read_text().splitlines():
        k, _, v = line.partition("=")
        env[k] = v.strip("'")
    for name in fm.MEMORY_COMPOSE_VARS:
        assert env[name]
    assert env["HERDR_MEMORY_PORT"] == "8899"
    assert env["HERDR_MEMORY_MODEL_DIR"] == "/srv/models/e5"
    users = dict(p.split(":") for p in env["HERDR_MEMORY_TENANT_USERS"].split(","))
    assert list(users) == list(fm.TENANTS) and len(set(users.values())) == 4
    # existing variables are unchanged
    assert env["HERDR_CP_PG_PASSWORD"]


def test_every_memory_secret_differs_from_postgres_and_minio(central):
    enable(central)
    pg = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["compose"]["HERDR_CP_PG_PASSWORD"]
    edit(central, lambda cp: cp["memory"].__setitem__("db_password", pg + pg))   # distinct
    assert fcc.compose_env(load_fleet_config(central))
    edit(central, lambda cp: cp["compose"].__setitem__("HERDR_CP_PG_PASSWORD", cp["memory"]["db_password"]))
    with pytest.raises(FleetConfigError, match="equals a Postgres/MinIO secret"):
        fcc.compose_env(load_fleet_config(central))


@pytest.mark.parametrize("drop", ["db_password", "tenants.project", "tenants.global", "tenants.infra",
                                  "tenants.personal", "endpoint", "model_dir", "image_tag", "port"])
def test_enabled_with_a_missing_key_is_refused_by_name(central, drop):
    enable(central)

    def cut(cp):
        if drop.startswith("tenants."):
            del cp["memory"]["tenants"][drop.split(".")[1]]
        else:
            del cp["memory"][drop]
    edit(central, cut)
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(central)
    assert drop in str(exc.value) and "enabled but missing" in str(exc.value)


@pytest.mark.parametrize("field,value,needle", [
    ("endpoint", "http://memory.dev.lan", "https"),
    ("endpoint", "https://user:pw@memory.dev.lan", "bare https origin"),
    ("endpoint", "https://memory.dev.lan/path", "bare https origin"),
    ("port", 80, "port"), ("port", 5432, "port"), ("port", "8888", "port"), ("port", True, "port"),
    ("model_dir", "relative/dir", "absolute"), ("model_dir", "/a:b", "absolute"),
    ("image_tag", "bad tag!", "image_tag"), ("enabled", "yes", "enabled"),
    ("surprise", 1, "unknown keys"),
])
def test_invalid_values_are_refused(central, field, value, needle):
    enable(central)
    edit(central, lambda cp: cp["memory"].__setitem__(field, value))
    with pytest.raises(FleetConfigError, match=needle):
        load_fleet_config(central)


def test_duplicate_or_malformed_keys_are_refused_without_echoing_them(central):
    enable(central)
    mem = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]
    leaked = mem["tenants"]["project"]
    edit(central, lambda cp: cp["memory"]["tenants"].__setitem__("personal", leaked))
    with pytest.raises(FleetConfigError, match="all be different") as exc:
        load_fleet_config(central)
    assert leaked not in str(exc.value)
    edit(central, lambda cp: cp["memory"]["tenants"].__setitem__("personal", "short,with:chars"))
    with pytest.raises(FleetConfigError, match="tenants.personal") as exc:
        load_fleet_config(central)
    assert "short,with:chars" not in str(exc.value)
    edit(central, lambda cp: cp["memory"]["tenants"].__setitem__("mallory", "x" * 40))
    with pytest.raises(FleetConfigError, match="unknown tenants: mallory"):
        load_fleet_config(central)


def test_public_section_refuses_anything_but_a_url(central):
    enable(central)
    data = yaml.safe_load(central.read_text())
    data["sections"]["memory"]["values"]["token"] = "x" * 40
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(FleetConfigError, match="only a non-secret `url`"):
        load_fleet_config(central)


def test_compose_mapping_cannot_set_memory_variables(central):
    enable(central)
    edit(central, lambda cp: cp["compose"].__setitem__("HERDR_MEMORY_PORT", "9"))
    with pytest.raises(FleetConfigError, match="must not set HERDR_MEMORY_PORT"):
        fcc.compose_env(load_fleet_config(central))


def test_port_colliding_with_a_project_port_is_refused(central):
    enable(central, port=8085)
    edit(central, lambda cp: cp["compose"].__setitem__("HERDR_LLM_PROXY_PORT", "8085"))
    with pytest.raises(FleetConfigError, match="collides"):
        fcc.compose_env(load_fleet_config(central))


def test_export_key_is_private_and_writes_only_that_tenant(central, tmp_path):
    enable(central)
    cfg = load_fleet_config(central)
    out = tmp_path / "keys" / "personal.key"
    fm.export_key(cfg, "personal", out)
    mem = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]
    assert out.read_text() == mem["tenants"]["personal"]
    assert mode(out) == 0o600 and mode(out.parent) == 0o700
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    os.chmod(open_dir, 0o755)
    with pytest.raises(FleetConfigError, match="chmod 700"):
        fm.export_key(cfg, "project", open_dir / "k")
    with pytest.raises(FleetConfigError, match="unknown tenant"):
        fm.export_key(cfg, "root", tmp_path / "keys" / "x")


def _cli(central, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "fleet", "config", "memory",
                           "--path", str(central), *args], capture_output=True, text=True, env=env, cwd=ROOT)


def test_cli_never_prints_a_secret(central, tmp_path):
    r = _cli(central, "init", "--enable", "--endpoint", "https://memory.dev.lan", "--model-dir", "/srv/models/e5")
    assert r.returncode == 0, r.stderr
    vals = secrets_of(central)
    assert "generated: db_password, tenants.project" in r.stdout
    out_all = r.stdout + r.stderr
    st = _cli(central, "status")
    assert st.returncode == 0 and json.loads(st.stdout)["tenants"] == {t: "set" for t in fm.TENANTS}
    ep = _cli(central, "endpoint")
    assert ep.stdout.strip() == "https://memory.dev.lan"
    key = tmp_path / "k" / "infra.key"
    ex = _cli(central, "export-key", "infra", "--out", str(key))
    assert ex.returncode == 0 and mode(key) == 0o600
    comp = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "fleet", "config", "render-compose",
                           "--path", str(central), "--out", str(tmp_path / "e" / "compose.env")],
                          capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=ROOT)
    assert comp.returncode == 0
    val = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "fleet", "config", "validate",
                          "--path", str(central)], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=ROOT)
    for blob in (out_all, st.stdout, st.stderr, ep.stdout, ex.stdout, ex.stderr, comp.stdout, comp.stderr,
                 val.stdout, val.stderr):
        assert not any(v in blob for v in vals)


def test_cli_missing_key_refusal_does_not_print_values(central):
    enable(central)
    edit(central, lambda cp: cp["memory"]["tenants"].pop("infra"))
    r = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "fleet", "config", "render-compose",
                        "--path", str(central), "--out", str(central.parent / "x.env")],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)), cwd=ROOT)
    assert r.returncode == 1 and "tenants.infra" in r.stderr
    assert not (central.parent / "x.env").exists()
    mem = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]["memory"]
    assert not any(v in r.stdout + r.stderr for v in [mem["db_password"], *mem["tenants"].values()])
