"""Exercise real coordinator callbacks and their ordering, with no production/root commands."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DEPLOY = (ROOT / "deploy/control-plane/deploy.sh").read_text()


def function(name):
    return DEPLOY[DEPLOY.index(name + "() {") :].split("\n}\n", 1)[0] + "\n}\n"


@pytest.mark.parametrize(
    ("direction", "interrupted"), [("forward", False), ("rollback", False), ("rollback", True)]
)
@pytest.mark.parametrize("delete_fails", [False, True])
def test_real_coordinator_restores_matching_policy_and_profile_before_grant(
    tmp_path, direction, delete_fails, interrupted
):
    log = tmp_path / "events"
    state = tmp_path / "state.py"
    state.write_text("")
    previous = tmp_path / "env/rollback-worktree/herdr_engineering"
    previous.mkdir(parents=True)
    (previous / "memory_client_profile.py").write_text("")
    (previous / "cli.py").write_text("")
    (previous / "__init__.py").write_text("")
    current = tmp_path / "work/herdr_engineering"
    current.mkdir(parents=True)
    (current / "memory_client_profile.py").write_text("")
    old = tmp_path / "old-profile.tar"
    old.write_text("old fixture")
    new = tmp_path / "new-profile.tar"
    new.write_text("new fixture")
    if interrupted:
        invocation = (
            "HOST_MUTATED=1; DEPLOY_FINISHED=0\ncleanup() { return 0; }\n"
            + function("finish")
            + "\ntrap finish EXIT\ntrap 'exit 143' TERM\nkill -TERM $$\n"
        )
    elif direction == "forward":
        invocation = DEPLOY[DEPLOY.index("if forward; then") :]
    else:
        invocation = f'{direction} || status=$?\nexit "$status"\n'
    script = f'''
set -euo pipefail
WORK="{current.parent}"; PREV_WORK="{previous.parent}"; ENV_DIR="{tmp_path}/env"
ATTEMPT="{tmp_path}/attempt"; STATE_DIR="{tmp_path}"; STATE_HELPER="{state}"
REPO="$WORK"; HERE="{ROOT}/deploy/control-plane"; DEPLOY_LOCK_FD=7
SHA=new; TAG=new; REF=candidate; MEMORY=0; PREV_COMMIT=old; PREV_SHA=old; PREV_TAG=old; PREVIOUS_IMAGE=old
COLLAB=0; SHARE=0; NEW_SHARE=""; COLLAB_HASH=""; PREV_COLLAB_HASH=""
ENV_FILE=unused; FLEET_DIR=unused; LLM_DIR=unused; NEW_LLM=unused
HOST_MUTATED=0; DEPLOY_FINISHED=0; WEB_EGRESS_MANAGED=1
WEB_EGRESS_CURRENT="{tmp_path}/current.json"
WEB_EGRESS_COMPOSE_ENABLED=0; MEMORY_PRIVATE_EGRESS_CIDR=unused; WEB_EGRESS_GENERATION=unused
HERDR_SHARES_DIR="{tmp_path}/shares"; HERDR_SHARE_EVENTS_DIR="{tmp_path}/share-events"
HERDR_COLLAB_MIRROR_DIR="{tmp_path}/mirror"; HERDR_COLLAB_OUTBOX_DIR="{tmp_path}/outbox"; HERDR_COLLAB_SPOOL_DIR="{tmp_path}/spool"
PIPELINES_SYNC=missing; SYNC_STABLE=missing; APP_SERVICES=(control-plane-web); EDGE_SERVICE=(); SHARE_SERVICE=()
MEMORY_CLIENT_SUPPORTED=1; MEMORY_CLIENT_MODE=install
MEMORY_CLIENT_PREVIOUS="{old}"; MEMORY_CLIENT_CANDIDATE="{new}"; MEMORY_CLIENT_CURRENT="{tmp_path}/promoted-profile"
. "{ROOT}/deploy/control-plane/web-egress-deploy.sh"
. "{ROOT}/deploy/control-plane/memory-client-deploy.sh"
web_egress_cmd() {{
  local action="$1"; shift
  if [ "$action" = selector ]; then
    printf '%s\\n' '{{"enabled":true,"cidr":"172.31.252.0/29","generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
  else
    echo "egress:$action $*" >> "{log}"
    if [ "$action" = before ] && [ "{int(delete_fails)}" = 1 ]; then return 1; fi
  fi
}}
memory_client_cmd() {{ echo "profile:$*" >> "{log}"; }}
container_of() {{ echo private-web; }}
install_private() {{ echo "host-install:$1" >> "{log}"; }}
restore_host_state() {{ echo restore-host-state >> "{log}"; }}
deploy_lock_phase() {{ echo "lock:$1" >> "{log}"; }}
compose_up() {{
  echo "compose:$3 selector=$WEB_EGRESS_COMPOSE_ENABLED cidr=$MEMORY_PRIVATE_EGRESS_CIDR" >> "{log}"
  [ "$WEB_EGRESS_COMPOSE_ENABLED" = 1 ]
}}
rollback_share() {{ return 0; }}
verify() {{ echo "verify:$1" >> "{log}"; }}
git() {{ return 0; }}
{function("forward")}
{function("rollback")}
status=0
{invocation}
'''
    result = subprocess.run(
        ["bash", "-c", script], env=os.environ.copy(), text=True, capture_output=True, timeout=15
    )
    events = log.read_text().splitlines()
    if delete_fails:
        assert result.returncode != 0
        assert not any(
            line.startswith(("host-install:", "compose:", "profile:"))
            or line == "restore-host-state"
            for line in events
        )
        return
    assert result.returncode == (143 if interrupted else (0 if direction == "forward" else 1)), (
        result.stderr
    )
    before = next(i for i, line in enumerate(events) if line.startswith("egress:before"))
    compose = next(i for i, line in enumerate(events) if line.startswith("compose:"))
    profile = next(
        i for i, line in enumerate(events) if line.startswith("profile:") and " install " in line
    )
    grant = next(i for i, line in enumerate(events) if line.startswith("egress:after"))
    assert before < compose < profile < grant
    assert str(old if direction == "rollback" else new) in events[profile]
    assert "selector=1 cidr=172.31.252.0/29" in events[compose]
    commit = next(i for i, line in enumerate(events) if line.startswith("egress:commit"))
    assert grant < commit
