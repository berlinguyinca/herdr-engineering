"""deploy.sh / deploy-fleet-agents.sh, exercised by subprocess with a stub `docker`
on PATH (nothing real is ever built, started or contacted)."""
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "control-plane" / "deploy.sh"
AGENTS = ROOT / "deploy" / "deploy-fleet-agents.sh"
ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "placeholder-llm-one", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-two", "web_bind": "127.0.0.1", "web_port": "8080", "gateway_tailnet_ip": "127.0.0.1",
    "github_token": "placeholder-gh-three", "hosts": {"bender": ["builder"]},
}


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    central = tmp_path / "central" / "fleet-config.yaml"
    central.parent.mkdir()
    fcc.init_config(central, dict(ANSWERS))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    stub = bindir / "docker"
    # Records each call; on `compose ... up` it snapshots the --env-file it was given
    # (existence, mode, directory mode) and fails when FAIL_COMPOSE is set.
    stub.write_text(f"""#!/usr/bin/env bash
echo "docker $*" >> {log}
if [ "$1" = compose ]; then
  echo "composeenv fleetdir=${{HERDR_CP_FLEET_DIR:-}} tag=${{HERDR_CP_IMAGE_TAG:-}} collabdir=${{HERDR_COLLAB_CONFIG_DIR:-}} llmdir=${{HERDR_LLM_SLICE_DIR:-}}" >> {log}
  if [ -n "${{HERDR_LLM_SLICE_DIR:-}}" ]; then echo "llmslice $(cat "$HERDR_LLM_SLICE_DIR/llm-config.json" | tr -d '\n')" >> {log}; fi
  prev=""
  for a in "$@"; do
    if [ "$prev" = --env-file ]; then
      echo "envfile $a mode=$(stat -c %a "$a") dirmode=$(stat -c %a "$(dirname "$a")")" >> {log}
    fi
    prev="$a"
  done
  [ -n "${{FAIL_COMPOSE:-}}" ] && exit 1
fi
exit 0
""")
    stub.chmod(0o755)
    secrets = {s for s in _strings(yaml.safe_load(central.read_text()))
               if len(s) >= 12 and "://" not in s and s not in ("control-plane",)}
    e = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    for k in list(e):
        if k.startswith(("HERDR_CP_", "FABRIC_")):
            del e[k]
    return {"home": home, "central": central, "log": log, "env": e, "secrets": secrets,
            "tmp": tmp_path}


def run(script, e, *args, timeout=120):
    return subprocess.run(["bash", str(script), *args], env=e["env"], capture_output=True,
                          text=True, timeout=timeout, cwd=ROOT)


def deploy(e, *args, central=True):
    base = ["--no-fetch", "--ref", "HEAD"]
    if central:
        base += ["--fleet-config", str(e["central"])]
    return run(DEPLOY, e, *base, *args)


def test_deploy_dry_run_renders_env_from_central_file_and_removes_it(env):
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "fleet config render-compose" in r.stdout
    assert str(env["home"] / ".config" / "herdr-engineering" / "control-plane.env") not in r.stdout + r.stderr
    assert "control-plane.env" not in r.stdout + r.stderr
    # dry-run does not even write the env file, and leaves no temp dir behind
    assert not (env["home"] / ".config").exists()
    assert "rendered the compose variables" in r.stdout
    # the transient 0700 dir the dry-run rendered into is gone (stub docker is a no-op)
    compose_calls = [ln for ln in env["log"].read_text().splitlines() if "--env-file" in ln]
    assert compose_calls and " config -q" in compose_calls[0]
    path = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("envfile ")][0].split()[1]
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))


def test_deploy_refuses_when_central_file_missing(env):
    env["central"].unlink()
    r = deploy(env, "--dry-run")
    assert r.returncode != 0
    assert "herdr-eng fleet config init" in r.stderr
    assert not env["log"].exists()  # never reached docker


def test_deploy_refuses_when_central_file_invalid(env):
    env["central"].write_text("version: 99\n")
    env["central"].chmod(0o600)
    r = deploy(env, "--dry-run")
    assert r.returncode != 0
    assert "herdr-eng fleet config init" in r.stderr or "fleet config validate" in r.stderr
    assert not env["log"].exists()


def test_deploy_default_central_path_is_under_home(env):
    r = run(DEPLOY, env, "--no-fetch", "--ref", "HEAD", "--dry-run")
    assert r.returncode != 0
    assert str(env["home"] / ".config/herdr-engineering/fleet/fleet-config.yaml") in r.stderr


def test_deploy_dry_run_prints_mount_and_no_secret_values(env):
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "/etc/herdr/fleet/fleet-config.yaml" in out and "read-only" in out
    assert str(env["central"]) in out
    assert env["secrets"], "fixture must carry secrets to look for"
    for secret in env["secrets"]:
        assert secret not in out


def test_deploy_mounts_the_resolved_directory_of_a_symlinked_central_file(env):
    link = env["tmp"] / "legacy-fleet-config.yaml"
    link.symlink_to(env["central"])
    env["env"]["FAIL_COMPOSE"] = "1"
    run(DEPLOY, env, "--no-fetch", "--ref", "HEAD", "--fleet-config", str(link))
    lines = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("composeenv ")]
    assert lines and f"fleetdir={env['central'].parent} " in lines[0]


def test_deploy_refuses_a_central_dir_holding_other_files(env):
    (env["central"].parent / "control-plane.env").write_text("X=1\n")
    r = deploy(env, "--dry-run")
    assert r.returncode != 0
    assert "must hold only fleet-config.yaml" in r.stderr and "control-plane.env" in r.stderr
    assert not env["log"].exists()


def test_deploy_hands_llm_proxy_only_the_llm_slice(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    deploy(env)
    log = env["log"].read_text()
    llmdir = env["home"] / ".config/herdr-engineering/llm-proxy"
    assert f"llmdir={llmdir}" in log
    assert sorted(os.listdir(llmdir)) == ["llm-config.json"]
    assert stat.S_IMODE(os.stat(llmdir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(llmdir / "llm-config.json").st_mode) == 0o600
    data = yaml.safe_load(env["central"].read_text())
    sliced = [ln for ln in log.splitlines() if ln.startswith("llmslice ")][0]
    assert data["sections"]["control_plane"]["values"]["pg_dsn"] not in sliced
    assert data["sections"]["ci"]["values"]["token"] not in sliced
    assert data["sections"]["llm"]["values"]["upstream_key"] in sliced


def test_trap_removes_env_file_on_failure(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    r = deploy(env)
    assert r.returncode != 0
    log = env["log"].read_text()
    lines = [ln for ln in log.splitlines() if ln.startswith("envfile ")]
    assert lines, log
    path = lines[0].split()[1]
    assert "mode=600" in lines[0] and "dirmode=700" in lines[0]
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))
    for secret in env["secrets"]:
        assert secret not in r.stdout + r.stderr
    # no receipt on failure, and none ever carries config values
    assert not (env["home"] / ".local/state/herdr-engineering/deploys").exists()


def test_compose_gets_central_file_mount_and_no_authored_env(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    deploy(env)
    log = env["log"].read_text()
    assert "HERDR_CP_ENV_FILE" not in log
    compose_calls = [ln for ln in log.splitlines() if ln.startswith("docker compose")]
    assert compose_calls and all("--env-file" in ln for ln in compose_calls)


# ---------------------------------------------------------------- compose files

def test_compose_web_has_no_env_file_and_mounts_central_file_read_only():
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())
    for name, svc in doc["services"].items():
        assert "env_file" not in svc, name
    web = doc["services"]["control-plane-web"]
    assert "--fleet-config" in web["command"]
    assert web["command"][web["command"].index("--fleet-config") + 1] == "/etc/herdr/fleet/fleet-config.yaml"
    # the DIRECTORY is mounted: a rename-replace of the file stays visible
    assert "${HERDR_CP_FLEET_DIR:?run deploy.sh}:/etc/herdr/fleet:ro" in web["volumes"]
    assert not any("fleet-config.yaml" in v for v in web["volumes"])
    app_env = web.get("environment") or {}
    assert not [k for k in app_env if k.startswith("HERDR_CP_")]
    proxy = doc["services"]["llm-proxy"]
    assert "FABRIC_LLM_UPSTREAM_KEY" not in (proxy.get("environment") or {})
    assert proxy["command"][proxy["command"].index("--fleet-config") + 1] == "/etc/herdr/llm/llm-config.json"
    # only the llm slice directory: never the central file's directory
    assert proxy["volumes"] == ["${HERDR_LLM_SLICE_DIR:?run deploy.sh}:/etc/herdr/llm:ro"]


def test_collab_overlay_reads_only_the_collab_slice():
    # Updated in the origin/main merge: main moved collaboration out of control-plane-web
    # into its own public container, so the overlay no longer overrides the dashboard's
    # command. collab-public gets its settings from the render-collab slice via
    # --fleet-config, never from the central file, the environment or inline ids.
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.collab.yaml").read_text())
    assert "control-plane-web" not in doc["services"]
    pub = doc["services"]["collab-public"]
    cmd = pub["command"]
    assert "--collab-only" in cmd
    assert cmd[cmd.index("--fleet-config") + 1] == "/etc/herdr/collab/collab-config.json"
    for flag in ("--collab-cognito-pool", "--collab-cognito-client", "--collab-cognito-group",
                 "--collab-cognito-web-client", "--collab-cognito-domain", "--collab-public-url"):
        assert flag not in cmd, flag
    assert "--collab-require-https" in cmd       # fixed: the public container never serves plain http
    assert "environment" not in pub and "env_file" not in pub
    vols = pub["volumes"]
    assert "${HERDR_COLLAB_CONFIG_DIR:?run deploy.sh}:/etc/herdr/collab:ro" in vols
    assert not any("/etc/herdr/fleet" in v or "HERDR_CP_FLEET" in v for v in vols)
    edge = doc["services"]["collab-edge"]
    assert "environment" not in edge and "env_file" not in edge


COLLAB = {"cognito_pool": "us-west-2_TESTPOOL", "cognito_clients": ["client-a"],
          "cognito_group": "team.test"}


def _with_collab(e, collab=COLLAB, cert_dir="/srv/acme"):
    data = yaml.safe_load(e["central"].read_text())
    cpv = data["sections"]["control_plane"]["values"]
    if collab is not None:
        cpv["collab"] = dict(collab)
    if cert_dir is not None:
        cpv["compose"]["HERDR_COLLAB_CERT_DIR"] = cert_dir
    e["central"].unlink()
    fcc.write_private_file(e["central"], yaml.safe_dump(data, sort_keys=False))
    marker = e["home"] / ".config" / "herdr-engineering" / "collab-enabled"
    marker.parent.mkdir(parents=True)
    marker.touch()


def _home_files(e):
    return sorted(str(p.relative_to(e["home"])) for p in e["home"].rglob("*"))


def test_collab_dry_run_renders_the_slice_and_touches_nothing(env):
    _with_collab(env)
    before = _home_files(env)
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "collaboration network: enabled" in r.stdout
    assert "collab-public collab-edge" in r.stdout
    assert _home_files(env) == before          # no edge conf, data dir, slice or timer installed
    calls = [ln for ln in env["log"].read_text().splitlines() if " config -q" in ln]
    assert calls and "compose.collab.yaml" in calls[0]
    for secret in env["secrets"] - {"control_plane"}:   # a key name, not a value
        assert secret not in r.stdout + r.stderr


def test_collab_enabled_without_collab_settings_fails_clearly(env):
    _with_collab(env, collab=None)
    r = deploy(env, "--dry-run")
    assert r.returncode != 0 and "control_plane.collab" in r.stderr


def test_collab_enabled_without_cert_dir_fails_clearly(env):
    _with_collab(env, cert_dir=None)
    r = deploy(env, "--dry-run")
    assert r.returncode != 0 and "HERDR_COLLAB_CERT_DIR" in r.stderr


def test_collab_sync_reads_pool_and_group_from_the_slice_not_env(env):
    _with_collab(env)
    out = env["tmp"] / "slice.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), out)
    bindir = env["tmp"] / "bin"
    for name in ("curl", "aws"):
        (bindir / name).write_text(f"#!/usr/bin/env bash\necho {name} \"$*\" >> {env['tmp']}/sync.log\nexit 1\n")
        (bindir / name).chmod(0o755)
    env["env"]["HERDR_COLLAB_POOL"] = "us-west-2_FROMENV"
    sync = ROOT / "deploy" / "control-plane" / "collab-sync.sh"
    r = run(sync, env, "once", "--config", str(out), "--dir", str(env["tmp"] / "sync"))
    assert r.returncode != 0                     # the stub curl fails
    log = (env["tmp"] / "sync.log").read_text()
    assert "us-west-2_TESTPOOL" in log and "FROMENV" not in log
    r = run(sync, env, "once", "--config", str(env["tmp"] / "missing.json"))
    assert r.returncode != 0 and "control_plane.collab" in r.stderr


def test_generate_env_script_is_gone():
    assert not (ROOT / "deploy/control-plane/generate-env.sh").exists()


# ------------------------------------------------------------ deploy-fleet-agents

def test_fleet_agents_skip_host_without_host_json(env):
    hosts = env["tmp"] / "hosts.txt"
    hosts.write_text("alpha|alpha.example.test|systemd\n")
    bindir = env["tmp"] / "bin"
    ssh = bindir / "ssh"
    ssh.write_text(f"""#!/usr/bin/env bash
echo "ssh $*" >> {env['tmp']}/ssh.log
case "$*" in *"test -s"*) exit 1 ;; esac
exit 0
""")
    ssh.chmod(0o755)
    for name in ("scp", "pip", "pip3"):  # nothing may reach a real host
        (bindir / name).write_text(f"#!/usr/bin/env bash\necho {name} \"$*\" >> {env['tmp']}/ssh.log\nexit 0\n")
        (bindir / name).chmod(0o755)
    env["env"]["HERDR_FLEET_HOSTS"] = str(hosts)
    r = run(AGENTS, env, "--no-fetch", "--ref", "HEAD")
    assert "herdr-eng fleet join alpha" in r.stdout + r.stderr
    calls = (env["tmp"] / "ssh.log").read_text()
    assert "test -s" in calls and "host.json" in calls
    assert "mkdir" not in calls and "ln -sfn" not in calls  # nothing rolled to that host
    assert r.returncode != 0  # a skipped host is a failed roll


def test_dry_run_fails_when_compose_map_is_missing(env):
    data = yaml.safe_load(env["central"].read_text())
    del data["sections"]["control_plane"]["values"]["compose"]
    env["central"].unlink()
    fcc.write_private_file(env["central"], yaml.safe_dump(data, sort_keys=False))
    r = deploy(env, "--dry-run")
    assert r.returncode != 0 and "invalid" in r.stderr
    for secret in env["secrets"]:
        assert secret not in r.stdout + r.stderr
