"""deploy.sh and the artifact host: the gate, the dry run, the installed files and the order of the compose calls.

Nothing starts a container: deploy.sh runs with the stub docker of test_deploy_scripts.py (the same harness the share
tests use); the real-Docker checks live in test_artifact_host_compose.py."""

from __future__ import annotations

import importlib.util
import re
import secrets
import stat
from pathlib import Path

import yaml

from herdr_engineering import fleet_config_cli as fcc

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_ah_deploy_harness", ROOT / "tests" / "test_deploy_scripts.py"
)
assert _spec and _spec.loader
_ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ds)
env = _ds.env
candidate_ref = _ds.candidate_ref

HOST = "artifacts.example.test"
MCP_HOST = "mcp.example.test"
SESSION = secrets.token_hex(32)
CLAIM = secrets.token_hex(32)
DBPW = secrets.token_hex(16)
COLLAB = {
    "cognito_pool": "us-west-2_TESTPOOL",
    "cognito_clients": ["client-a"],
    "cognito_group": "team.test",
    "cognito_web_client": "webclient1",
    "cognito_domain": "https://pool.auth.us-west-2.amazoncognito.com",
    "public_url": f"https://{MCP_HOST}/",
    "operator_user": "owner-test",
}


def _section(**over: object) -> dict:
    section: dict = {
        "enabled": True,
        "public_host": HOST,
        "mcp_public_hosts": [MCP_HOST],
        "session_secret": SESSION,
        "claim_key": CLAIM,
        "db_password": DBPW,
    }
    section.update(over)
    return section


def _set(e, section: dict | None, *, collab: bool = True, share: dict | None = None) -> None:
    if collab:
        _ds._with_collab(e, collab=COLLAB)
    data = yaml.safe_load(e["central"].read_text())
    cpv = data["sections"]["control_plane"]["values"]
    if section is not None:
        cpv["artifact_host"] = section
        cpv["artifact_library"] = {
            "host_url": "http://artifact-host-internal:8091",
            "host_key": "k" * 40,
        }
    if share is not None:
        cpv["share"] = share
    e["central"].unlink()
    fcc.write_private_file(e["central"], yaml.safe_dump(data, sort_keys=False))


def _cert(e, *names: str) -> None:
    from tests.test_share_deploy_config import _live_cert  # noqa: PLC0415

    _live_cert(e, *names)


def _no_secrets(text: str) -> None:
    for value in (SESSION, CLAIM, DBPW):
        assert value not in text


def test_dry_run_with_the_host_disabled_is_unchanged(env) -> None:
    _set(env, None)
    r0 = _ds.deploy(env, "--dry-run")
    assert r0.returncode == 0, r0.stderr
    _set(env, _section(enabled=False), collab=False)
    r1 = _ds.deploy(env, "--dry-run")
    assert r1.returncode == 0, r1.stderr
    norm = lambda s: re.sub(r"herdr-cp-env\.\w+", "<tmp>", s)  # noqa: E731
    assert norm(r1.stdout) == norm(r0.stdout)
    assert "artifact host" not in r1.stdout


def test_dry_run_with_the_host_enabled_renders_and_never_prints_secrets(env) -> None:
    _set(env, _section())
    before = _ds._home_files(env)
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "artifact host enabled" in r.stdout
    _no_secrets(r.stdout + r.stderr)
    assert _ds._home_files(env) == before
    config_calls = [ln for ln in env["log"].read_text().splitlines() if " config -q" in ln]
    assert any("artifact-host/compose.yaml" in ln for ln in config_calls)
    assert any("compose.artifact-host.yaml" in ln for ln in config_calls)


def test_enabled_host_without_the_collab_marker_is_refused(env) -> None:
    _set(env, _section(), collab=False)
    data = yaml.safe_load(env["central"].read_text())
    data["sections"]["control_plane"]["values"]["collab"] = dict(COLLAB)
    env["central"].unlink()
    fcc.write_private_file(env["central"], yaml.safe_dump(data, sort_keys=False))
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode != 0 and "collab" in r.stderr
    _no_secrets(r.stdout + r.stderr)


def test_enabled_host_alongside_share_is_refused(env) -> None:
    share = {
        "enabled": True,
        "signing_secret": secrets.token_hex(32),
        "public_url": "https://share.example.test/",
    }
    _set(env, _section(), share=share)
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode != 0
    assert (
        "central fleet config is invalid" in r.stderr
    )  # validate refuses; its detail is withheld by deploy.sh
    _no_secrets(r.stdout + r.stderr)


def test_incomplete_host_is_refused_without_naming_values(env) -> None:
    _set(env, _section(session_secret="short"))
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode != 0
    assert "central fleet config is invalid" in r.stderr
    _no_secrets(r.stdout + r.stderr)


def test_real_deploy_installs_the_files_and_brings_the_host_up_before_the_control_plane(
    env,
) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    _no_secrets(r.stdout + r.stderr)
    ah = env["home"] / ".config/herdr-engineering/artifact-host"
    assert stat.S_IMODE(ah.stat().st_mode) == 0o700
    assert sorted(p.name for p in ah.iterdir()) == [
        "artifact-host-config.json",
        "artifact-host-db-password",
    ]
    for p in ah.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o644
    assert (ah / "artifact-host-db-password").read_text() == DBPW
    edge = (env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf").read_text()
    assert f"server_name {HOST};" in edge and "location = /mcp/artifacts" in edge
    log = env["log"].read_text().splitlines()
    assert any(ln.startswith("docker build") and "herdr-artifact-host:" in ln for ln in log)
    migrate = next(i for i, ln in enumerate(log) if " run --rm artifact-host-public migrate" in ln)
    up = next(i for i, ln in enumerate(log) if "artifact-host/compose.yaml" in ln and " up " in ln)
    assert "--wait" in log[up]
    cp_up = next(
        i
        for i, ln in enumerate(log)
        if "compose.artifact-host.yaml" in ln and " up " in ln and "collab-edge" in ln
    )
    assert migrate < up < cp_up


def test_disabling_the_host_stops_its_project_and_installs_nothing(env) -> None:
    _set(env, _section(enabled=False))
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (env["home"] / ".config/herdr-engineering/artifact-host").exists()
    log = env["log"].read_text()
    assert "label=com.docker.compose.project=herdr-artifact-host" in log
    assert "compose.artifact-host.yaml" not in log
