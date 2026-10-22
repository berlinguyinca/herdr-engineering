"""deploy.sh / deploy-fleet-agents.sh, exercised by subprocess with a stub `docker`
on PATH (nothing real is ever built, started or contacted)."""
import json
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
    "ci_token": "ci-secret-token-0002", "web_bind": "127.0.0.1", "web_port": "8080", "gateway_tailnet_ip": "127.0.0.1",
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
    running = tmp_path / "running-tag"
    # Records each call. `inspect` answers $STUB_RUNNING_IMAGE. On `compose ... up` it
    # snapshots the --env-file (mode, dir mode), the -f files and the image tag, fails
    # when FAIL_COMPOSE is set or the tag is in FAIL_COMPOSE_TAGS, and otherwise records
    # the tag as "running" (what the stub curl's health answer reports).
    stub.write_text(f"""#!/usr/bin/env bash
echo "docker $*" >> {log}
if [ "$1" = inspect ]; then
  case "$*" in
    *.State*)   # container state: crash-looping when "<running tag>:<container>" is in STUB_CRASH
      name="${{@: -1}}"; tag="$(cat {running} 2>/dev/null)"
      [ -n "$tag" ] || {{ echo "missing false 0 none"; exit 0; }}
      case " ${{STUB_CRASH:-}} " in
        *" $tag:$name "*) echo "restarting true $(date +%s%N) none" ;;
        *) echo "running false 0 none" ;;
      esac
      exit 0 ;;
  esac
  [ -n "${{STUB_RUNNING_IMAGE:-}}" ] || exit 1
  echo "$STUB_RUNNING_IMAGE"; exit 0
fi
if [ "$1" = build ] && [ -n "${{FAIL_BUILD:-}}" ]; then exit 1; fi
if [ "$1" = compose ]; then
  echo "composeenv fleetdir=${{HERDR_CP_FLEET_DIR:-}} tag=${{HERDR_CP_IMAGE_TAG:-}} collabdir=${{HERDR_COLLAB_CONFIG_DIR:-}} llmdir=${{HERDR_LLM_SLICE_DIR:-}}" >> {log}
  if [ -n "${{HERDR_LLM_SLICE_DIR:-}}" ]; then echo "llmslice $(cat "$HERDR_LLM_SLICE_DIR/llm-config.json" | tr -d '\n')" >> {log}; fi
  prev=""; files=""; up=0
  for a in "$@"; do
    if [ "$prev" = --env-file ]; then
      echo "envfile $a mode=$(stat -c %a "$a") dirmode=$(stat -c %a "$(dirname "$a")")" >> {log}
    fi
    if [ "$prev" = -f ]; then files="$files $a"; fi
    [ "$a" = up ] && up=1
    prev="$a"
  done
  echo "composefiles tag=${{HERDR_CP_IMAGE_TAG:-}}$files" >> {log}
  if [ "$up" = 1 ]; then
    [ -n "${{FAIL_COMPOSE:-}}" ] && exit 1
    case " ${{FAIL_COMPOSE_TAGS:-}} " in *" ${{HERDR_CP_IMAGE_TAG:-}} "*) exit 1 ;; esac
    echo "${{HERDR_CP_IMAGE_TAG:-}}" > {running}
  fi
fi
exit 0
""")
    stub.chmod(0o755)
    # curl: the health URL reports the running tag (unless listed in STUB_UNHEALTHY_TAGS);
    # anything else (collab-sync's JWKS fetch) gets a key set. Nothing leaves the machine.
    curl = bindir / "curl"
    curl.write_text(f"""#!/usr/bin/env bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o|-m) [ "$1" = -o ] && out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
echo "curl $url" >> {log}
case "$url" in
  *api/v1/health*)
    tag="$(cat {running} 2>/dev/null)"
    [ -n "$tag" ] || exit 7
    case " ${{STUB_UNHEALTHY_TAGS:-}} " in *" $tag "*) exit 22 ;; esac
    echo '{{"status":"ok","commit":"'"$tag"'"}}' ;;
  *) echo '{{"keys":[{{"kid":"k"}}]}}' > "${{out:-/dev/stdout}}" ;;
esac
""")
    curl.chmod(0o755)
    for name, body in (("aws", 'echo \'["user-a"]\''), ("systemctl", "exit 0")):
        (bindir / name).write_text(f"#!/usr/bin/env bash\necho {name} \"$*\" >> {log}\n{body}\n")
        (bindir / name).chmod(0o755)
    secrets = {s for s in _strings(yaml.safe_load(central.read_text()))
               if len(s) >= 12 and "://" not in s and s not in ("control-plane",)}
    e = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    for k in list(e):
        if k.startswith(("HERDR_CP_", "FABRIC_")):
            del e[k]
    e.update(HERDR_CP_HEALTH_URL="http://stub.invalid/api/v1/health", HERDR_CP_HEALTH_TRIES="2",
             HERDR_CP_HEALTH_INTERVAL="0")
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
    r = deploy(env)
    assert r.returncode == 0, r.stderr
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


def test_compose_runs_every_app_service_on_the_deploy_tag():
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())
    for name in ("control-plane-web", "control-plane-worker", "llm-proxy"):
        assert doc["services"][name]["image"] == "herdr-engineering:${HERDR_CP_IMAGE_TAG:?run deploy.sh}", name
    collab = yaml.safe_load((ROOT / "deploy/control-plane/compose.collab.yaml").read_text())
    assert collab["services"]["collab-public"]["image"] == "herdr-engineering:${HERDR_CP_IMAGE_TAG:?run deploy.sh}"


def test_deploy_recreates_web_worker_and_proxy_on_one_tag(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    deploy(env)
    log = env["log"].read_text().splitlines()
    up = [ln for ln in log if ln.startswith("docker compose") and " up " in ln]
    assert up and up[0].endswith("up -d --no-deps --no-build control-plane-web control-plane-worker llm-proxy")
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True,
                         text=True).stdout.strip()
    assert any(ln.startswith("composeenv ") and f" tag={sha} " in ln for ln in log)
    assert any(ln.startswith("docker build") and f"herdr-engineering:{sha}" in ln for ln in log)


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
    # collab-public gets its settings from the render-collab slice via --fleet-config, never
    # from the central file, the environment or inline ids. Second origin/main merge (#111):
    # the overlay overrides the dashboard's command again, to add --collab-operator-url;
    # compose replaces the whole list, so it must be the base command plus only that pair.
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.collab.yaml").read_text())
    base = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())
    web = doc["services"]["control-plane-web"]
    assert set(web) == {"command"}
    wcmd = web["command"]
    i = wcmd.index("--collab-operator-url")
    assert wcmd[:i] + wcmd[i + 2:] == base["services"]["control-plane-web"]["command"]
    assert not any(a.startswith("--collab-") for a in wcmd[:i] + wcmd[i + 2:])
    pub = doc["services"]["collab-public"]
    cmd = pub["command"]
    assert "--collab-only" in cmd
    assert cmd[cmd.index("--fleet-config") + 1] == "/etc/herdr/collab/collab-config.json"
    for flag in ("--collab-cognito-pool", "--collab-cognito-client", "--collab-cognito-group",
                 "--collab-cognito-web-client", "--collab-cognito-domain", "--collab-public-url",
                 "--collab-operator-user", "--collab-operator-email"):
        assert flag not in cmd, flag
    assert not any("HERDR_COLLAB_OPERATOR_USER" in a or "HERDR_COLLAB_OPERATOR_EMAIL" in a
                   for a in cmd)
    assert "--collab-require-https" in cmd       # fixed: the public container never serves plain http
    assert "environment" not in pub and "env_file" not in pub
    vols = pub["volumes"]
    assert "${HERDR_COLLAB_CONFIG_DIR:?run deploy.sh}:/etc/herdr/collab:ro" in vols
    assert not any("/etc/herdr/fleet" in v or "HERDR_CP_FLEET" in v for v in vols)
    edge = doc["services"]["collab-edge"]
    assert "environment" not in edge and "env_file" not in edge


COLLAB = {"cognito_pool": "us-west-2_TESTPOOL", "cognito_clients": ["client-a"],
          "cognito_group": "team.test", "operator_user": "owner-test"}


_DEFAULT = object()


def _with_collab(e, collab=COLLAB, cert_dir=_DEFAULT):
    if cert_dir is _DEFAULT:
        cert_dir = str(e["tmp"] / "edge-certs")
    data = yaml.safe_load(e["central"].read_text())
    cpv = data["sections"]["control_plane"]["values"]
    if collab is not None:
        cpv["collab"] = dict(collab)
    if cert_dir is not None:
        cpv["compose"]["HERDR_COLLAB_CERT_DIR"] = cert_dir
    # collab-sync.sh's default certbot layout under the fake HOME (#115): a lineage to copy
    # and a certbot to put in the renew unit. Nothing runs certbot.
    acme = e["home"] / ".local/share/inferweave/bender-failover/acme"
    live = acme / "config/live/localdev.mcp.metabolomics.us"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_text("not a real cert\n")
    (live / "privkey.pem").write_text("not a real key\n")
    certbot = acme.parent / "acme-venv/bin/certbot"
    certbot.parent.mkdir(parents=True)
    certbot.write_text("#!/bin/sh\nexit 0\n")
    certbot.chmod(0o755)
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


# ------------------------------------------------------------- C2: honest rollback

def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def _head():
    return _git("rev-parse", "--short=12", "HEAD")


def _seed_previous(e, commit, extra=None):
    """Pretend `commit` is what runs now: its image is up and it has a receipt."""
    full = _git("rev-parse", commit)
    tag = full[:12]
    state = e["home"] / ".local/state/herdr-engineering/deploys"
    state.mkdir(parents=True, exist_ok=True)
    receipt = {"ref": "x", "commit": full, "image": f"herdr-engineering:{tag}", **(extra or {})}
    (state / f"{tag}.json").write_text(json.dumps(receipt))
    e["env"]["STUB_RUNNING_IMAGE"] = f"herdr-engineering:{tag}"
    return full, tag


def _layout_commit():
    """Oldest commit whose compose.yaml has the current deploy-layout marker: a valid
    automatic-rollback target that differs from HEAD (the commit being deployed)."""
    first = _git("log", "--format=%H", "-S", "x-herdr-deploy-layout: 2", "--",
                 "deploy/control-plane/compose.yaml").splitlines()[-1]
    if first == _git("rev-parse", "HEAD"):
        pytest.skip("HEAD introduced the layout marker; no older layout-2 commit to roll back to")
    return first


def _worktrees():
    return _git("worktree", "list", "--porcelain")


def test_forward_compose_failure_rolls_back_with_the_previous_commits_files(env):
    prev_full, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["FAIL_COMPOSE_TAGS"] = _head()
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    log = env["log"].read_text().splitlines()
    files = [ln for ln in log if ln.startswith("composefiles ")]
    assert files[0].startswith(f"composefiles tag={_head()} ")
    rb = [ln for ln in files if ln.startswith(f"composefiles tag={prev_tag} ")]
    assert rb and "rollback-worktree/deploy/control-plane/compose.yaml" in rb[0]
    assert f"rolled back to herdr-engineering:{prev_tag} (commit {prev_tag}) and healthy" in r.stderr
    assert "FAILED" in r.stderr                    # the deploy itself is reported failed
    assert not (env["home"] / f".local/state/herdr-engineering/deploys/{_head()}.json").exists()
    assert "rollback-worktree" not in _worktrees()  # removed by the trap


def test_unhealthy_new_commit_rolls_back_and_reports_healthy(env):
    _, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    r = deploy(env)
    assert r.returncode == 1
    assert "and healthy" in r.stderr and "FAILED TO ROLL BACK" not in r.stderr


def test_failed_rollback_is_non_zero_and_honest(env):
    prev_full, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["FAIL_COMPOSE"] = "1"                # forward AND rollback compose fail
    r = deploy(env)
    assert r.returncode == 3
    assert "FAILED TO ROLL BACK" in r.stderr and "and healthy" not in r.stderr
    assert f"--ref {prev_full}" in r.stderr         # the manual command
    assert "rollback-worktree" not in _worktrees()
    for secret in env["secrets"]:
        assert secret not in r.stdout + r.stderr


def test_unhealthy_rollback_is_non_zero_and_honest(env):
    _, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["STUB_UNHEALTHY_TAGS"] = f"{_head()} {prev_tag}"
    r = deploy(env)
    assert r.returncode == 3 and "FAILED TO ROLL BACK" in r.stderr


def test_first_deploy_failure_has_no_rollback_and_says_so(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    r = deploy(env)
    assert r.returncode == 1
    assert "no automatic rollback for this deploy: nothing was running (first deploy)" in r.stdout
    assert "NOT rolled back" in r.stderr and "rolled back to" not in r.stderr
    assert len([ln for ln in env["log"].read_text().splitlines() if ln.startswith("composefiles ")]) == 1


def test_pre_feature_running_commit_gets_the_manual_recipe(env):
    old_full, _ = _seed_previous(env, "b4e6df6")
    env["env"]["FAIL_COMPOSE"] = "1"
    r = deploy(env)
    assert r.returncode == 1
    assert "predates the current deploy layout" in r.stdout + r.stderr
    assert "control-plane.env" in r.stderr and f"--ref {old_full}" in r.stderr
    assert len([ln for ln in env["log"].read_text().splitlines() if ln.startswith("composefiles ")]) == 1


def test_success_writes_a_receipt_with_the_previous_commit_and_no_values(env):
    prev_full, prev_tag = _seed_previous(env, _layout_commit())
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    receipt_path = env["home"] / f".local/state/herdr-engineering/deploys/{_head()}.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["commit"].startswith(_head())
    assert receipt["previous_commit"] == prev_full
    assert receipt["previous_image"] == f"herdr-engineering:{prev_tag}"
    assert receipt["collab_slice_sha256"] == ""
    for secret in env["secrets"]:
        assert secret not in receipt_path.read_text()


def _collab_ups(e):
    return [ln for ln in e["log"].read_text().splitlines()
            if ln.startswith("docker compose") and "--force-recreate collab-public" in ln]


def test_changed_collab_slice_force_recreates_collab_public(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit(), {"collab_slice_sha256": "0" * 64})
    collab = env["home"] / ".config/herdr-engineering/collab"
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    assert len(_collab_ups(env)) == 1
    receipt = json.loads((env["home"] / f".local/state/herdr-engineering/deploys/{_head()}.json").read_text())
    import hashlib
    assert receipt["collab_slice_sha256"] == hashlib.sha256(
        (collab / "collab-config.json").read_bytes()).hexdigest()
    # same slice as the receipt of what runs now: no forced recreate
    env["log"].write_text("")
    env["env"]["STUB_RUNNING_IMAGE"] = f"herdr-engineering:{_head()}"
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    assert _collab_ups(env) == []


def test_collab_deploy_hands_the_cert_dir_from_the_central_config_to_collab_sync(env):
    # #115 (second origin/main merge): collab-sync copies the edge cert into the directory the
    # edge mounts. That is control_plane.compose.HERDR_COLLAB_CERT_DIR, passed as a flag and
    # written into the timer unit (the timer has no environment), never HERDR_COLLAB_CERT_DIR env.
    _with_collab(env)
    env["env"]["HERDR_COLLAB_CERT_DIR"] = str(env["tmp"] / "from-env")
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    certs = env["tmp"] / "edge-certs"
    assert (certs / "fullchain.pem").read_text() == "not a real cert\n"
    assert oct((certs / "privkey.pem").stat().st_mode & 0o777) == "0o600"
    assert not (env["tmp"] / "from-env").exists()
    units = env["home"] / ".config/systemd/user"
    assert f'--cert-dir "{certs}"' in (units / "herdr-collab-sync.service").read_text()   # quoted paths
    assert 'certbot" renew' in (units / "herdr-collab-cert-renew.service").read_text()
    # edge nginx.conf written in place at a stable path (#110)
    edge = env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf"
    assert edge.read_text() == (ROOT / "deploy/control-plane/collab-edge/nginx.conf").read_text()


def test_edge_conf_is_rewritten_in_place_and_the_edge_reloaded(env):
    _with_collab(env)
    edge = env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf"
    edge.parent.mkdir(parents=True)
    edge.write_text("old\n")
    inode = edge.stat().st_ino
    env["env"]["STUB_RUNNING_IMAGE"] = "herdr-engineering:000000000000"   # edge "exists"
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    assert edge.stat().st_ino == inode
    assert edge.read_text() != "old\n"
    assert "docker exec herdr-collab-edge nginx -s reload" in env["log"].read_text()


def test_collab_slice_dir_owner_mismatch_warns(env):
    _with_collab(env)
    data = yaml.safe_load(env["central"].read_text())
    data["sections"]["control_plane"]["values"]["compose"]["HERDR_COLLAB_CERT_UID"] = "424242"
    env["central"].unlink()
    fcc.write_private_file(env["central"], yaml.safe_dump(data, sort_keys=False))
    r = deploy(env)
    assert "WARNING" in r.stderr and "424242" in r.stderr


def test_link_remote_refuses_a_url_as_first_argument(env):
    script = ROOT / "deploy" / "herdr-cp-feed-plugin" / "link-remote.sh"
    for arg in ("http://cp.example.test", "https://cp.example.test", "http"):
        r = run(script, env, arg)
        assert r.returncode == 2, r.stderr
        assert "not a URL" in r.stderr
    assert not (ROOT / "http:").exists() and not list(env["home"].rglob("*cp.example.test*"))


def test_recreated_services_have_no_relative_bind_mounts():
    """deploy.sh runs compose from a temporary worktree: a relative bind source would
    dangle (and be re-created empty by docker) after the worktree is removed."""
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())
    collab = yaml.safe_load((ROOT / "deploy/control-plane/compose.collab.yaml").read_text())
    services = {n: doc["services"][n] for n in ("control-plane-web", "control-plane-worker", "llm-proxy")}
    services.update(collab["services"])
    for name, svc in services.items():
        for vol in svc.get("volumes") or []:
            src = vol if isinstance(vol, str) else vol.get("source", "")
            assert not str(src).startswith("."), (name, src)
    assert doc["x-herdr-deploy-layout"] == 2
    worker = doc["services"]["control-plane-worker"]["command"]
    assert worker[worker.index("--migrations") + 1] == "/app/control-plane/migrations"
    dockerfile = (ROOT / "deploy/fabric-stack/Dockerfile").read_text()
    assert "COPY deploy/control-plane/migrations/ ./control-plane/migrations/" in dockerfile
    assert "COPY deploy/control-plane/config.yaml ./control-plane/config.yaml" in dockerfile



# ------------------------- re-review I-B: every recreated service must be up

def test_crash_looping_worker_fails_the_deploy_into_rollback(env):
    _, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["STUB_CRASH"] = f"{_head()}:herdr-cp-worker"
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    assert "not settled: control-plane-worker(restarting)" in r.stderr
    assert "and healthy" in r.stderr          # the old commit's worker is fine
    assert not (env["home"] / f".local/state/herdr-engineering/deploys/{_head()}.json").exists()


def test_first_deploy_with_a_crash_looping_llm_proxy_is_not_ok(env):
    env["env"]["STUB_CRASH"] = f"{_head()}:herdr-llm-proxy"
    r = deploy(env)
    assert r.returncode == 1 and "ok:" not in r.stdout
    assert "llm-proxy(restarting)" in r.stderr and "NOT rolled back" in r.stderr


def test_rollback_is_not_healthy_on_the_web_alone(env):
    _with_collab(env)
    _, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    env["env"]["STUB_CRASH"] = f"{prev_tag}:herdr-collab-public"   # old collab-public refuses its slice
    r = deploy(env)
    assert r.returncode == 3
    assert "FAILED TO ROLL BACK" in r.stderr and "and healthy" not in r.stderr
    assert "collab-public(restarting)" in r.stderr


def test_rollback_renders_the_compose_env_with_the_previous_commits_code(env):
    _, prev_tag = _seed_previous(env, _layout_commit())
    env["env"]["FAIL_COMPOSE_TAGS"] = _head()
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    envfiles = [ln.split()[1] for ln in env["log"].read_text().splitlines() if ln.startswith("envfile ")]
    assert envfiles[0].endswith("/compose.env") and envfiles[-1].endswith("/rollback-compose.env")


def test_receipt_is_valid_json_for_a_ref_with_a_quote(env):
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    receipt = json.loads((env["home"] / f".local/state/herdr-engineering/deploys/{_head()}.json").read_text())
    assert receipt["ref"] == "HEAD"


def test_llm_slice_dir_holding_other_files_is_refused(env):
    d = env["home"] / ".config/herdr-engineering/llm-proxy"
    d.mkdir(parents=True)
    (d / "stray.txt").write_text("x")
    r = deploy(env)
    assert r.returncode != 0 and "must hold only llm-config.json" in r.stderr
    assert "docker build" not in env["log"].read_text() if env["log"].exists() else True


# ------------------------------ re-review I-C: no host change before the build

def _seed_host_state(env):
    edge = env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf"
    edge.parent.mkdir(parents=True)
    edge.write_text("previous nginx conf\n")
    llm = env["home"] / ".config/herdr-engineering/llm-proxy/llm-config.json"
    llm.parent.mkdir(parents=True, mode=0o700)
    llm.write_text('{"previous": "llm slice"}\n')
    llm.chmod(0o600)
    units = env["home"] / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "herdr-collab-sync.service").write_text("previous unit\n")
    return edge, llm, units


def test_failed_build_changes_nothing_on_the_host(env):
    _with_collab(env)
    edge, llm, units = _seed_host_state(env)
    before = {p: p.read_text() for p in (edge, llm, units / "herdr-collab-sync.service")}
    env["env"]["FAIL_BUILD"] = "1"
    r = deploy(env)
    assert r.returncode != 0
    for p, text in before.items():
        assert p.read_text() == text, p
    log = env["log"].read_text()
    assert "nginx -s reload" not in log and "systemctl" not in log
    assert not (env["home"] / ".config/herdr-engineering/collab/collab-config.json").exists()


def test_failed_deploy_restores_nginx_conf_slices_and_sync_units(env):
    _with_collab(env)
    edge, llm, units = _seed_host_state(env)
    inode = edge.stat().st_ino
    env["env"]["FAIL_COMPOSE"] = "1"
    env["env"]["STUB_RUNNING_IMAGE"] = "herdr-engineering:000000000000"   # edge "exists"
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    assert edge.read_text() == "previous nginx conf\n" and edge.stat().st_ino == inode
    assert llm.read_text() == '{"previous": "llm slice"}\n'
    assert (units / "herdr-collab-sync.service").read_text() == "previous unit\n"
    assert not (units / "herdr-collab-sync.timer").exists()          # did not exist before
    assert not (env["home"] / ".config/herdr-engineering/collab/collab-config.json").exists()
    log = env["log"].read_text().splitlines()
    build = next(i for i, ln in enumerate(log) if ln.startswith("docker build"))
    first_sync = next(i for i, ln in enumerate(log) if ln.startswith("systemctl"))
    assert build < first_sync                  # collab-sync install only after the build
    assert sum("nginx -s reload" in ln for ln in log) >= 2   # new conf, then the restored one


def test_collab_sync_install_failure_triggers_rollback(env):
    _with_collab(env)
    edge, llm, units = _seed_host_state(env)
    _, prev_tag = _seed_previous(env, _layout_commit())
    (env["tmp"] / "bin" / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
    r = deploy(env)
    assert r.returncode in (1, 3), r.stderr
    assert "deploy of" in r.stderr and "failed" in r.stderr
    assert edge.read_text() == "previous nginx conf\n"
    ups = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("composefiles ")]
    assert not any(f"tag={_head()} " in ln for ln in ups)     # forward compose never ran
