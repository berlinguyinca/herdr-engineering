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


@pytest.fixture(scope="module")
def candidate_ref():
    """Pin HEAD once: deploy.sh must render the real committed candidate's code.

    The script itself is exercised from this working tree so its uncommitted
    hardening is tested. Never substitute caller imports for target-ref rendering.
    """
    ref = os.environ.get("HERDR_DEPLOY_TEST_REF", "HEAD")
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}")


@pytest.fixture
def env(tmp_path, candidate_ref, monkeypatch):
    monkeypatch.setitem(globals(), "_head", lambda: candidate_ref[:12])
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
    health_reads = tmp_path / "health-reads"
    running_mount = tmp_path / "running-mount"
    running_images = tmp_path / "running-images.json"
    running_config = tmp_path / "running-config-image"
    # Containers named in `missing` do not exist (inspect says "No such object") until a
    # compose up names their service; STUB_FAIL_REMOVES adds names when a forward up fails
    # (an interrupted recreate). STUB_REMOVAL_IN_PROGRESS=N makes the first N ups fail with
    # Docker's "removal ... already in progress", collab-public reporting `removing` once.
    missing = tmp_path / "missing"
    removal_left = tmp_path / "removal-left"
    removing = tmp_path / "removing"
    # Records each call. `inspect` answers $STUB_RUNNING_IMAGE. On `compose ... up` it
    # snapshots the --env-file (mode, dir mode), the -f files and the image tag, fails
    # when FAIL_COMPOSE is set or the tag is in FAIL_COMPOSE_TAGS, and otherwise records
    # the tag as "running" (what the stub curl's health answer reports).
    stub.write_text(f"""#!/usr/bin/env bash
echo "docker $*" >> {log}
# deploy-state.py asks for `inspect --type container NAME`: answer it like `inspect NAME`
if [ "$1 ${{2:-}} ${{3:-}}" = "inspect --type container" ] && [ "$#" = 4 ]; then set -- inspect "$4"; fi
is_missing() {{ case " $(cat {missing} 2>/dev/null) " in *" $1 "*) return 0 ;; esac; return 1; }}
if [ "$1" = inspect ] && [ "${{@: -1}}" = "${{STUB_INSPECT_ERROR:-}}" ]; then
  echo "permission denied while trying to connect to the docker daemon" >&2; exit 1
fi
if [ "$1" = inspect ] && is_missing "${{@: -1}}"; then
  echo "Error: No such object: ${{@: -1}}" >&2; exit 1
fi
if [ "$1 $2" = 'image inspect' ]; then
  python3 - "$3" <<'PY'
import json, os, sys
old_id = 'sha256:' + 'a' * 64
old_tag = 'herdr-engineering:' + os.environ.get('STUB_PREVIOUS_TAG', '')
previous = sys.argv[1] in (old_id, old_tag)
identity = old_id if previous else 'sha256:' + 'b' * 64
if sys.argv[1] == old_tag:
    identity = os.environ.get('STUB_PREVIOUS_TAG_ID', identity)
commit = os.environ.get('STUB_PREVIOUS_COMMIT', '') if previous else {candidate_ref!r}
env = json.loads(os.environ['STUB_IMAGE_ENV']) if previous and 'STUB_IMAGE_ENV' in os.environ else ['HERDR_ENGINEERING_COMMIT=' + commit]
print(json.dumps([{{'Id': identity, 'Config': {{'Env': env}}}}]))
PY
  exit 0
fi
if [ "$1" = inspect ]; then
  if [ "$#" = 2 ]; then
    [ -n "${{STUB_RUNNING_IMAGE:-}}" ] || [ -s {running} ] || exit 1
    python3 - "$2" <<'PY'
import json, os, pathlib, sys
mount = pathlib.Path({str(running_mount)!r})
images = pathlib.Path({str(running_images)!r})
ids = json.loads(images.read_text()) if images.exists() else {{}}
config = pathlib.Path({str(running_config)!r})
print(json.dumps([{{"Image": ids.get(sys.argv[1], "sha256:" + "a" * 64),
    "Config": {{"Image": config.read_text() if config.exists() else os.environ.get("STUB_RUNNING_IMAGE", "herdr-engineering:test")}},
    "Mounts": [{{"Destination": "/etc/herdr/fleet", "Source": mount.read_text() if mount.exists() else os.environ["STUB_FLEET_DIR"]}}]}}]))
PY
    exit 0
  fi
  case "$*" in
    *.State*)   # container state: crash-looping when "<running tag>:<container>" is in STUB_CRASH
      name="${{@: -1}}"; tag="$(cat {running} 2>/dev/null)"
      left="$(cat {removing} 2>/dev/null || echo 0)"
      if [ "$name" = herdr-collab-public ] && [ "$left" -gt 0 ]; then
        echo $((left - 1)) > {removing}; echo "removing false 0 none"; exit 0
      fi
      [ -n "$tag" ] || {{ echo "missing false 0 none"; exit 0; }}
      case " ${{STUB_CRASH:-}} " in
        *" $tag:$name "*) echo "restarting true $(date +%s%N) none" ;;
        *) echo "running false 0 none" ;;
      esac
      exit 0 ;;
  esac
  if [ -f {running_config} ]; then cat {running_config}; exit 0; fi
  [ -n "${{STUB_RUNNING_IMAGE:-}}" ] || exit 1
  echo "$STUB_RUNNING_IMAGE"; exit 0
fi
if [ "$1" = build ] && [ -n "${{FAIL_BUILD:-}}" ]; then exit 1; fi
if [ "$1" = compose ]; then
  echo "composeenv fleetdir=${{HERDR_CP_FLEET_DIR:-}} tag=${{HERDR_CP_IMAGE_TAG:-}} collabdir=${{HERDR_COLLAB_CONFIG_DIR:-}} llmdir=${{HERDR_LLM_SLICE_DIR:-}} githubappdir=${{HERDR_CP_GITHUB_APP_DIR:-}}" >> {log}
  if [ -n "${{HERDR_LLM_SLICE_DIR:-}}" ]; then echo "llmslice $(cat "$HERDR_LLM_SLICE_DIR/llm-config.json" | tr -d '\n')" >> {log}; fi
  prev=""; files=""; up=0; pins=""
  for a in "$@"; do
    if [ "$prev" = --env-file ]; then
      echo "envfile $a mode=$(stat -c %a "$a") dirmode=$(stat -c %a "$(dirname "$a")")" >> {log}
      echo "webport tag=${{HERDR_CP_IMAGE_TAG:-}} $(grep '^HERDR_CP_WEB_PUBLIC_PORT=' "$a")" >> {log}
    fi
    if [ "$prev" = -f ]; then
      files="$files $a"
      case "$a" in *rollback-images.json) pins="$a"; echo "pinned $(cat "$a")" >> {log} ;; esac
    fi
    [ "$a" = up ] && up=1
    prev="$a"
  done
  echo "composefiles tag=${{HERDR_CP_IMAGE_TAG:-}}$files" >> {log}
  lock="$HOME/.local/share/herdr-engineering/collab-sync.lock"
  if [ "$up" = 1 ] && [ -e "$lock" ]; then
    if flock -n "$lock" true; then echo "collablock free" >> {log}; else echo "collablock held" >> {log}; fi
  fi
  if [ "$up" = 1 ] && [ -n "${{STUB_REMOVAL_IN_PROGRESS:-}}" ]; then
    [ -e {removal_left} ] || echo "$STUB_REMOVAL_IN_PROGRESS" > {removal_left}
    n="$(cat {removal_left})"
    if [ "$n" -gt 0 ]; then
      echo $((n - 1)) > {removal_left}; echo 1 > {removing}
      echo "removalinprogress" >> {log}
      echo "Error response from daemon: removal of container herdr-collab-public is already in progress" >&2
      exit 1
    fi
  fi
  if [ "$up" = 1 ] && [ -n "${{STUB_FAIL_REMOVES:-}}" ]; then
    case " ${{FAIL_COMPOSE_TAGS:-}} " in *" ${{HERDR_CP_IMAGE_TAG:-}} "*)
      echo "${{STUB_FAIL_REMOVES}} $(cat {missing} 2>/dev/null)" > {missing} ;; esac
  fi
  if [ "$up" = 1 ]; then
    python3 - <<'PY' >> {log}
import json, pathlib, os
state = pathlib.Path(os.environ['HOME']) / '.local/state/herdr-engineering/deploys'
attempts = list(state.glob('attempt-*/metadata.json'))
print('prepared_before_recreate', any(json.loads(p.read_text())['outcome'] == 'prepared' for p in attempts))
PY
    if [ -n "${{FAIL_COMPOSE:-}}" ]; then [ -z "${{STUB_COMPOSE_STDERR:-}}" ] || echo "$STUB_COMPOSE_STDERR" >&2; exit 1; fi
    case " ${{FAIL_COMPOSE_TAGS:-}} " in *" ${{HERDR_CP_IMAGE_TAG:-}} "*) exit 1 ;; esac
    echo "${{HERDR_CP_IMAGE_TAG:-}}" > {running}
    if [ -e {missing} ]; then   # the services this up names exist again
      keep=""
      for m in $(cat {missing}); do
        svc="$(case "$m" in herdr-cp-web) echo control-plane-web ;; herdr-cp-worker) echo control-plane-worker ;;
          herdr-llm-proxy) echo llm-proxy ;; herdr-collab-public) echo collab-public ;; herdr-collab-edge) echo collab-edge ;; esac)"
        case " $* " in *" $svc "*) ;; *) keep="$keep $m" ;; esac
      done
      echo "$keep" > {missing}
    fi
    if [ -z "$pins" ] || [ -z "${{STUB_IGNORE_ROLLBACK_MOUNT:-}}" ]; then
      printf %s "$HERDR_CP_FLEET_DIR" > {running_mount}
    fi
    python3 - "$pins" <<'PY'
import json, os, pathlib, sys
names = {{'control-plane-web':'herdr-cp-web', 'control-plane-worker':'herdr-cp-worker',
         'llm-proxy':'herdr-llm-proxy', 'collab-public':'herdr-collab-public', 'collab-edge':'herdr-collab-edge'}}
ids = {{v: 'sha256:' + 'b' * 64 for v in names.values()}}
if sys.argv[1] and not os.environ.get('STUB_IGNORE_ROLLBACK_IMAGES'):
    ids.update({{names[k]: v['image'] for k,v in json.loads(pathlib.Path(sys.argv[1]).read_text())['services'].items()}})
pathlib.Path({str(running_images)!r}).write_text(json.dumps(ids))
pathlib.Path({str(running_config)!r}).write_text(ids['herdr-cp-web'] if sys.argv[1] else 'herdr-engineering:' + os.environ['HERDR_CP_IMAGE_TAG'])
PY
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
    if [ -n "${{STUB_READY_AFTER:-}}" ]; then
      count="$(cat {health_reads} 2>/dev/null || echo 0)"
      count=$((count + 1)); echo "$count" > {health_reads}
      [ "$count" -gt "$STUB_READY_AFTER" ] || exit 7
    fi
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
             HERDR_CP_HEALTH_INTERVAL="0", STUB_FLEET_DIR=str(central.parent))
    return {"home": home, "central": central, "log": log, "env": e, "secrets": secrets,
            "ref": candidate_ref,
            "tmp": tmp_path}


def run(script, e, *args, timeout=120):
    return subprocess.run(["bash", str(script), *args], env=e["env"], capture_output=True,
                          text=True, timeout=timeout, cwd=ROOT)


def deploy(e, *args, central=True):
    base = ["--no-fetch", "--ref", e["ref"]]
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


def test_default_readiness_budget_waits_past_the_old_thirty_probe_window(env):
    env['env'].pop('HERDR_CP_HEALTH_TRIES')
    env['env']['STUB_READY_AFTER'] = '35'
    result = deploy(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ok:' in result.stdout
    assert 'rolled back' not in result.stdout + result.stderr
    assert int((env['tmp'] / 'health-reads').read_text()) > 35


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
    run(DEPLOY, env, "--no-fetch", "--ref", env["ref"], "--fleet-config", str(link))
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
    sha = _head()
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
    # Failure retains recovery metadata, but never a successful deploy receipt.
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    assert not (state / f"{_head()}.json").exists()


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
    # collab-sync.sh's HerdR-owned certbot layout under the fake HOME (#115): a lineage
    # to copy and a certbot to put in the renew unit. Nothing runs certbot.
    acme = e["home"] / ".local/share/herdr-engineering/collab-acme"
    live = acme / "config/live/localdev.mcp.metabolomics.us"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_text("not a real cert\n")
    (live / "privkey.pem").write_text("not a real key\n")
    certbot = e["home"] / ".local/share/herdr-engineering/collab-acme-venv/bin/certbot"
    certbot.parent.mkdir(parents=True)
    certbot.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
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


def test_collab_sync_defaults_to_herdr_owned_renewal_state(env):
    _with_collab(env)
    config = env["tmp"] / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), config)
    sync = ROOT / "deploy" / "control-plane" / "collab-sync.sh"
    r = run(sync, env, "install", "--config", str(config),
            "--cert-dir", str(env["tmp"] / "edge-certs"))
    assert r.returncode == 0, r.stderr

    renew_unit = (env["home"] / ".config/systemd/user/herdr-collab-cert-renew.service").read_text()
    certbot = env["home"] / ".local/share/herdr-engineering/collab-acme-venv/bin/certbot"
    acme = env["home"] / ".local/share/herdr-engineering/collab-acme"
    assert f'ExecStart="{certbot}" renew' in renew_unit
    assert f'--config-dir "{acme}/config"' in renew_unit
    assert "inferweave/bender-failover" not in renew_unit


def test_collab_renewal_dry_run_skips_certbot_random_sleep(env):
    _with_collab(env)
    config = env["tmp"] / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), config)
    sync = ROOT / "deploy" / "control-plane" / "collab-sync.sh"
    r = run(sync, env, "renew-check", "--config", str(config))
    assert r.returncode == 0, r.stderr

    acme = env["home"] / ".local/share/herdr-engineering/collab-acme"
    assert r.stdout.strip() == (
        "renew --dry-run --no-random-sleep-on-renew --non-interactive "
        "--cert-name localdev.mcp.metabolomics.us "
        f"--config-dir {acme}/config --work-dir {acme}/work --logs-dir {acme}/logs"
    )


def test_collab_custom_acme_dir_derives_its_certbot_venv(env):
    _with_collab(env)
    config = env["tmp"] / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), config)
    acme = env["tmp"] / "tenant-acme"
    certbot = Path(f"{acme}-venv/bin/certbot")
    certbot.parent.mkdir(parents=True)
    certbot.write_text("#!/bin/sh\nprintf 'custom:%s\\n' \"$*\"\n")
    certbot.chmod(0o755)

    sync = ROOT / "deploy" / "control-plane" / "collab-sync.sh"
    r = run(sync, env, "renew-check", "--config", str(config), "--acme-dir", str(acme))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("custom:renew --dry-run --no-random-sleep-on-renew ")
    assert f"--config-dir {acme}/config" in r.stdout


def test_collab_explicit_certbot_overrides_custom_acme_derivation(env):
    _with_collab(env)
    config = env["tmp"] / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), config)
    acme = env["tmp"] / "tenant-acme"
    certbot = env["tmp"] / "operator-bin/certbot"
    certbot.parent.mkdir(parents=True)
    certbot.write_text("#!/bin/sh\nprintf 'explicit:%s\\n' \"$*\"\n")
    certbot.chmod(0o755)

    sync = ROOT / "deploy" / "control-plane" / "collab-sync.sh"
    r = run(sync, env, "renew-check", "--config", str(config), "--acme-dir", str(acme),
            "--certbot", str(certbot))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("explicit:renew --dry-run --no-random-sleep-on-renew ")
    assert f"--config-dir {acme}/config" in r.stdout


def test_collab_sync_provisions_the_operators_invitees_from_the_slice_not_env(env):
    # #119 (third origin/main merge): the operator, pool and group reach collab-provision.py as
    # flags from the slice; main's HERDR_COLLAB_OPERATOR_USER / _POOL / _GROUP env is ignored.
    _with_collab(env)
    out = env["tmp"] / "slice.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), out)
    data = env["tmp"] / "collab-data"
    data.mkdir()
    (data / "collab.jsonl").write_text(json.dumps({"event_type": "invite_created", "payload": {
        "invite_id": "i1", "email": "new@example.org", "actor_user_id": "owner-test",
        "expires_at_epoch": 4_000_000_000}}) + "\n" + json.dumps({"event_type": "invite_created", "payload": {
        "invite_id": "i2", "email": "other@example.org", "actor_user_id": "env-operator",
        "expires_at_epoch": 4_000_000_000}}) + "\n")
    bindir = env["tmp"] / "bin"
    (bindir / "curl").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bindir / "curl").chmod(0o755)
    (bindir / "aws").write_text(f"""#!/usr/bin/env bash
echo "aws $*" >> {env['tmp']}/aws.log
case "$*" in
  *list-users*) echo '{{"Users": []}}' ;;
  *admin-create-user*) echo '{{"User": {{"Username": "sub-new"}}}}' ;;
esac
""")
    (bindir / "aws").chmod(0o755)
    env["env"].update({"HERDR_COLLAB_OPERATOR_USER": "env-operator", "HERDR_COLLAB_POOL": "us-east-1_FROMENV",
                       "HERDR_COLLAB_GROUP": "env.group", "HERDR_COLLAB_DATA_DIR": str(env["tmp"] / "nowhere")})
    sync = ROOT / "deploy" / "control-plane" / "collab-sync.sh"
    r = run(sync, env, "once", "--config", str(out), "--dir", str(env["tmp"] / "sync"), "--data-dir", str(data))
    assert r.returncode != 0                     # the stub curl fails after provisioning
    log = (env["tmp"] / "aws.log").read_text()
    assert "admin-add-user-to-group --user-pool-id=us-west-2_TESTPOOL --username=sub-new --group-name=team.test" in log
    assert "--region us-west-2" in log
    assert "other@example.org" not in log and "FROMENV" not in log and "env.group" not in log
    ledger = env["home"] / ".local/share/herdr-engineering/collab-provision/ledger.jsonl"
    assert json.loads(ledger.read_text())["result"] == "added"


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
    e["env"].update(STUB_PREVIOUS_TAG=tag, STUB_PREVIOUS_COMMIT=tag)
    return full, tag


def _layout_commit():
    """Oldest commit whose compose.yaml has the current deploy-layout marker: a valid
    automatic-rollback target that differs from HEAD (the commit being deployed)."""
    first = _git("log", "--format=%H", "-S", "x-herdr-deploy-layout: 2", "--",
                 "deploy/control-plane/compose.yaml").splitlines()[-1]
    if first == _git("rev-parse", "HEAD"):
        pytest.skip("HEAD introduced the layout marker; no older layout-2 commit to roll back to")
    return first


def _assert_deploy_worktrees_removed(e):
    # Other sessions may have a live rollback-worktree. Verify every worktree
    # created by THIS invocation, including Git registration and filesystem state.
    registered = {line.removeprefix("worktree ")
                  for line in _git("worktree", "list", "--porcelain").splitlines()
                  if line.startswith("worktree ")}
    trees = set()
    for line in e["log"].read_text().splitlines():
        if line.startswith("envfile "):
            trees.add(Path(line.split()[1]).parent / "rollback-worktree")
        elif line.startswith("composefiles "):
            trees.update(Path(p).parents[2] for p in line.split()[2:]
                         if Path(p).name == "compose.yaml")
    assert trees
    for tree in trees:
        assert str(tree) not in registered, tree
        assert not tree.exists(), tree


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
    _assert_deploy_worktrees_removed(env)


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
    _assert_deploy_worktrees_removed(env)
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


def test_failed_rollout_restores_previous_config_and_immutable_images(env):
    """A candidate config must never be reused as the rollback config."""
    _, prev_tag = _seed_previous(env, _layout_commit())
    previous = env["tmp"] / "live-previous"
    previous.mkdir()
    old_bytes = env["central"].read_bytes()
    (previous / "fleet-config.yaml").write_bytes(old_bytes)
    env["env"]["STUB_FLEET_DIR"] = str(previous)
    data = yaml.safe_load(old_bytes)
    data["sections"]["control_plane"]["values"]["public_url"] = "https://candidate.example.test"
    data["sections"]["control_plane"]["values"]["compose"]["HERDR_CP_WEB_PUBLIC_PORT"] = "8181"
    fcc.write_private_file(env["central"], yaml.safe_dump(data))
    candidate_bytes = env["central"].read_bytes()
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    metadata_files = list(state.glob("attempt-*/metadata.json"))
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text())
    assert metadata["outcome"] == "rolled_back"
    snapshot = Path(metadata["previous"]["fleet_config_snapshot"])
    assert snapshot.read_bytes() == old_bytes
    assert Path(metadata["candidate"]["fleet_config_snapshot"]).read_bytes() == candidate_bytes
    assert metadata["previous"]["fleet_config_source"] == str(previous / "fleet-config.yaml")
    assert metadata["candidate"]["fleet_config_source"] == str(env["central"])
    assert metadata["previous"]["images"]["control-plane-web"] == "sha256:" + "a" * 64
    assert metadata["candidate"]["image_id"] == "sha256:" + "b" * 64
    log = env["log"].read_text()
    assert f"fleetdir={snapshot.parent} tag={prev_tag}" in log
    assert "prepared_before_recreate True" in log
    assert f"webport tag={_head()} HERDR_CP_WEB_PUBLIC_PORT='8181'" in log
    assert f"webport tag={prev_tag} HERDR_CP_WEB_PUBLIC_PORT='8080'" in log
    pins = json.loads(next(ln.removeprefix("pinned ") for ln in log.splitlines() if ln.startswith("pinned ")))
    assert pins["services"]["control-plane-web"]["image"] == "sha256:" + "a" * 64
    assert set(pins["services"]) == {"control-plane-web", "control-plane-worker", "llm-proxy"}
    assert env["central"].read_bytes() == candidate_bytes  # no host config rewrite
    for p in (snapshot, Path(metadata["candidate"]["fleet_config_snapshot"]), metadata_files[0]):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    for p in (state, metadata_files[0].parent, snapshot.parent):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700
    for secret in env["secrets"]:
        assert secret not in metadata_files[0].read_text() + r.stdout + r.stderr


def test_success_receipt_retains_protected_config_but_mounts_canonical_source(env):
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    receipt = json.loads((state / f"{_head()}.json").read_text())
    assert receipt["image_id"] == "sha256:" + "b" * 64
    snapshot = Path(receipt["fleet_config_snapshot"])
    assert snapshot.is_relative_to(state)
    assert snapshot.read_bytes() == env["central"].read_bytes()
    assert receipt["fleet_config_source"] == str(env["central"].resolve())
    assert f"fleetdir={env['central'].parent} tag={_head()}" in env["log"].read_text()
    assert json.loads(Path(receipt["rollback_metadata"]).read_text())["outcome"] == "succeeded"


def _seed_pinned_previous(env):
    full, tag = _seed_previous(env, _layout_commit())
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    state.chmod(0o700)
    mounted = state / "protected-rollback"
    mounted.mkdir(mode=0o700)
    saved = mounted / "fleet-config.yaml"
    saved.write_bytes(env["central"].read_bytes())
    saved.chmod(0o600)
    env["env"].update(STUB_RUNNING_IMAGE="sha256:" + "a" * 64,
                      STUB_FLEET_DIR=str(mounted))
    return full, tag, state, saved


@pytest.mark.parametrize("full_stamp", [False, True])
def test_pinned_previous_resolves_receipt_and_keeps_actual_mount_and_service_ids(env, full_stamp):
    full, tag, state, saved = _seed_pinned_previous(env)
    if full_stamp:
        env["env"]["STUB_PREVIOUS_COMMIT"] = full
    receipt_path = state / f"{tag}.json"
    receipt = json.loads(receipt_path.read_text())
    # A recorded immutable ID remains valid even if the mutable tag has moved.
    receipt.update(image_id="sha256:" + "a" * 64,
                   fleet_config_snapshot=str(state / "obsolete-snapshot"))
    receipt_path.write_text(json.dumps(receipt))
    env["env"]["STUB_PREVIOUS_TAG_ID"] = "sha256:" + "f" * 64
    ids = {"herdr-cp-web": "sha256:" + "a" * 64,
           "herdr-cp-worker": "sha256:" + "c" * 64,
           "herdr-llm-proxy": "sha256:" + "d" * 64}
    (env["tmp"] / "running-images.json").write_text(json.dumps(ids))
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    assert f"automatic rollback target: sha256:{'a' * 64} (commit {tag})" in r.stdout
    metadata = json.loads(next(state.glob("attempt-*/metadata.json")).read_text())
    previous = metadata["previous"]
    assert previous["commit"] == full
    assert previous["fleet_config_source"] == str(saved)
    assert Path(previous["fleet_config_snapshot"]).read_bytes() == saved.read_bytes()
    assert previous["images"] == {
        "control-plane-web": ids["herdr-cp-web"],
        "control-plane-worker": ids["herdr-cp-worker"], "llm-proxy": ids["herdr-llm-proxy"],
    }
    assert json.loads((state / f"{_head()}.json").read_text())["previous_commit"] == full


def test_next_failed_candidate_after_pinned_rollback_is_still_protected(env):
    full, tag = _seed_previous(env, _layout_commit())
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    old_bytes = env["central"].read_bytes()
    first = deploy(env)
    assert first.returncode == 1 and "and healthy" in first.stderr
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    first_path = next(state.glob("attempt-*/metadata.json"))
    first_metadata = json.loads(first_path.read_text())
    first_snapshot = Path(first_metadata["previous"]["fleet_config_snapshot"])
    data = yaml.safe_load(old_bytes)
    data["sections"]["control_plane"]["values"]["compose"]["HERDR_CP_WEB_PUBLIC_PORT"] = "8181"
    fcc.write_private_file(env["central"], yaml.safe_dump(data))
    second = deploy(env)
    assert second.returncode == 1 and "and healthy" in second.stderr, second.stderr
    second_path, = set(state.glob("attempt-*/metadata.json")) - {first_path}
    metadata = json.loads(second_path.read_text())
    assert metadata["outcome"] == "rolled_back"
    assert metadata["previous"]["commit"] == full
    assert metadata["previous"]["fleet_config_source"] == str(first_snapshot)
    snapshot = Path(metadata["previous"]["fleet_config_snapshot"])
    assert snapshot.read_bytes() == old_bytes != env["central"].read_bytes()
    assert metadata["previous"]["images"] == first_metadata["previous"]["images"]
    assert f"fleetdir={snapshot.parent} tag={tag}" in env["log"].read_text()
    for path in (snapshot, second_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for secret in env["secrets"]:
        assert secret not in second.stdout + second.stderr + second_path.read_text()
    _assert_deploy_worktrees_removed(env)


@pytest.mark.parametrize("fault", [
    "missing_stamp", "invalid_stamp", "duplicate_stamp", "wrong_stamp", "wrong_full_stamp",
    "missing_receipt", "wrong_commit", "wrong_receipt_tag", "wrong_receipt_id", "retagged",
    "wrong_config_id", "unprotected_mount",
])
def test_pinned_previous_identity_mismatch_refuses_before_mutation(env, fault):
    full, tag, state, saved = _seed_pinned_previous(env)
    receipt_path = state / f"{tag}.json"
    receipt = json.loads(receipt_path.read_text())
    if fault.endswith("stamp"):
        stamps = {
            "missing_stamp": [], "invalid_stamp": ["HERDR_ENGINEERING_COMMIT=secret-invalid"],
            "duplicate_stamp": [f"HERDR_ENGINEERING_COMMIT={tag}"] * 2,
            "wrong_stamp": ["HERDR_ENGINEERING_COMMIT=" + "f" * 12],
            "wrong_full_stamp": ["HERDR_ENGINEERING_COMMIT=" + tag + "f" * 28],
        }
        env["env"]["STUB_IMAGE_ENV"] = json.dumps(stamps[fault] + ["UNRELATED_SECRET=never-print-this"])
    elif fault == "wrong_commit":
        receipt["commit"] = "f" * 40
    elif fault == "wrong_receipt_tag":
        receipt["image"] = "herdr-engineering:" + "f" * 12
    elif fault == "wrong_receipt_id":
        receipt["image_id"] = "sha256:" + "f" * 64
    elif fault == "retagged":
        env["env"]["STUB_PREVIOUS_TAG_ID"] = "sha256:" + "f" * 64
    elif fault == "wrong_config_id":
        env["env"]["STUB_RUNNING_IMAGE"] = "sha256:" + "f" * 64
    elif fault == "unprotected_mount":
        saved.parent.chmod(0o755)
    receipt_path.write_text(json.dumps(receipt))
    if fault == "missing_receipt":
        receipt_path.unlink()
    r = deploy(env)
    assert r.returncode != 0, r.stdout
    assert " up " not in env["log"].read_text()
    assert not (env["home"] / ".config/herdr-engineering/llm-proxy").exists()
    assert "deployment recovery state operation failed" in r.stderr
    assert "never-print-this" not in r.stdout + r.stderr
    assert "secret-invalid" not in r.stdout + r.stderr


@pytest.mark.parametrize("retagged", [False, True])
def test_legacy_receipt_without_buildstamp_requires_tag_to_match_running_id(env, retagged):
    full, _ = _seed_previous(env, _layout_commit())
    env["env"]["STUB_IMAGE_ENV"] = "[]"
    if retagged:
        env["env"]["STUB_PREVIOUS_TAG_ID"] = "sha256:" + "f" * 64
    r = deploy(env)
    if retagged:
        assert r.returncode != 0
        assert " up " not in env["log"].read_text()
        assert "deployment recovery state operation failed" in r.stderr
    else:
        assert r.returncode == 0, r.stderr
        receipt = env["home"] / f".local/state/herdr-engineering/deploys/{_head()}.json"
        assert json.loads(receipt.read_text())["previous_commit"] == full


def test_rollback_prefers_last_deployed_snapshot_when_canonical_file_was_edited(env):
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    saved = state / "last-good" / "fleet-config.yaml"
    saved.parent.mkdir(parents=True, mode=0o700)
    old_bytes = env["central"].read_bytes()
    saved.write_bytes(old_bytes)
    saved.chmod(0o600)
    _seed_previous(env, _layout_commit(), {
        "image_id": "sha256:" + "a" * 64,
        "fleet_config_source": str(env["central"]), "fleet_config_snapshot": str(saved),
    })
    data = yaml.safe_load(old_bytes)
    data["sections"]["control_plane"]["values"]["public_url"] = "https://changed.example.test"
    fcc.write_private_file(env["central"], yaml.safe_dump(data))
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    metadata = json.loads(next(state.glob("attempt-*/metadata.json")).read_text())
    assert metadata["outcome"] == "rolled_back"
    assert Path(metadata["previous"]["fleet_config_snapshot"]).read_bytes() == old_bytes
    assert env["central"].read_bytes() != old_bytes


@pytest.mark.parametrize("fault", ["unavailable", "wrong_image", "unprotected"])
def test_invalid_previous_snapshot_aborts_before_service_mutation(env, fault):
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    saved = state / "last-good" / "fleet-config.yaml"
    if fault != "unavailable":
        saved.parent.mkdir(parents=True, mode=0o700)
        saved.write_bytes(env["central"].read_bytes())
        saved.chmod(0o600)
        if fault == "unprotected":
            saved.parent.chmod(0o755)
    _seed_previous(env, _layout_commit(), {
        "image_id": "sha256:" + ("c" if fault == "wrong_image" else "a") * 64,
        "fleet_config_snapshot": str(saved),
    })
    r = deploy(env)
    assert r.returncode != 0
    log = env["log"].read_text()
    assert " up " not in log
    assert not (env["home"] / ".config/herdr-engineering/llm-proxy").exists()
    assert "deployment recovery state operation failed" in r.stderr


@pytest.mark.parametrize("fault", ["IMAGES", "MOUNT"])
def test_rollback_cannot_claim_success_on_health_with_wrong_image_or_config(env, fault):
    _seed_previous(env, _layout_commit())
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    env["env"][f"STUB_IGNORE_ROLLBACK_{fault}"] = "1"
    r = deploy(env)
    assert r.returncode == 3
    assert "FAILED TO ROLL BACK" in r.stderr and "and healthy" not in r.stderr
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    metadata = json.loads(next(state.glob("attempt-*/metadata.json")).read_text())
    assert metadata["outcome"] == "rollback_failed"


def test_full_collab_rollback_pins_edge_without_replacing_command_overlay(env):
    _with_collab(env)
    _, prev_tag = _seed_previous(env, _layout_commit())
    target = _head()
    env["env"]["STUB_UNHEALTHY_TAGS"] = target
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    state = env["home"] / ".local/state/herdr-engineering/deploys"
    metadata_path = next(state.glob("attempt-*/metadata.json"))
    metadata = json.loads(metadata_path.read_text())
    assert metadata["outcome"] == "rolled_back"
    pins = json.loads((metadata_path.parent / "rollback-images.json").read_text())
    assert set(pins["services"]) == {
        "control-plane-web", "control-plane-worker", "llm-proxy", "collab-public", "collab-edge",
    }
    assert all(set(v) == {"image"} for v in pins["services"].values())
    log = env["log"].read_text()
    rb = next(line for line in log.splitlines() if line.startswith(f"composefiles tag={prev_tag} "))
    assert "compose.collab.yaml" in rb and rb.endswith("rollback-images.json")
    assert "collab-public collab-edge" in log


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
    renew_unit = (units / "herdr-collab-cert-renew.service").read_text()
    certbot = env["home"] / ".local/share/herdr-engineering/collab-acme-venv/bin/certbot"
    acme = env["home"] / ".local/share/herdr-engineering/collab-acme"
    assert f'ExecStart="{certbot}" renew' in renew_unit
    assert f'--config-dir "{acme}/config"' in renew_unit
    assert "inferweave/bender-failover" not in renew_unit
    # #119: the invite provisioner reads collab-public's log in the data dir deploy.sh mounts
    data_dir = env["home"] / ".local/share/herdr-engineering/collab"
    unit = (units / "herdr-collab-sync.service").read_text()
    assert f'--data-dir "{data_dir}"' in unit
    assert "Environment=" not in unit and "HERDR_COLLAB_OPERATOR_USER" not in unit
    assert (env["home"] / ".local/share/herdr-engineering/collab-provision.py").is_file()
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
    assert receipt["ref"] == env["ref"]


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


# ---------------------------------------------------------------- GitHub App key mount

def _app_dir(e):
    return e["home"] / ".config/herdr-engineering/github-app"


def test_compose_mounts_the_github_app_dir_read_only_into_web_only():
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())
    mount = "${HERDR_CP_GITHUB_APP_DIR:?run deploy.sh}:/etc/herdr/github-app:ro"
    assert mount in doc["services"]["control-plane-web"]["volumes"]
    for name, svc in doc["services"].items():
        if name != "control-plane-web":
            assert not any("github-app" in str(v) for v in svc.get("volumes") or []), name


def test_dry_run_reports_the_github_app_mount_and_creates_nothing(env):
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "/etc/herdr/github-app" in r.stdout and "would create it empty" in r.stdout
    assert not _app_dir(env).exists()


def test_deploy_creates_an_empty_0700_github_app_dir_and_mounts_it(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    deploy(env)
    d = _app_dir(env)
    assert d.is_dir() and (d.stat().st_mode & 0o777) == 0o700 and not list(d.iterdir())
    lines = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("composeenv ")]
    assert lines and all(f"githubappdir={d}" in ln for ln in lines)


def test_a_valid_github_app_key_dir_is_accepted_and_the_key_never_printed(env):
    d = _app_dir(env)
    d.mkdir(parents=True, mode=0o700)
    d.chmod(0o700)
    key = d / "github-app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nPLACEHOLDERKEYMATERIAL\n-----END PRIVATE KEY-----\n")
    key.chmod(0o600)
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "PLACEHOLDERKEYMATERIAL" not in r.stdout + r.stderr
    assert "would create" not in r.stdout


@pytest.mark.parametrize("fault,needle", [
    ("dir_mode", "must be mode 0700"),
    ("extra_file", "must hold only github-app.pem"),
    ("key_mode", "must be mode 0600 or 0400"),
    ("key_symlink", "must be a regular file"),
    ("dir_symlink", "must be a real directory"),
    ("key_dir", "must be a regular file"),
])
def test_a_bad_github_app_dir_is_refused_before_anything_runs(env, fault, needle):
    d = _app_dir(env)
    if fault == "dir_symlink":
        real = env["tmp"] / "elsewhere"
        real.mkdir(mode=0o700)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.symlink_to(real)
    else:
        d.mkdir(parents=True, mode=0o700)
        d.chmod(0o700)
        key = d / "github-app.pem"
        if fault == "key_symlink":
            target = env["tmp"] / "key.pem"
            target.write_text("k")
            target.chmod(0o600)
            key.symlink_to(target)
        elif fault == "key_dir":
            key.mkdir()
        else:
            key.write_text("k")
            key.chmod(0o640 if fault == "key_mode" else 0o600)
        if fault == "dir_mode":
            d.chmod(0o750)
        if fault == "extra_file":
            (d / "notes.txt").write_text("x")
    r = deploy(env, "--dry-run")
    assert r.returncode != 0
    assert needle in r.stderr
    assert not env["log"].exists()


# ------------- 2026-10-29: deploy vs. the collab-sync timer, and missing containers

def _sync_lock(e):
    return e["home"] / ".local/share/herdr-engineering/collab-sync.lock"


def _lock_is_free(e, wait=5.0):
    """True once nothing holds the lock (a just-exited deploy's children may linger briefly)."""
    import time
    deadline = time.monotonic() + wait
    while True:
        free = subprocess.run(["flock", "-n", str(_sync_lock(e)), "true"]).returncode == 0
        if free or time.monotonic() > deadline:
            return free
        time.sleep(0.05)


def _metadata(e):
    state = e["home"] / ".local/state/herdr-engineering/deploys"
    return json.loads(next(state.glob("attempt-*/metadata.json")).read_text())


def test_collab_lock_held_by_another_process_fails_before_touching_anything(env):
    import time
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    lock = _sync_lock(env)
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(["flock", str(lock), "sleep", "60"])
    try:
        while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
            time.sleep(0.02)
        r = deploy(env, "--collab-lock-wait", "1")
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "collab-sync is still running after 1s" in r.stderr and "nothing was changed" in r.stderr
    log = env["log"].read_text()
    assert " up " not in log and "systemctl" not in log
    assert not (env["home"] / ".config/herdr-engineering/llm-proxy/llm-config.json").exists()
    assert not (env["home"] / ".config/herdr-engineering/collab/collab-config.json").exists()
    assert _metadata(env)["outcome"] == "collab_sync_busy"


@pytest.mark.parametrize("path", ["succeeded", "rolled_back", "rollback_failed"])
def test_deploy_holds_the_collab_lock_across_compose_and_rollback_and_releases_it(env, path):
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    if path == "rolled_back":
        env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    elif path == "rollback_failed":
        env["env"]["STUB_UNHEALTHY_TAGS"] = f"{_head()} {_layout_commit()[:12]}"
    r = deploy(env)
    assert r.returncode == {"succeeded": 0, "rolled_back": 1, "rollback_failed": 3}[path], r.stderr
    assert _metadata(env)["outcome"] == path
    log = env["log"].read_text().splitlines()
    held = [ln for ln in log if ln.startswith("collablock ")]
    assert held and set(held) == {"collablock held"}            # every up ran under the lock
    if path != "succeeded":
        assert len(held) >= 2                                   # forward AND rollback
    # the refresh deploy.sh runs itself was not skipped by its own lock
    assert "another collab-sync is running" not in r.stderr
    assert (env["home"] / ".local/share/herdr-engineering/collab-sync/jwks.json").is_file()
    unit = (env["home"] / ".config/systemd/user/herdr-collab-sync.service")
    assert path != "succeeded" or "--lock-held" not in unit.read_text()
    assert not any(" stop " in ln or " disable " in ln for ln in log if ln.startswith("systemctl"))
    assert _lock_is_free(env)


def test_collab_lock_is_released_when_the_deploy_is_terminated(env):
    import signal
    import time
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    env["env"].update(STUB_READY_AFTER="100000", HERDR_CP_HEALTH_TRIES="100000",
                      HERDR_CP_HEALTH_INTERVAL="0.05")
    p = subprocess.Popen(["bash", str(DEPLOY), "--no-fetch", "--ref", env["ref"],
                          "--fleet-config", str(env["central"])], env=env["env"], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 90
        while "api/v1/health" not in (env["log"].read_text() if env["log"].exists() else ""):
            assert p.poll() is None and time.monotonic() < deadline, p.communicate()
            time.sleep(0.05)
        assert not _lock_is_free(env, wait=0)                   # held mid-deploy
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 143
    assert _lock_is_free(env)


def test_removal_in_progress_is_waited_out_and_retried(env):
    _with_collab(env)
    env["env"]["STUB_REMOVAL_IN_PROGRESS"] = "1"
    r = deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok:" in r.stdout
    log = env["log"].read_text()
    assert log.count("removalinprogress") == 1
    assert "removal of container herdr-collab-public is already in progress" in r.stderr
    assert "docker inspect -f {{.State.Status}} herdr-collab-public" in log
    ups = [ln for ln in log.splitlines() if ln.startswith("docker compose") and " up " in ln]
    # first deploy = changed slice: the failed collab-public up, its retry, then the rest once
    assert [("--force-recreate collab-public" in ln) for ln in ups] == [True, True, False]


def test_removal_in_progress_retries_are_bounded(env):
    _with_collab(env)
    env["env"]["STUB_REMOVAL_IN_PROGRESS"] = "10"
    r = deploy(env)
    assert r.returncode == 1 and "ok:" not in r.stdout
    assert env["log"].read_text().count("removalinprogress") == 4    # first try + 3 retries
    assert "NOT rolled back" in r.stderr


def test_rollback_recreates_a_collab_public_the_failed_forward_removed(env):
    _with_collab(env)
    _, prev_tag = _seed_previous(env, _layout_commit())
    env["env"].update(FAIL_COMPOSE_TAGS=_head(), STUB_FAIL_REMOVES="herdr-collab-public")
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    assert "and healthy" in r.stderr and "collab-public collab-edge" in r.stderr
    assert _metadata(env)["outcome"] == "rolled_back"
    rb = [ln for ln in env["log"].read_text().splitlines()
          if ln.startswith("docker compose") and " up " in ln and "rollback-worktree" in ln]
    assert rb and " collab-public " in rb[-1] + " "
    assert "herdr-collab-public" not in (env["tmp"] / "missing").read_text()


def test_capture_records_an_absent_service_and_rollback_recreates_it_from_the_web_image(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    (env["tmp"] / "missing").write_text("herdr-collab-public")
    env["env"]["STUB_UNHEALTHY_TAGS"] = _head()
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    assert "missing service containers (will be created by this deploy): collab-public" in r.stdout
    metadata = _metadata(env)
    assert metadata["outcome"] == "rolled_back"
    previous = metadata["previous"]
    assert previous["absent"] == ["collab-public"]
    assert previous["images"]["collab-public"] == previous["images"]["control-plane-web"]
    pins = json.loads(next(ln.removeprefix("pinned ") for ln in env["log"].read_text().splitlines()
                           if ln.startswith("pinned ")))
    assert pins["services"]["collab-public"]["image"] == previous["images"]["control-plane-web"]


def test_absent_edge_is_not_pinned_and_comes_from_the_compose_file(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    (env["tmp"] / "missing").write_text("herdr-collab-edge")
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    previous = _metadata(env)["previous"]
    assert previous["absent"] == ["collab-edge"] and "collab-edge" not in previous["images"]


def test_missing_container_at_deploy_start_is_repaired_by_a_normal_deploy(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    (env["tmp"] / "missing").write_text("herdr-collab-public")
    r = deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok:" in r.stdout and "deployment recovery state operation failed" not in r.stderr
    assert (env["tmp"] / "missing").read_text().split() == []


def test_capture_still_fails_on_an_inspect_error_that_is_not_absence(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    env["env"]["STUB_INSPECT_ERROR"] = "herdr-collab-public"
    r = deploy(env)
    assert r.returncode != 0
    assert "deployment recovery state operation failed" in r.stderr
    assert "permission denied" not in r.stdout + r.stderr
    assert " up " not in env["log"].read_text()


# ------------- fix round: single recreate, deploy-wide lock, tight retry match

def _deploy_lock(e):
    return e["home"] / ".local/state/herdr-engineering/deploys/deploy.lock"


def _public_ups(e):
    return [ln for ln in e["log"].read_text().splitlines()
            if ln.startswith("docker compose") and " up " in ln and " collab-public" in ln]


def test_changed_slice_recreates_collab_public_exactly_once_and_reloads_the_edge(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit(), {"collab_slice_sha256": "0" * 64})
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    ups = _public_ups(env)
    assert len(ups) == 1 and "--force-recreate collab-public" in ups[0]
    rest = [ln for ln in env["log"].read_text().splitlines()
            if ln.startswith("docker compose") and " up " in ln and ln not in ups]
    assert len(rest) == 1 and rest[0].endswith("control-plane-web control-plane-worker llm-proxy collab-edge")
    assert "docker exec herdr-collab-edge nginx -s reload" in env["log"].read_text()


def test_unchanged_slice_recreates_collab_public_once_without_forcing(env):
    _with_collab(env)
    _seed_previous(env, _layout_commit())
    first = deploy(env)
    assert first.returncode == 0, first.stderr
    env["log"].write_text("")
    env["env"]["STUB_RUNNING_IMAGE"] = f"herdr-engineering:{_head()}"
    r = deploy(env)
    assert r.returncode == 0, r.stderr
    ups = _public_ups(env)
    assert len(ups) == 1 and "--force-recreate" not in ups[0]


def test_generic_compose_failure_runs_up_exactly_once(env):
    _with_collab(env)
    env["env"].update(FAIL_COMPOSE="1", STUB_COMPOSE_STDERR="Error: some other operation is already in progress")
    r = deploy(env)
    assert r.returncode == 1
    ups = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("docker compose") and " up " in ln]
    assert len(ups) == 1, ups
    assert "waiting for it, then retrying" not in r.stderr


def test_deploy_lock_held_elsewhere_fails_before_changing_anything(env):
    import time
    lock = _deploy_lock(env)
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(["flock", str(lock), "sleep", "60"])
    try:
        while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
            time.sleep(0.02)
        r = deploy(env, "--deploy-lock-wait", "1")
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 1
    assert "another deploy.sh is still running after 1s" in r.stderr and "nothing was changed" in r.stderr
    assert not env["log"].exists()                       # never reached docker
    assert not list(lock.parent.glob("attempt-*"))


def test_concurrent_deploys_serialize(env):
    import time
    lock = _deploy_lock(env)
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(["flock", str(lock), "sleep", "1"])
    while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
        time.sleep(0.02)
    started = time.monotonic()
    r = deploy(env, "--deploy-lock-wait", "60")
    holder.wait()
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - started >= 0.5                  # it waited for the holder
    assert subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0


def test_second_concurrent_deploy_fails_cleanly_while_the_first_runs(env):
    import time
    env["env"].update(STUB_READY_AFTER="100000", HERDR_CP_HEALTH_TRIES="100000",
                      HERDR_CP_HEALTH_INTERVAL="0.05")
    first = subprocess.Popen(["bash", str(DEPLOY), "--no-fetch", "--ref", env["ref"],
                              "--fleet-config", str(env["central"])], env=env["env"], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 90
        while "api/v1/health" not in (env["log"].read_text() if env["log"].exists() else ""):
            assert first.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        before = env["log"].read_text()
        second = deploy(env, "--deploy-lock-wait", "1")
        assert second.returncode == 1 and "another deploy.sh is still running" in second.stderr
        assert "docker build" not in env["log"].read_text()[len(before):]
    finally:
        first.kill()
        first.wait()


def test_web_absent_at_capture_aborts_preparation(env):
    """deploy-state.py prepare: only the WEB is load-bearing; if it vanishes the attempt aborts."""
    (env["tmp"] / "missing").write_text("herdr-cp-web")
    env["env"]["STUB_RUNNING_IMAGE"] = "herdr-engineering:" + "0" * 12
    state = env["tmp"] / "state"
    r = subprocess.run(["python3", str(ROOT / "deploy/control-plane/deploy-state.py"), "prepare",
                        str(state), str(env["central"]), env["ref"], "f" * 40,
                        "herdr-engineering:" + "0" * 12, "1"],
                       env=env["env"], capture_output=True, text=True)
    assert r.returncode == 1
    assert "deployment recovery state operation failed" in r.stderr
    assert "docker inspect --type container herdr-cp-web" in env["log"].read_text()


def test_candidate_without_lock_held_support_warns_and_leaves_refresh_to_the_timer(env):
    added = _git("log", "--format=%H", "-S", "--lock-held", "--",
                 "deploy/control-plane/collab-sync.sh").splitlines()[-1]
    old = _git("rev-parse", f"{added}^")
    _with_collab(env)
    r = run(DEPLOY, env, "--no-fetch", "--ref", old, "--fleet-config", str(env["central"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot run under the deploy's lock; its refresh is left to the timer" in r.stderr
    assert "another collab-sync is running" in r.stderr     # the old script saw the held lock
    assert _lock_is_free(env)


# ---------------------------------------------------------------- deployment definitions mount

def _pipes_dir(e):
    return e["home"] / ".local/share/herdr-engineering/pipelines"


def _log_compose_env(e):
    """Wrap the docker stub so each call also logs HERDR_CP_PIPELINES_DIR (the stub is shared)."""
    stub = e["tmp"] / "bin" / "docker"
    real = e["tmp"] / "bin" / "docker-real"
    stub.rename(real)
    stub.write_text(f'#!/usr/bin/env bash\n[ "$1" != compose ] || echo "pipelinesdir=${{HERDR_CP_PIPELINES_DIR:-}}" >> {e["log"]}\n'
                    f'exec {real} "$@"\n')
    stub.chmod(0o755)


def test_compose_mounts_the_pipelines_dir_read_only_into_web_only():
    doc = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())
    mount = "${HERDR_CP_PIPELINES_DIR:?run deploy.sh}:/etc/herdr/pipelines:ro"
    assert mount in doc["services"]["control-plane-web"]["volumes"]
    for name, svc in doc["services"].items():
        if name != "control-plane-web":
            assert not any("pipelines" in str(v) for v in svc.get("volumes") or []), name


def test_dry_run_reports_the_pipelines_mount_and_creates_nothing(env):
    _log_compose_env(env)
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "/etc/herdr/pipelines" in r.stdout and "a real deploy creates it empty, 0755" in r.stdout
    assert not _pipes_dir(env).exists()
    assert not (env["home"] / ".config/systemd/user/herdr-pipelines-sync.timer").exists()
    assert f"pipelinesdir={_pipes_dir(env)}" in env["log"].read_text()     # compose config -q sees it


def test_deploy_creates_the_pipelines_dir_mounts_it_and_installs_the_sync_timer(env):
    _log_compose_env(env)
    # a full successful first deploy (health + settle checks): slower than the failing ones
    r = run(DEPLOY, env, "--no-fetch", "--ref", env["ref"], "--fleet-config", str(env["central"]), timeout=300)
    assert r.returncode == 0, r.stderr
    d = _pipes_dir(env)
    assert d.is_dir() and (d.stat().st_mode & 0o777) == 0o755 and not list(d.iterdir())
    log = env["log"].read_text()
    lines = [ln for ln in log.splitlines() if ln.startswith("pipelinesdir=")]
    assert lines and all(ln == f"pipelinesdir={d}" for ln in lines)
    assert "systemctl --user enable --now herdr-pipelines-sync.timer" in log
    share = env["home"] / ".local/share/herdr-engineering"
    assert (share / "pipelines-sync.sh").exists()
    assert (share / "pipelines-validator/deployment_definitions.py").exists()
    service = (env["home"] / ".config/systemd/user/herdr-pipelines-sync.service").read_text()
    assert f'--dir "{d}"' in service


def test_a_failed_deploy_removes_newly_installed_pipelines_units(env):
    env["env"]["FAIL_COMPOSE"] = "1"
    r = deploy(env)
    assert r.returncode == 1, r.stderr
    units = env["home"] / ".config/systemd/user"
    assert not (units / "herdr-pipelines-sync.service").exists()
    assert not (units / "herdr-pipelines-sync.timer").exists()
    assert not (env["home"] / ".local/share/herdr-engineering/pipelines-sync.sh").exists()


def test_a_valid_pipelines_dir_is_accepted(env):
    d = _pipes_dir(env)
    d.mkdir(parents=True, mode=0o755)
    d.chmod(0o755)
    (d / "hive-gateway.yaml").write_text("component: hive-gateway\n")
    (d / "hive-gateway.yaml").chmod(0o644)
    r = deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "a real deploy creates it empty, 0755" not in r.stdout


@pytest.mark.parametrize("fault,needle", [
    ("dir_symlink", "must be a real directory"),
    ("dir_mode", "must not be writable by group or other"),
    ("non_yaml", "must hold only regular *.yaml"),
    ("file_symlink", "must hold only regular *.yaml"),
    ("subdir", "must hold only regular *.yaml"),
    ("big_file", "must hold only regular *.yaml"),
    ("file_mode", "must hold only regular *.yaml"),
    ("too_many", "at most 256"),
])
def test_a_bad_pipelines_dir_is_refused_before_anything_runs(env, fault, needle):
    d = _pipes_dir(env)
    if fault == "dir_symlink":
        real = env["tmp"] / "elsewhere"
        real.mkdir()
        d.parent.mkdir(parents=True)
        d.symlink_to(real)
    else:
        d.mkdir(parents=True)
        d.chmod(0o755)
        if fault == "dir_mode":
            d.chmod(0o775)
        elif fault == "non_yaml":
            (d / "validate.py").write_text("x")
        elif fault == "file_symlink":
            target = env["tmp"] / "x.yaml"
            target.write_text("x")
            (d / "x.yaml").symlink_to(target)
        elif fault == "subdir":
            (d / "nested.yaml").mkdir()
        elif fault == "big_file":
            (d / "big.yaml").write_text("x" * (64 * 1024 + 1))
        elif fault == "file_mode":
            (d / "x.yaml").write_text("x")
            (d / "x.yaml").chmod(0o664)
        elif fault == "too_many":
            for i in range(257):
                (d / f"c{i}.yaml").write_text("x")
    r = deploy(env, "--dry-run")
    assert r.returncode != 0
    assert needle in r.stderr
    assert not env["log"].exists()
