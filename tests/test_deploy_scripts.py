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
    "llm_upstream_key": "llm-secret-key-0001", "ci_url": "https://ci.example.test",
    "ci_token": "ci-secret-token-0002", "web_bind": "127.0.0.1", "web_port": "8080",
    "github_token": "gh-secret-token-0003", "hosts": {"bender": ["builder"]},
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
    assert "rendering" not in r.stdout or "dry-run" in r.stdout
    leftovers = [p for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("herdr-cp-env.*")]
    assert not leftovers


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
    assert str(env["home"] / ".config/herdr-engineering/fleet-config.yaml") in r.stderr


def test_deploy_dry_run_prints_mount_and_no_secret_values(env):
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "/etc/herdr/fleet-config.yaml" in out and "read-only" in out
    assert str(env["central"]) in out
    assert env["secrets"], "fixture must carry secrets to look for"
    for secret in env["secrets"]:
        assert secret not in out


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
    assert web["command"][web["command"].index("--fleet-config") + 1] == "/etc/herdr/fleet-config.yaml"
    assert any(v.endswith(":/etc/herdr/fleet-config.yaml:ro") for v in web["volumes"])
    app_env = web.get("environment") or {}
    assert not [k for k in app_env if k.startswith("HERDR_CP_")]
    proxy = doc["services"]["llm-proxy"]
    assert "FABRIC_LLM_UPSTREAM_KEY" not in (proxy.get("environment") or {})
    assert "--fleet-config" in proxy["command"]


def test_collab_overlay_keeps_the_fleet_config_flag():
    # a compose override replaces the whole command list
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.collab.yaml").read_text())
    cmd = doc["services"]["control-plane-web"]["command"]
    assert cmd[cmd.index("--fleet-config") + 1] == "/etc/herdr/fleet-config.yaml"


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
