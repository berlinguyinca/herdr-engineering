"""deploy/control-plane/memory-deploy.sh (the `deploy.sh --with-memory` step) exercised with stub `docker`, `uname`,
`curl` and a throwaway git checkout standing in for berlinguyinca/herdr-memory. Nothing real is built, started or
contacted. deploy.sh itself cannot run on a macOS bash 3.2 (pre-existing: flock, find -printf), so the memory logic
lives in a sourced file that these tests drive directly; the wiring in deploy.sh is checked statically at the end."""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
LIB = CP / "memory-deploy.sh"
DEPLOY = CP / "deploy.sh"
TAG = "tagfortest1"
SECRETS = ["DBPASSWORD" + "x" * 30, "PROJECTKEY" + "p" * 30, "GLOBALKEY" + "g" * 30,
           "INFRAKEY" + "i" * 30, "PERSONALKEY" + "q" * 30]
GNU = "/opt/homebrew/opt/coreutils/libexec/gnubin"

HARNESS = r'''
set -euo pipefail
S="$STATE"
HEALTH_TRIES=2; HEALTH_INTERVAL=0
DRY="${DRY:-0}"; CENTRAL=/central/fleet-config.yaml; SHA=abc123abc123; TAG=cptag
ENV_DIR="$S/envdir"; mkdir -p "$ENV_DIR"; ENV_FILE="$S/compose.env"
WORK="$S/work"; ATTEMPT="$S/attempt"
MEMORY_CHECKOUT="$CHECKOUT"
HOME="$S/home"; mkdir -p "$HOME"
MEM_STATE_DIR="$HOME/.local/state/herdr-engineering/memory"
herdr_eng() { echo "herdr_eng $*" >> "$S/log"; return 1; }   # no previous-config re-render in these tests
# compose_up_retry <worktree> <env file> <tag> <sha> <compose args...>: records, emulates container state
compose_up_retry() {
  local envf="$2"; shift 4
  echo "compose env=$envf $*" >> "$S/log"
  case " $* " in *" --profile memory "*) ;; *) echo "NOT A MEMORY CALL: $*" >> "$S/log" ;; esac
  local svc cname
  for svc in "$@"; do
    case "$svc" in hindsight-db) cname=herdr-hindsight-db ;; hindsight-api) cname=herdr-hindsight ;; *) continue ;; esac
    case " ${FAIL_UP:-} " in *" $svc "*) return 1 ;; esac
    if [ "$svc" = hindsight-api ]; then cp "$S/tag_id" "$S/img_$cname" 2>/dev/null || true; fi
    case " $* " in *" --force-recreate "*) [ -z "${HEAL:-}" ] || rm -f "$S/set_$cname" ;; esac
    if [ -f "$S/set_$cname" ]; then cp "$S/set_$cname" "$S/state_$cname"; else echo "running false 0 healthy" > "$S/state_$cname"; fi
  done
  return 0
}
. "$LIB"
'''


def _script(body: str) -> str:
    return HARNESS + body


@pytest.fixture
def mem(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = state / "log"
    log.write_text("")
    (state / "tag_id").write_text("sha256:" + "n" * 64)

    def stub(name, text):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)

    stub("uname", '#!/bin/bash\n[ "${1:-}" = -m ] && echo "${STUB_ARCH:-x86_64}" || echo Linux\n')
    stub("curl", f'#!/bin/bash\necho "curl $*" >> {log}\n[ -z "${{STUB_NO_HEALTH:-}}" ]\n')
    stub("docker", f'''#!/bin/bash
S={state}
echo "docker $*" >> $S/log
last="${{@: -1}}"
case "$1" in
  inspect)
    case "$*" in
      *"{{{{.Image}}}}"*) [ -f "$S/img_$last" ] && cat "$S/img_$last" || exit 1 ;;
      *"State.Status"*) [ -f "$S/state_$last" ] && cat "$S/state_$last" || echo "missing false 0 none" ;;
      *"State.Health.Status"*) [ -f "$S/state_$last" ] && awk '{{print $4}}' "$S/state_$last" || true ;;
    esac ;;
  image) cat "$S/tag_id" ;;
  tag) echo "$2" > "$S/tag_id" ;;
  stop) shift; for n in "$@"; do echo "exited false 0 none" > "$S/state_$n"; done ;;
esac
exit 0
''')
    # a throwaway "herdr-memory" repo: the pin is the FIRST commit; HEAD is a later one that must NOT be built
    ck = tmp_path / "herdr-memory"
    (ck / "deploy").mkdir(parents=True)

    def git(*a):
        subprocess.run(["git", "-C", str(ck), *a], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.test",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.test"))

    git("init", "-q")
    build_log = tmp_path / "build.log"
    (ck / "deploy" / "build.sh").write_text(
        f'#!/usr/bin/env bash\necho "pinned-build image=$HERDR_MEMORY_IMAGE cwd=$(basename "$PWD")" >> {build_log}\n'
        f'[ -z "${{FAIL_BUILD:-}}" ] || {{ echo "boom: build failed"; exit 1; }}\n')
    git("add", "-A")
    git("commit", "-qm", "pin")
    pin = subprocess.run(["git", "-C", str(ck), "rev-parse", "--short=7", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    (ck / "deploy" / "build.sh").write_text(f'#!/usr/bin/env bash\necho "WRONG: built from HEAD" >> {build_log}\n')
    git("commit", "-qam", "later")
    # the "deployed commit" tree: lock file + backup script stub
    work = state / "work"
    (work / "lock").mkdir(parents=True)
    (work / "deploy" / "control-plane").mkdir(parents=True)
    (work / "lock" / "upstreams.yaml").write_text(yaml.safe_dump(
        {"dependencies": {"herdr-memory-deploy": {"revision": pin, "checkout": str(ck)}}}))
    (work / "deploy" / "control-plane" / "memory-backup.sh").write_text(f'#!/bin/bash\necho "backup-install $*" >> {log}\n')
    (work / "deploy" / "control-plane" / "memory-backup.sh").chmod(0o755)
    model = tmp_path / "model"
    (model / "onnx").mkdir(parents=True)
    (model / "onnx" / "model.onnx").write_text("x")
    env_file = state / "compose.env"
    lines = {"HERDR_MEMORY_DB_PASSWORD": SECRETS[0],
             "HERDR_MEMORY_TENANT_USERS": ",".join(f"{t}:{k}" for t, k in zip(
                 ("project", "global", "infra", "personal"), SECRETS[1:])),
             "HERDR_MEMORY_IMAGE_TAG": TAG, "HERDR_MEMORY_MODEL_DIR": str(model), "HERDR_MEMORY_PORT": "8888"}
    env_file.write_text("".join(f"{k}='{v}'\n" for k, v in lines.items()))
    env_file.chmod(0o600)
    env = dict(os.environ, PATH=f"{bindir}:{GNU}:{os.environ['PATH']}", STATE=str(state), CHECKOUT=str(ck),
               LIB=str(LIB), HOME=str(tmp_path / "realhome"))

    class M:
        pass
    m = M()
    m.state, m.log, m.env, m.env_file, m.pin, m.ck, m.build_log, m.model = state, log, env, env_file, pin, ck, build_log, model
    m.lines = lines
    m.marker = state / "home/.local/state/herdr-engineering/memory/deployed.json"

    def run(body, **extra):
        e = dict(env, **extra)
        r = subprocess.run(["bash", "-c", _script(body)], env=e, capture_output=True, text=True, timeout=60)
        r.log = log.read_text()
        r.all = r.stdout + r.stderr + r.log
        return r
    m.run = run
    m.write_env = lambda drop=(), **over: env_file.write_text("".join(
        f"{k}='{over.get(k, v)}'\n" for k, v in lines.items() if k not in drop))
    return m


def no_secrets(text):
    assert not any(s in text for s in SECRETS), "a secret value leaked"


def control_plane_untouched(log):
    # memory step only ever drives hindsight-* services through `--profile memory`
    assert "NOT A MEMORY CALL" not in log
    for svc in ("control-plane-web", "control-plane-worker", "llm-proxy", "postgres", "rustfs", "coredns", "fabric-gateway"):
        assert not re.search(rf"\b{svc}\b", log), svc
    for bad in ("volume rm", "compose down", " rm ", "prune"):
        assert bad not in log


# ---------------------------------------------------------------- preflight ---------------------------------------

def test_preflight_ok_reads_pin_and_checkout_from_the_lock_file(mem):
    r = mem.run('memory_preflight; echo "pin=$MEM_PIN tag=$MEM_TAG port=$MEM_PORT ck=$MEM_CK"')
    assert r.returncode == 0, r.stderr
    assert f"pin={mem.pin} tag={TAG} port=8888 ck={mem.ck}" in r.stdout
    no_secrets(r.all)


@pytest.mark.parametrize("arch", ["arm64", "aarch64"])
def test_preflight_refuses_arm_with_a_clear_message(mem, arch):
    r = mem.run("memory_preflight", STUB_ARCH=arch)
    assert r.returncode == 1
    assert f"this host is {arch}" in r.stderr and "linux/amd64" in r.stderr and "Nothing was changed" in r.stderr
    assert "docker" not in mem.log.read_text()


@pytest.mark.parametrize("arch", ["x86_64", "amd64"])
def test_preflight_accepts_amd64(mem, arch):
    assert mem.run("memory_preflight", STUB_ARCH=arch).returncode == 0


@pytest.mark.parametrize("var", ["HERDR_MEMORY_DB_PASSWORD", "HERDR_MEMORY_TENANT_USERS", "HERDR_MEMORY_IMAGE_TAG",
                                 "HERDR_MEMORY_MODEL_DIR", "HERDR_MEMORY_PORT"])
def test_preflight_refuses_each_missing_variable_by_name(mem, var):
    mem.write_env(drop=(var,))
    r = mem.run("memory_preflight")
    assert r.returncode == 1 and var in r.stderr and "Nothing was changed" in r.stderr
    no_secrets(r.all)


def test_preflight_refuses_an_empty_variable(mem):
    mem.write_env(HERDR_MEMORY_TENANT_USERS="")
    r = mem.run("memory_preflight")
    assert r.returncode == 1 and "HERDR_MEMORY_TENANT_USERS" in r.stderr


def test_preflight_refuses_a_checkout_without_the_pinned_revision(mem, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
    r = mem.run("memory_preflight", CHECKOUT=str(other))
    assert r.returncode == 1 and "not a herdr-memory git checkout holding the pinned revision" in r.stderr
    r2 = mem.run("memory_preflight", CHECKOUT=str(tmp_path / "does-not-exist"))
    assert r2.returncode == 1 and "pinned revision" in r2.stderr


def test_preflight_refuses_a_lock_without_the_pin(mem):
    (mem.state / "work" / "lock" / "upstreams.yaml").write_text("dependencies: {}\n")
    r = mem.run("memory_preflight")
    assert r.returncode == 1 and "no herdr-memory-deploy revision pin" in r.stderr


def test_missing_model_refuses_a_real_deploy_but_only_warns_in_dry_run(mem):
    (mem.model / "onnx" / "model.onnx").unlink()
    real = mem.run("memory_preflight")
    assert real.returncode == 1 and "model.onnx is missing" in real.stderr
    dry = mem.run("memory_preflight", DRY="1")
    assert dry.returncode == 0 and "WARNING" in dry.stderr and "model.onnx" in dry.stderr


def test_the_shipped_lock_file_carries_a_usable_pin():
    dep = yaml.safe_load((ROOT / "lock" / "upstreams.yaml").read_text())["dependencies"]["herdr-memory-deploy"]
    assert re.fullmatch(r"[0-9a-f]{7,40}", dep["revision"]) and dep["checkout"].startswith("~/")


# ---------------------------------------------------------------- happy path --------------------------------------

def test_first_deploy_builds_the_pinned_revision_and_starts_only_memory_services(mem):
    r = mem.run('rc=0; memory_preflight; memory_phase || rc=$?; echo "rc=$rc"')
    assert r.returncode == 0, r.all
    assert "rc=0" in r.stdout and "ok: memory running" in r.stdout
    build = mem.build_log.read_text()
    assert f"pinned-build image=herdr-memory-hindsight:{TAG}" in build and "WRONG" not in build
    assert "cwd=memory-src" in build                       # built from an export, not the user's checkout
    log = mem.log.read_text()
    # database first, alone; then the API; no force-recreate on a normal forward step
    assert log.index("--no-build hindsight-db") < log.index("--no-build hindsight-api")
    assert "--force-recreate" not in log
    assert "curl -fsS -m 5 http://127.0.0.1:8888/health" in log      # loopback health gate
    assert "backup-install" in log
    control_plane_untouched(log)
    no_secrets(r.all)


def test_deploy_marker_is_private_and_holds_no_secret(mem):
    r = mem.run("memory_preflight; memory_phase")
    assert r.returncode == 0, r.all
    data = json.loads(mem.marker.read_text())
    assert data["image_tag"] == TAG and data["port"] == 8888 and data["herdr_memory_revision"] == mem.pin
    assert stat.S_IMODE(mem.marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(mem.marker.parent.stat().st_mode) == 0o700
    no_secrets(mem.marker.read_text())


# ---------------------------------------------------------------- failures: first deploy --------------------------

@pytest.mark.parametrize("how", ["build", "api_up", "db_up", "unhealthy", "crashloop", "health_url"])
def test_failed_first_memory_deploy_stops_memory_only_and_says_so(mem, how):
    extra = {}
    if how == "build":
        extra["FAIL_BUILD"] = "1"
    elif how == "api_up":
        extra["FAIL_UP"] = "hindsight-api"
    elif how == "db_up":
        extra["FAIL_UP"] = "hindsight-db"
    elif how == "unhealthy":
        (mem.state / "set_herdr-hindsight").write_text("running false 0 unhealthy\n")
    elif how == "crashloop":
        (mem.state / "set_herdr-hindsight").write_text("restarting true 3 none\n")
    elif how == "health_url":
        extra["STUB_NO_HEALTH"] = "1"
    r = mem.run("memory_preflight; memory_phase", **extra)
    assert r.returncode == 1                                  # deploy.sh turns this into exit 4
    assert "memory step FAILED (the control plane is unaffected" in r.stderr
    assert "first deploy" in r.stderr and "no previous memory version" in r.stderr
    assert "docker stop herdr-hindsight herdr-hindsight-db" in mem.log.read_text()
    assert not mem.marker.exists()
    if how == "build":
        assert "boom: build failed" in r.stderr
    control_plane_untouched(mem.log.read_text())
    no_secrets(r.all)


# ---------------------------------------------------------------- failures: update with a previous version ---------

def seed_previous(mem):
    """A memory deploy already running on an older image, with an older marker."""
    (mem.state / "img_herdr-hindsight").write_text("sha256:" + "o" * 64)
    (mem.state / "state_herdr-hindsight").write_text("running false 0 healthy\n")
    (mem.state / "state_herdr-hindsight-db").write_text("running false 0 healthy\n")
    mem.marker.parent.mkdir(parents=True, mode=0o700)
    mem.marker.write_text(json.dumps({"image_tag": "old"}))


def test_failed_update_rolls_the_api_back_to_the_previous_image(mem):
    seed_previous(mem)
    # the new api is unhealthy once; the recreated old one (force-recreate) is healthy again
    (mem.state / "set_herdr-hindsight").write_text("running false 0 unhealthy\n")
    r = mem.run("memory_preflight; rc=0; memory_phase || rc=$?; echo rc=$rc", HEAL="1")
    assert "rc=1" in r.stdout, r.all
    log = mem.log.read_text()
    assert f"docker tag sha256:{'o' * 64} herdr-memory-hindsight:{TAG}" in log
    assert "--force-recreate hindsight-api" in log
    assert "memory rolled back to the previous image and healthy" in r.stderr
    assert "first deploy" not in r.stderr
    assert json.loads(mem.marker.read_text()) == {"image_tag": "old"}      # old marker intact
    assert "docker stop" not in log                                        # the old version keeps running
    control_plane_untouched(log)
    no_secrets(r.all)


def test_failed_update_whose_rollback_also_fails_says_so(mem):
    seed_previous(mem)
    r = mem.run("memory_preflight; FAIL_UP=hindsight-api rc=0; memory_phase || rc=$?; echo rc=$rc")
    assert "rc=1" in r.stdout
    assert "FAILED TO ROLL BACK memory" in r.stderr and "control plane is unaffected" in r.stderr
    assert "rolled back to the previous image and healthy" not in r.stderr
    assert json.loads(mem.marker.read_text()) == {"image_tag": "old"}
    control_plane_untouched(mem.log.read_text())


def test_rollback_prefers_the_previous_config_snapshot_when_it_renders_memory(mem):
    seed_previous(mem)
    prev = mem.state / "attempt" / "previous"
    prev.mkdir(parents=True)
    (prev / "fleet-config.yaml").write_text("x: 1\n")
    # herdr_eng "renders" an env file that carries the memory vars, like the previous config would
    r = mem.run('''memory_preflight
herdr_eng() { local out; while [ $# -gt 0 ]; do [ "$1" = --out ] && out="$2"; shift; done
  echo "HERDR_MEMORY_TENANT_USERS='project:OLD'" > "$out"; }
FAIL_UP=hindsight-api
rc=0; memory_phase || rc=$?; echo rc=$rc''')
    assert "memory-prev.env" in mem.log.read_text()
    assert "rc=1" in r.stdout
    no_secrets(r.all)


# ---------------------------------------------------------------- wiring in deploy.sh (static) --------------------

def test_deploy_sh_wiring_is_opt_in_and_after_the_control_plane_receipt():
    text = DEPLOY.read_text()
    assert "--with-memory) MEMORY=1" in text and "MEMORY=0" in text
    assert text.index("python3 \"$STATE_HELPER\" outcome \"$ATTEMPT\" succeeded") < text.index("memory_phase || exit 4")
    assert "memory_preflight || exit 1" in text
    # the control plane's own compose up never names a memory service; the memory step always passes the profile
    assert "hindsight" not in text.split("memory_phase || exit 4")[0].split("compose_up()")[1].split("healthy_commit")[0]
    lib = LIB.read_text()
    for ln in lib.splitlines():
        if "compose_up_retry" in ln and "up -d" in ln:
            assert "--profile memory" in ln and "hindsight-api" in ln or "mem_up" in ln


def test_memory_services_are_never_in_the_control_plane_service_lists():
    text = DEPLOY.read_text()
    m = re.search(r"APP_SERVICES=\(([^)]*)\)", text)
    assert "hindsight" not in m.group(1)


def test_memory_lib_is_executable_syntax_clean_and_has_no_inline_secrets():
    r = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.returncode == 0 and not re.search(r"HINDSIGHT_API_TENANT_USERS=\S", LIB.read_text())
