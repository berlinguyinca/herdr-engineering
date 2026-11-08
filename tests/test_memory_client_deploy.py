"""Exercise profile lifecycle through the existing deploy coordinator's callbacks."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'deploy/control-plane/memory-client-deploy.sh'


@pytest.fixture
def harness(tmp_path):
    work = tmp_path / 'work'
    (work / 'herdr_engineering').mkdir(parents=True)
    (work / 'herdr_engineering/memory_client_profile.py').write_text('# capability marker')
    scripts = work / 'deploy/control-plane'
    scripts.mkdir(parents=True)
    log = tmp_path / 'calls.jsonl'
    builder = scripts / 'prepare-memory-client.sh'
    builder.write_text(f'#!/bin/bash\nprintf "builder %s\\n" "$*" >> "{log}"\necho herdr-memory-client:{"a" * 40}\n')
    builder.chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    python = bin_dir / 'python3'
    python.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] != ['-m', 'herdr_engineering.memory_client_profile']:
    os.execv({sys.executable!r}, [{sys.executable!r}, *args])
action = args[2]
with open({str(log)!r}, 'a') as f:
    f.write(json.dumps({{'action': action, 'args': args[3:]}}) + '\\n')
if action == 'policy-info':
    print(json.dumps({{'enabled': os.environ.get('STUB_CLIENT_MODE', 'clear') == 'install',
                      'mode': os.environ.get('STUB_CLIENT_MODE', 'clear')}}))
elif action == 'prepare':
    if os.environ.get('STUB_CLIENT_PREPARE_FAIL'):
        sys.exit(1)
    out = pathlib.Path(args[args.index('--out') + 1])
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.write_bytes(b'NEW-PRIVATE-PROFILE')
    out.chmod(0o600)
    print('{{"prepared":true}}')
elif action in ('install', 'check', 'verify'):
    if action == 'install' and os.environ.get('STUB_CLIENT_INSTALL_FAIL'):
        sys.exit(1)
else:
    sys.exit(2)
''')
    python.chmod(0o755)
    script = f'''
set -euo pipefail
WORK="{work}"; ENV_DIR="{tmp_path}/env"; STATE_DIR="{tmp_path}/state"; ATTEMPT="{tmp_path}/attempt"
CENTRAL="{tmp_path}/fleet-config.yaml"; MEMORY_CLIENT_CONFIG=""; MEMORY_CHECKOUT=""
mkdir -p "$ENV_DIR" "$STATE_DIR" "$ATTEMPT"; chmod 700 "$ENV_DIR" "$STATE_DIR" "$ATTEMPT"
BACKED_UP=()
backup_file() {{
  BACKED_UP+=("$1")
  if [ -f "$1" ]; then cp -p "$1" "{tmp_path}/previous-backup"; fi
}}
install_private() {{
  mkdir -p "$(dirname "$2")"; chmod 700 "$(dirname "$2")"
  install -m 600 "$1" "$2"
}}
container_of() {{ echo private-web-container; }}
. "{LIB}"
'''
    return {'root': tmp_path, 'work': work, 'log': log, 'script': script,
            'env': dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])}


def run(harness, body, **environment):
    return subprocess.run(['bash', '-c', harness['script'] + body],
                          env=dict(harness['env'], **environment), capture_output=True,
                          text=True, timeout=20)


def events(harness):
    if not harness['log'].exists():
        return []
    return [json.loads(line) for line in harness['log'].read_text().splitlines() if line.startswith('{')]


def test_disabled_defaults_need_no_cli_image_or_source_profile(harness):
    result = run(harness, '''
memory_client_preflight
memory_client_build
memory_client_snapshot
memory_client_apply forward "$WORK"
memory_client_promote
''')
    assert result.returncode == 0, result.stderr
    calls = events(harness)
    assert all(c['action'] not in ('prepare', 'verify') for c in calls)
    assert not any('builder' in line for line in harness['log'].read_text().splitlines())
    assert not list((harness['root'] / 'attempt').rglob('*.tar'))


def test_preflight_dry_path_only_plans_builder_and_stages_private_artifact(harness):
    result = run(harness, '''
memory_client_preflight
[ "$MEMORY_CLIENT_ENABLED" = 1 ]
[ -f "$MEMORY_CLIENT_STAGED" ]
''', STUB_CLIENT_MODE='install')
    assert result.returncode == 0, result.stderr
    assert harness['log'].read_text().count('builder --dry-run') == 1
    assert [e['action'] for e in events(harness)] == ['policy-info', 'prepare']
    assert 'NEW-PRIVATE-PROFILE' not in result.stdout + result.stderr + harness['log'].read_text()


def test_forward_and_rollback_use_distinct_profile_snapshots(harness):
    current = harness['root'] / 'state/memory-client/profile.tar'
    current.parent.mkdir(parents=True, mode=0o700)
    current.write_bytes(b'OLD-PRIVATE-PROFILE')
    current.chmod(0o600)
    result = run(harness, '''
memory_client_preflight
memory_client_build
memory_client_snapshot
memory_client_apply forward "$WORK"
memory_client_promote
memory_client_apply rollback "$WORK"
''', STUB_CLIENT_MODE='install')
    assert result.returncode == 0, result.stderr
    installs = [e for e in events(harness) if e['action'] == 'install']
    assert len(installs) == 2
    forward = Path(installs[0]['args'][installs[0]['args'].index('--archive') + 1])
    previous = Path(installs[1]['args'][installs[1]['args'].index('--archive') + 1])
    assert forward != previous
    assert forward.read_bytes() == b'NEW-PRIVATE-PROFILE'
    assert previous.read_bytes() == b'OLD-PRIVATE-PROFILE'
    assert current.read_bytes() == b'NEW-PRIVATE-PROFILE'
    # Never put secrets alongside the fleet file's mounted directory.
    assert '/candidate-memory-client/' in str(forward)
    assert '/previous-memory-client/' in str(previous)


def test_first_activation_rollback_clears_instead_of_reusing_forward_credentials(harness):
    result = run(harness, '''
memory_client_preflight
memory_client_snapshot
memory_client_apply forward "$WORK"
memory_client_apply rollback "$WORK"
''', STUB_CLIENT_MODE='install')
    assert result.returncode == 0, result.stderr
    installs = [e for e in events(harness) if e['action'] == 'install']
    assert '--archive' in installs[0]['args'] and '--archive' not in installs[1]['args']


def test_profile_install_failure_is_a_deploy_failure_without_promotion(harness):
    result = run(harness, '''
memory_client_preflight
memory_client_snapshot
memory_client_apply forward "$WORK"
memory_client_promote
''', STUB_CLIENT_MODE='install', STUB_CLIENT_INSTALL_FAIL='1')
    assert result.returncode != 0
    assert any(e['action'] == 'install' for e in events(harness))
    assert not (harness['root'] / 'state/memory-client/profile.tar').exists()
    assert 'NEW-PRIVATE-PROFILE' not in result.stdout + result.stderr


def test_rollback_to_pre_feature_image_never_receives_new_profile(harness):
    old = harness['root'] / 'old-commit'
    old.mkdir()
    result = run(harness, f'''
memory_client_preflight
memory_client_snapshot
memory_client_apply rollback "{old}"
''', STUB_CLIENT_MODE='install')
    assert result.returncode == 0, result.stderr
    assert not [e for e in events(harness) if e['action'] == 'install']


def test_preparation_failure_stops_before_build_or_profile_install(harness):
    result = run(harness, '''
memory_client_preflight
memory_client_build
''', STUB_CLIENT_MODE='install', STUB_CLIENT_PREPARE_FAIL='1')
    assert result.returncode != 0
    assert [e['action'] for e in events(harness)] == ['policy-info', 'prepare']


@pytest.mark.parametrize('interrupted', [False, True], ids=['failed-forward', 'signal-exit'])
def test_coordinator_rollback_restores_previous_profile_before_verification(harness, interrupted):
    """Exercise the coordinator's real rollback/EXIT functions with isolated host callbacks."""
    source = (ROOT / 'deploy/control-plane/deploy.sh').read_text()
    rollback = source[source.index('rollback() {'):].split('\n}\n', 1)[0] + '\n}\n'
    resume = source[source.index('resume_worker() {'):].split('\n}\n', 1)[0] + '\n}\n'
    finish = source[source.index('finish() {'):].split('\n}\n', 1)[0] + '\n}\n'
    previous = harness['root'] / 'env/rollback-worktree/herdr_engineering'
    previous.mkdir(parents=True)
    (previous / 'memory_client_profile.py').write_text('# previous runtime capability')
    (previous / '__init__.py').write_text('')
    (previous / 'cli.py').write_text('# isolated previous render callback')
    state = harness['root'] / 'state-helper.py'
    state.write_text('# isolated state receipt callback')
    current = harness['root'] / 'state/memory-client/profile.tar'
    current.parent.mkdir(parents=True, mode=0o700)
    current.write_bytes(b'OLD-PRIVATE-PROFILE')
    current.chmod(0o600)
    body = f'''
memory_client_preflight
memory_client_snapshot
PREV_COMMIT=old; PREV_SHA=old; PREV_TAG=old; PREVIOUS_IMAGE=old
SHA=new; REPO="$WORK"; STATE_HELPER="{state}"; COLLAB=0
APP_SERVICES=(control-plane-web); EDGE_SERVICE=(); SHARE_SERVICE=()
HOST_MUTATED=1; DEPLOY_FINISHED=0; WORKER_PAUSED=0
DEPLOY_LOCK_PHASES=()
deploy_lock_phase() {{ DEPLOY_LOCK_PHASES+=("$1"); }}
restore_host_state() {{ return 0; }}
git() {{ return 0; }}
compose_up() {{ return 0; }}
rollback_share() {{ return 0; }}
rollback_services() {{ return 0; }}
verify() {{ printf '%s\\n' '{{"action":"verify-rollback","args":[]}}' >> "{harness['log']}"; }}
cleanup() {{ return 0; }}
{rollback}
{resume}
{finish}
'''
    if interrupted:
        body += 'trap finish EXIT\ntrap \'exit 143\' TERM\nkill -TERM $$\n'
    else:
        body += 'status=0; rollback || status=$?; exit "$status"\n'
    result = run(harness, body, STUB_CLIENT_MODE='install')
    assert result.returncode == (143 if interrupted else 1), result.stderr
    calls = events(harness)
    installs = [e for e in calls if e['action'] == 'install']
    assert len(installs) == 1
    archive = Path(installs[0]['args'][installs[0]['args'].index('--archive') + 1])
    assert archive.read_bytes() == b'OLD-PRIVATE-PROFILE'
    assert calls.index(installs[0]) < next(i for i, e in enumerate(calls)
                                         if e['action'] == 'verify-rollback')


@pytest.mark.parametrize(('opt_in','enabled','supported','mode'),[
    (0,1,1,'install'),(1,0,1,'install'),(1,1,0,'install'),(1,1,1,'unmanaged'),(1,0,1,'clear'),
])
def test_forward_connectivity_noop_without_optin_managed_sdk(harness,opt_in,enabled,supported,mode):
    result=run(harness,f'''
MEMORY_PRIVATE_EGRESS_CHANGED=1; MEMORY_PRIVATE_EGRESS={opt_in}; MEMORY_CLIENT_ENABLED={enabled}
MEMORY_CLIENT_SUPPORTED={supported}; MEMORY_CLIENT_MODE={mode}
memory_client_forward_connectivity "$WORK"
''')
    assert result.returncode==0,result.stderr
    assert not harness['log'].exists()


def test_ordinary_redeploy_preserves_approved_policy_without_remote_health_gate(harness):
    from herdr_engineering.web_egress_runtime import make_record, private_json
    approved=make_record('172.30.254.0/29','100.71.1.7/32','e'*32)
    approved.update(network_id='c'*64,rule_may_exist=True)
    current=harness['root']/'state/web-egress/current.json'
    private_json(current,approved)
    before=current.read_bytes()
    body=f'''
REPO="{ROOT}"; HERE="{ROOT}/deploy/control-plane"; PROJECT=herdr-control-plane
MEMORY_PRIVATE_EGRESS=auto; MEMORY_PRIVATE_EGRESS_CIDR=172.31.252.0/29; MEMORY_PRIVATE_EGRESS_GATEWAY=""
. "{ROOT}/deploy/control-plane/web-egress-deploy.sh"
web_egress_cmd() {{
  if [ "$1" = approved ]; then
    (cd "$REPO" && PYTHONPATH="$REPO" python3 -m herdr_engineering.web_egress_runtime "$@")
  elif [ "$1" = preview ]; then
    echo "preview:$*" >> "{harness['log']}"
    echo '{{"enabled":true,"managed":true}}'
  else return 99; fi
}}
web_egress_preflight
[ "$MEMORY_PRIVATE_EGRESS" = 1 ] && [ "$WEB_EGRESS_COMPOSE_ENABLED" = 1 ]
[ "$MEMORY_PRIVATE_EGRESS_CIDR" = 172.30.254.0/29 ]
[ "$MEMORY_PRIVATE_EGRESS_GATEWAY" = 100.71.1.7/32 ]
[ "$WEB_EGRESS_GENERATION" = {'e'*32} ]
MEMORY_CLIENT_ENABLED=1; MEMORY_CLIENT_SUPPORTED=1; MEMORY_CLIENT_MODE=install
memory_client_forward_connectivity "$WORK"
'''
    result=run(harness,body)
    assert result.returncode==0,result.stderr
    assert current.read_bytes()==before
    assert events(harness)==[]
    assert '--enabled --cidr 172.30.254.0/29 --gateway-ip 100.71.1.7/32' in harness['log'].read_text()


def test_auto_without_any_owned_state_never_inspects_or_calls_root(harness):
    body=f'''
REPO="{ROOT}"; HERE="{ROOT}/deploy/control-plane"; PROJECT=herdr-control-plane
MEMORY_PRIVATE_EGRESS=auto; MEMORY_PRIVATE_EGRESS_CIDR=172.31.252.0/29; MEMORY_PRIVATE_EGRESS_GATEWAY=""
. "{ROOT}/deploy/control-plane/web-egress-deploy.sh"
web_egress_cmd() {{ echo unexpected-egress-command >> "{harness['log']}"; return 99; }}
web_egress_preflight
[ "$MEMORY_PRIVATE_EGRESS" = 0 ] && [ "$WEB_EGRESS_COMPOSE_ENABLED" = 0 ]
'''
    result=run(harness,body)
    assert result.returncode==0,result.stderr
    assert not harness['log'].exists()
