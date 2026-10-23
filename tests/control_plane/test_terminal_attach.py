"""Terminal button: a viewer-side attach command built only from the verified session record."""
import json
import shlex
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.terminal_attach import AttachRefused, attach_commands

SID = 'herdr:beast:default:wK%3ApH'
PATH = '/api/v1/sessions/' + urllib.parse.quote(SID, safe='') + '/terminal'
LIVE = {'session_id': SID, 'host_id': 'beast', 'herdr_session': 'default', 'pane_id': 'wK:pH',
        'terminal_id': 'term_65df79f50e51582', 'status': 'idle'}
HOSTS = [{'host_id': 'beast', 'tailnet_ip': '100.64.0.2'}, {'host_id': 'bender', 'tailnet_ip': '100.64.0.1'}]


def test_remote_viewer_gets_ssh_form_and_both_forms_are_always_offered():
    out = attach_commands(LIVE, hosts=HOSTS, peer='100.64.0.1')
    assert out['viewer_host'] == 'bender' and out['recommended'] == 'remote'
    forms = {c['form']: c['command'] for c in out['commands']}
    assert forms['local'] == 'herdr --session default agent attach wK:pH'
    assert forms['remote'] == "ssh -t beast \"bash -lc 'herdr --session default agent attach wK:pH'\""
    # The remote form parses to exactly ssh + host + one remote command string.
    argv = shlex.split(forms['remote'])
    assert argv[:3] == ['ssh', '-t', 'beast']
    assert shlex.split(shlex.split(argv[3])[2]) == ['herdr', '--session', 'default', 'agent', 'attach', 'wK:pH']


def test_viewer_on_the_agent_host_gets_the_local_form_recommended():
    assert attach_commands(LIVE, hosts=HOSTS, peer='100.64.0.2')['recommended'] == 'local'


def test_ipv4_mapped_peer_is_recognised():
    assert attach_commands(LIVE, hosts=HOSTS, peer='::ffff:100.64.0.2')['viewer_host'] == 'beast'


@pytest.mark.parametrize('status', ['idle', 'done', 'working', 'blocked', 'unknown', 'active'])
def test_every_live_herdr_status_attaches(status):
    assert attach_commands({**LIVE, 'status': status})['commands']


@pytest.mark.parametrize('peer', ['127.0.0.1', '10.9.9.9', None, 'garbage'])
def test_unknown_viewer_defaults_to_ssh(peer):
    out = attach_commands(LIVE, hosts=HOSTS, peer=peer)
    assert out['viewer_host'] is None and out['recommended'] == 'remote'


@pytest.mark.parametrize('field,value', [
    ('host_id', 'beast;rm -rf ~'), ('host_id', '-oProxyCommand=sh'), ('host_id', 'be ast'),
    ('herdr_session', "x'$(id)'"), ('herdr_session', '-x'), ('pane_id', 'w1:p1`id`'),
    ('pane_id', 'a"b'), ('pane_id', 'p\n1'), ('pane_id', 'wK:pH\n'), ('host_id', 'beast\n'), ('terminal_id', 'not-a-terminal'),
    ('herdr_session', 'a' * 65), ('host_id', 'b' * 64),
])
def test_injection_in_session_fields_is_refused_and_not_echoed(field, value):
    with pytest.raises(AttachRefused) as exc:
        attach_commands({**LIVE, field: value}, hosts=HOSTS)
    assert exc.value.status == 409 and exc.value.body['reason'] == 'unsafe_identity'
    assert value not in json.dumps(exc.value.body)


@pytest.mark.parametrize('changes', [{'status': 'stopped'}, {'status': 'COMPLETE'}, {'pane_id': None},
                                     {'terminal_id': ''}, {'herdr_session': None}, {'host_id': None}])
def test_dead_or_unidentified_session_is_refused(changes):
    with pytest.raises(AttachRefused) as exc:
        attach_commands({**LIVE, **changes})
    assert exc.value.status == 409 and exc.value.body['reason'] == 'not_live'


# ------------------------------------------------------------------ HTTP route
class Server:
    def __init__(self, **kwargs):
        self.repo = ControlPlaneRepo()
        self.server = ControlPlaneServer(self.repo, host='127.0.0.1', port=0, **kwargs)
        threading.Thread(target=self.server.serve, daemon=True).start()
        self.server.wait_until_ready()

    def post(self, path=PATH, *, key=None, headers=None, data=b'{}', port=None):
        hdrs = {'Content-Type': 'application/json', **({'X-API-Key': key} if key else {}), **(headers or {})}
        request = urllib.request.Request(f'http://127.0.0.1:{port or self.server.port}' + path,
                                         data=data, headers=hdrs, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)


@pytest.fixture
def open_server():
    s = Server(allow_open=True)
    yield s
    s.server.shutdown()


def seed(repo, **changes):
    fields = {k: v for k, v in {**LIVE, **changes}.items() if k != 'session_id'}
    repo.upsert_session(SID, agent_role='pi', **fields)


def test_route_returns_commands_built_from_the_record(open_server):
    seed(open_server.repo)
    status, body = open_server.post(data=json.dumps({'host_id': 'evil;id', 'pane_id': 'x'}).encode())
    assert status == 200
    assert body['commands'][1]['command'] == 'herdr --session default agent attach wK:pH'
    assert 'evil' not in json.dumps(body)  # client-supplied strings are ignored


def test_route_unknown_session_is_404(open_server):
    status, body = open_server.post('/api/v1/sessions/nope/terminal')
    assert status == 404


def test_route_dead_and_injected_sessions_are_409(open_server):
    seed(open_server.repo, status='stopped')
    assert open_server.post()[1]['reason'] == 'not_live'
    seed(open_server.repo, status='idle', pane_id='w1:p1;id')
    status, body = open_server.post()
    assert status == 409 and body['reason'] == 'unsafe_identity' and ';id' not in json.dumps(body)


def test_route_refuses_cross_origin_and_non_json(open_server):
    seed(open_server.repo)
    assert open_server.post(headers={'Origin': 'https://evil.example'})[0] == 403
    assert open_server.post(headers={'Content-Type': 'text/plain'})[0] == 415
    host = f'127.0.0.1:{open_server.server.port}'
    assert open_server.post(headers={'Origin': f'http://{host}'})[0] == 200


def test_route_is_operator_only():
    s = Server(api_keys={'read': 'viewer', 'write': 'operator'})
    try:
        seed(s.repo)
        assert s.post(key='read')[0] == 403
        assert s.post()[0] == 401
        assert s.post(key='write')[0] == 200
        request = urllib.request.Request(f'http://127.0.0.1:{s.server.port}/api/v1/sessions/'
                                         + urllib.parse.quote(SID, safe=''), headers={'X-API-Key': 'read'})
        with urllib.request.urlopen(request, timeout=3) as response:
            session = json.load(response)
        assert session['capabilities']['attach_terminal'] is False
    finally:
        s.server.shutdown()


def test_public_and_collab_operator_listeners_do_not_serve_it(tmp_path):
    from herdr_engineering.control_plane.collab import (
        CollabService,
        FileEventLog,
        StaticTokenAuthenticator,
    )
    from tests.control_plane.test_collab import TOKENS, _digest
    from tests.control_plane.test_collab_operator import HOST, call, free_port
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}),
                        sink=FileEventLog(str(tmp_path / 'log')), public_config={'public_url': 'https://x/'})
    port = free_port()
    repo = ControlPlaneRepo()
    seed(repo)
    only = ControlPlaneServer(repo, host='127.0.0.1', port=0, collab=svc, allow_open=True,
                              public_port=0, public_host='127.0.0.1', collab_only=True,
                              operator_port=port, operator_user='alice', operator_email='alice@example.org',
                              operator_hosts=[HOST, f'127.0.0.1:{port}'])
    threading.Thread(target=only.serve, daemon=True).start()
    only.wait_until_ready()
    try:
        for listener in (only.public_port, only.operator_port):
            assert call(listener, 'POST', PATH, {})[0] == 404
            assert call(listener, 'GET', PATH)[0] == 404
    finally:
        only.shutdown()
