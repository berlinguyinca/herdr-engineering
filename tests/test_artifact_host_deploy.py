"""deploy.sh and the artifact host: the gate, the dry run, the installed files and the order of the compose calls.

Nothing starts a container: deploy.sh runs with the stub docker of test_deploy_scripts.py (the same harness the share
tests use); the real-Docker checks live in test_artifact_host_compose.py."""

from __future__ import annotations

import importlib.util
import re
import secrets
import shutil
import stat
from pathlib import Path

import pytest
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


def _host_log(env) -> list[str]:
    return [ln for ln in env["log"].read_text().splitlines() if "/deploy/artifact-host/compose.yaml " in ln]


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
    names = sorted(p.name for p in ah.iterdir() if p.is_file())
    assert names == [
        "db-admin-password",
        "internal.json",
        "migrate.json",
        "public.json",
        "roles.sql",
    ]
    for p in ah.iterdir():
        if p.is_file():
            want = 0o600 if p.name == "roles.sql" else 0o644
            assert stat.S_IMODE(p.stat().st_mode) == want, p.name
    edge = (env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf").read_text()
    assert f"server_name {HOST};" in edge and "location = /mcp/artifacts" in edge
    log = env["log"].read_text().splitlines()
    assert any(ln.startswith("docker build") and "herdr-artifact-host:" in ln for ln in log)
    idx = {
        "db": next(i for i, ln in enumerate(log) if "artifact-host/compose.yaml" in ln and " up " in ln),
        "roles": next(i for i, ln in enumerate(log) if " exec -T artifact-host-db psql " in ln),
        "dump": next(i for i, ln in enumerate(log) if " exec -T artifact-host-db pg_dump " in ln),
        "migrate": next(i for i, ln in enumerate(log) if " run --rm artifact-host-migrate" in ln),
        "roles_up": max(
            i for i, ln in enumerate(log) if "artifact-host/compose.yaml" in ln and " up " in ln
        ),
        "cp": next(
            i
            for i, ln in enumerate(log)
            if "compose.artifact-host.yaml" in ln and " up " in ln and "collab-edge" in ln
        ),
    }
    assert idx["db"] < idx["roles"] < idx["dump"] < idx["migrate"] < idx["roles_up"] < idx["cp"], idx
    assert "--wait" in log[idx["roles_up"]]


def test_a_routine_deploy_never_recreates_the_database_container(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    assert _ds.deploy(env).returncode == 0
    ups = [ln for ln in _host_log(env) if " up " in ln]
    assert ups
    for ln in ups:
        if "--force-recreate" in ln:
            assert "artifact-host-db" not in ln and "--no-deps" in ln, ln
    assert "artifact-host-db" in ups[0] and "--force-recreate" not in ups[0]
    assert "artifact-host-public" in ups[-1] and "artifact-host-relay" in ups[-1]


def test_the_database_is_backed_up_before_the_migration_and_a_failed_backup_stops_it(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    env["env"]["STUB_FAIL_AH"] = "pg_dump"
    r = _ds.deploy(env)
    assert r.returncode == 1, r.stdout + r.stderr
    host = _host_log(env)
    assert any(" pg_dump " in ln for ln in host)
    assert not any("artifact-host-migrate" in ln for ln in host), "no migration after a failed backup"
    assert "backup" in r.stderr
    ah = env["home"] / ".config/herdr-engineering/artifact-host"
    assert not list(ah.glob("backups/*.dump")), "the empty dump of a failed backup is removed"


def test_a_successful_backup_is_kept_private(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    assert _ds.deploy(env).returncode == 0
    ah = env["home"] / ".config/herdr-engineering/artifact-host"
    dumps = list((ah / "backups").glob("pre-*.dump"))
    assert len(dumps) == 1
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((ah / "backups").stat().st_mode) == 0o700


def test_the_certificate_must_name_the_artifact_host(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST)  # a certificate that does not name the host
    r = _ds.deploy(env)
    assert r.returncode != 0
    assert HOST in r.stderr and "certificate" in r.stderr
    _no_secrets(r.stdout + r.stderr)
    assert not any(" run --rm artifact-host-migrate" in ln for ln in env["log"].read_text().splitlines())


def test_the_certificate_check_passes_when_it_names_the_host_and_the_unit_keeps_checking(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    assert _ds.deploy(env).returncode == 0
    unit = (env["home"] / ".config/systemd/user/herdr-collab-sync.service").read_text()
    assert f"--share-host {HOST}" in unit


def test_a_successful_deploy_probes_the_hosts_own_name(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"curl https://{HOST}/s/probe/" in env["log"].read_text()


@pytest.mark.parametrize(
    "name",
    [
        "HERDR_ARTIFACT_HOST_PROJECT",
        "HERDR_ARTIFACT_NET_PREFIX",
        "HERDR_ARTIFACT_EDGE_SUBNET",
        "HERDR_ARTIFACT_INTERNAL_SUBNET",
        "HERDR_ARTIFACT_INTERNAL_IP",
        "HERDR_ARTIFACT_HOST_DIR",
        "HERDR_ARTIFACT_HOST_TAG",
    ],
)
def test_the_hosts_fixed_names_and_addresses_are_not_deploy_settings(env, name: str) -> None:
    _set(env, _section())
    env["env"][name] = "172.31.9.0/28" if "SUBNET" in name else "elsewhere"
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode == 2, r.stdout + r.stderr
    assert name in r.stderr and "fixed" in r.stderr
    assert not (env["log"].exists() and env["log"].read_text().strip()), "nothing ran"


# ---------------------------------------------------------------------------------------------------------- rollback


def _host_commit() -> str:
    """A committed release that already ships the artifact host (so it can be a rollback target)."""
    return _ds._git(
        "log", "--format=%H", "--", "deploy/artifact-host/compose.yaml"
    ).splitlines()[-1]


def _mark(env, state: str) -> None:
    marker = env["home"] / ".config/herdr-engineering/artifact-host.state"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(state)


def test_failed_enable_rolls_back_without_the_overlay_and_stops_the_host(env) -> None:
    """Enabled for the first time (the previous release ran no host): a failed control-plane up leaves no host and no overlay."""
    prev_full, prev_tag = _ds._seed_previous(env, _ds._layout_commit())
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    r = _ds.deploy(env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"rolled back to herdr-engineering:{prev_tag}" in r.stderr
    files = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("composefiles ")]
    assert "compose.artifact-host.yaml" in files[0]
    rb = [ln for ln in files if ln.startswith(f"composefiles tag={prev_tag} ")]
    assert rb and "compose.artifact-host.yaml" not in rb[0], "the previous release has no overlay"
    log = env["log"].read_text()
    assert "label=com.docker.compose.project=herdr-artifact-host" in log
    ah = env["home"] / ".config/herdr-engineering/artifact-host"
    assert not (ah / "public.json").exists(), "the files this deploy installed are removed again"
    assert not (env["home"] / ".config/herdr-engineering/artifact-host.state").exists()
    _ds._assert_deploy_worktrees_removed(env)


def test_failed_disable_restores_the_overlay_and_the_host(env) -> None:
    """The host ran before; disabling it fails in the control-plane up: the rollback brings the overlay AND the host back."""
    prev_full, prev_tag = _ds._seed_previous(env, _host_commit())
    _set(env, _section(enabled=False))
    _mark(env, "enabled")
    ah = env["home"] / ".config/herdr-engineering/artifact-host"
    ah.mkdir(parents=True)
    (ah / "roles.sql").write_text("-- the previous release's roles\n")
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    r = _ds.deploy(env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"rolled back to herdr-engineering:{prev_tag}" in r.stderr
    assert "NOT FULLY" not in r.stderr
    files = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("composefiles ")]
    assert "compose.artifact-host.yaml" not in files[0], "the failed forward deploy ran without the host"
    rb = next(ln for ln in files if ln.startswith(f"composefiles tag={prev_tag} "))
    assert "rollback-worktree/deploy/control-plane/compose.artifact-host.yaml" in rb
    prev_host = [
        ln for ln in _host_log(env) if "rollback-worktree/deploy/artifact-host/compose.yaml" in ln
    ]
    assert any(" up " in ln and "artifact-host-db" in ln for ln in prev_host), prev_host
    assert any(" exec -T artifact-host-db psql " in ln for ln in prev_host), "the roles are re-applied"
    assert any(" up " in ln and "artifact-host-public" in ln for ln in prev_host)
    assert not any("migrate" in ln for ln in prev_host)
    assert (ah / "roles.sql").read_text() == "-- the previous release's roles\n"
    _ds._assert_deploy_worktrees_removed(env)


def test_rollback_after_a_migration_fails_loudly_with_the_manual_steps(env) -> None:
    """Enabled before and now: the roles and the schema were touched, so the host is NOT silently reverted."""
    prev_full, prev_tag = _ds._seed_previous(env, _host_commit())
    _set(env, _section())
    _mark(env, "enabled")
    ah = env["home"] / ".config/herdr-engineering/artifact-host"
    ah.mkdir(parents=True)
    (ah / "roles.sql").write_text("-- previous\n")
    _cert(env, MCP_HOST, HOST)
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    r = _ds.deploy(env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "ARTIFACT HOST NOT REVERTED" in r.stderr
    assert "pg_restore" in r.stderr and "stop artifact-host-public" in r.stderr
    assert "--ref" in r.stderr  # the redeploy command of the previous release
    # the control plane itself went back, with the overlay, so nothing is left mixed on its side
    files = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("composefiles ")]
    rb = next(ln for ln in files if ln.startswith(f"composefiles tag={prev_tag} "))
    assert "compose.artifact-host.yaml" in rb
    assert not any(
        "rollback-worktree/deploy/artifact-host/compose.yaml" in ln for ln in _host_log(env)
    ), "the previous host code is not started against a changed database"
    _no_secrets(r.stdout + r.stderr)


def test_a_failed_migration_with_no_previous_host_is_rolled_back_and_the_host_stopped(env) -> None:
    prev_full, prev_tag = _ds._seed_previous(env, _ds._layout_commit())
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    env["env"]["STUB_FAIL_AH"] = "artifact-host-migrate"
    r = _ds.deploy(env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"rolled back to herdr-engineering:{prev_tag}" in r.stderr
    assert "label=com.docker.compose.project=herdr-artifact-host" in env["log"].read_text()


def test_a_successful_deploy_records_the_state_a_later_rollback_restores(env) -> None:
    _set(env, _section())
    _cert(env, MCP_HOST, HOST)
    assert _ds.deploy(env).returncode == 0
    marker = env["home"] / ".config/herdr-engineering/artifact-host.state"
    assert marker.read_text().strip() == "enabled"
    _set(env, _section(enabled=False), collab=False)
    assert _ds.deploy(env).returncode == 0
    assert marker.read_text().strip() == "disabled"


def test_deploy_uses_the_repo_virtualenv_not_the_bare_system_python(env, tmp_path) -> None:
    """The fleet commands need the project's dependencies: a `python3` first on PATH that lacks them must not matter."""
    real = shutil.which("python3")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "python3").write_text(
        "#!/bin/sh\ncase \"$*\" in *herdr_engineering.cli*) echo 'No module named yaml' >&2; exit 1 ;; esac\n"
        f'exec {real} "$@"\n'
    )
    (fake / "python3").chmod(0o755)
    env["env"]["PATH"] = f"{fake}:{env['env']['PATH']}"
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr


def test_disabling_the_host_stops_its_project_and_installs_nothing(env) -> None:
    _set(env, _section(enabled=False))
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (env["home"] / ".config/herdr-engineering/artifact-host").exists()
    log = env["log"].read_text()
    assert "label=com.docker.compose.project=herdr-artifact-host" in log
    assert "compose.artifact-host.yaml" not in log
