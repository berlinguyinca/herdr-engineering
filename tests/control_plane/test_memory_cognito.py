"""Real signed tokens and real HTTP boundaries; no external identity service or memory backend."""
import http.client
import json
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfig
from tests.control_plane.test_memory_explorer_api import FakeAdapter, key_id, rec

POOL = 'us-west-2_MemoryTest'
CLIENT = 'memory-client'
ORIGIN = 'https://dashboard.example.test:9447'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(user='alice', **over):
    claims = {'iss': f'https://cognito-idp.us-west-2.amazonaws.com/{POOL}', 'sub': user,
              'username': user, 'client_id': CLIENT, 'token_use': 'access',
              'cognito:groups': ['staff'], 'exp': time.time() + 120}
    claims.update(over)
    return jwt.encode(claims, KEY, algorithm='RS256', headers={'kid': 'memory-k1'})


@pytest.fixture
def runtime(tmp_path):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    (tmp_path / 'jwks.json').write_text(json.dumps({'keys': [{**public, 'kid': 'memory-k1'}]}))
    def members(users=('alice', 'bob'), age=0):
        (tmp_path / 'members.json').write_text(json.dumps({'updated': time.time() - age, 'groups': {'staff': list(users)}}))
    members()
    servers = []
    def start(*, proxy=True, origin=ORIGIN, keys=None, viewers=None, public=False):
        policy = {'enabled': True, 'viewers': viewers if viewers is not None else {
            'alice': {'projects': ['alpha'], 'scopes': []}}, 'cognito': {
            'origin': origin, 'sync_dir': str(tmp_path), 'trusted_proxy_ips': ['127.0.0.1'] if proxy else []}}
        collab = {'cognito_pool': POOL, 'cognito_clients': [CLIENT], 'cognito_web_client': CLIENT,
                  'cognito_group': 'staff', 'cognito_domain': 'https://example.auth.us-west-2.amazoncognito.com',
                  'open_signup': True}
        cfg = FleetConfig({'control_plane': {'roles': [], 'values': {'memory': policy, 'collab': collab}}}, {})
        store = SimpleNamespace(current=lambda: cfg)
        adapter = FakeAdapter([rec('allowed', 'alpha-facts'), rec('other', 'beta-facts'), rec('personal', 'personal-facts')])
        from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
        collaboration = CollabService(StaticTokenAuthenticator({})) if public else None
        server = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys, fleet_config=store,
                                   memory_adapter=adapter, memory_config=policy,
                                   collab=collaboration, public_port=0 if public else None)
        threading.Thread(target=server.serve, daemon=True).start(); server.wait_until_ready()
        servers.append(server)
        return server, adapter
    yield start, members
    for server in servers:
        server.shutdown()


def get(server, *, bearer=None, path='/api/v1/memory/search?q=note', extra=(), host='dashboard.example.test:9447', origin=ORIGIN):
    c = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    c.putrequest('GET', path, skip_host=True)
    c.putheader('Host', host)
    if origin is not None:
        c.putheader('Origin', origin)
    if bearer is not None:
        c.putheader('Authorization', 'Bearer ' + bearer)
    for k, v in extra:
        c.putheader(k, v)
    c.endheaders(); r = c.getresponse(); body = r.read(); c.close()
    return r.status, json.loads(body)


def test_verified_human_is_read_only_and_bank_filtered(runtime):
    s, _ = runtime[0]()
    code, body = get(s, bearer=token())
    assert code == 200
    assert [r['memory_id'] for r in body['results']] == ['allowed']
    assert get(s, bearer=token(), path='/api/v1/memory/status')[0] == 200
    assert get(s, bearer=token(), path='/api/v1/memory/allowed')[0] == 200
    for query in ['project=beta', 'project=personal', 'scope=global', 'scope=infra']:
        assert get(s, bearer=token(), path='/api/v1/memory/search?q=x&' + query)[0] == 403
    assert get(s, bearer=token(), path='/api/v1/memory/other')[0] == 404


@pytest.mark.parametrize('bearer', [None, 'invalid', *[token(**v) for v in [
    {'exp': time.time() - 10}, {'iss': 'https://foreign.example'}, {'client_id': 'foreign'},
    {'token_use': 'id'}, {'cognito:groups': []}]]])
def test_missing_invalid_tokens_never_use_open_listener(runtime, bearer):
    s, adapter = runtime[0](viewers={'open-listener': {'projects': ['alpha'], 'scopes': []}, 'alice': {'projects': ['alpha']}})
    assert get(s, bearer=bearer)[0] == 401
    assert adapter.calls == []


def test_unlisted_verified_person_is_forbidden(runtime):
    s, adapter = runtime[0]()
    assert get(s, bearer=token('bob'))[0] == 403
    assert adapter.calls == []


@pytest.mark.parametrize('host,origin', [
    ('foreign.example', ORIGIN), ('dashboard.example.test:NOTAPORT', ORIGIN),
    ('dashboard.example.test:9447', 'null'), ('dashboard.example.test:9447', ORIGIN + '/'),
    ('dashboard.example.test:9447', ORIGIN + '?q=1'), ('dashboard.example.test:9447', ORIGIN + '#x'),
    ('dashboard.example.test:9447', 'https://[broken'), ('dashboard.example.test:9447', 'http://dashboard.example.test:9447'),
    ('dashboard.example.test:9447', 'https://foreign.example'),
])
def test_browser_origin_and_host_are_strict(runtime, host, origin):
    s, adapter = runtime[0]()
    assert get(s, bearer=token(), host=host, origin=origin)[0] == 403
    assert not adapter.calls


@pytest.mark.parametrize('name,value', [('Host', 'dashboard.example.test:9447'), ('Origin', ORIGIN),
                                       ('Authorization', 'Bearer invalid'), ('Content-Type', 'application/json')])
def test_duplicate_security_headers_are_rejected(runtime, name, value):
    s, adapter = runtime[0]()
    extra = [(name, value)]
    if name == 'Content-Type':
        extra *= 2
    assert get(s, bearer=token(), extra=extra)[0] == 400
    assert not adapter.calls


def test_plaintext_cannot_claim_https_by_matching_authority_or_forwarded_header(runtime):
    # Regression: comparing netloc alone allowed an HTTP listener to impersonate the HTTPS origin.
    s, adapter = runtime[0](proxy=False)
    assert get(s, bearer=token(), extra=[('X-Forwarded-Proto', 'https')])[0] == 403
    assert not adapter.calls


def test_native_keys_still_require_explicit_viewer_and_no_ambiguous_credentials(runtime):
    s, _ = runtime[0](keys={'native': 'viewer', 'unlisted': 'operator'}, viewers={key_id('native'): {'projects': ['alpha'], 'scopes': []}})
    assert get(s, extra=[('X-API-Key', 'native')], host='127.0.0.1', origin=None)[0] == 200
    assert get(s, extra=[('X-API-Key', 'unlisted')], origin=None)[0] == 403
    assert get(s, bearer=token(), extra=[('X-API-Key', 'native')])[0] == 401
    assert get(s, extra=[('X-API-Key', 'native'), ('X-API-Key', 'native')])[0] == 400


def test_membership_removal_and_stale_directory_fail_closed(runtime):
    s, adapter = runtime[0]()
    assert get(s, bearer=token())[0] == 200
    runtime[1](users=())
    assert get(s, bearer=token())[0] == 401
    runtime[1](age=601)
    assert get(s, bearer=token())[0] == 401


def test_auth_config_is_public_only_on_explicit_browser_boundary(runtime):
    s, _ = runtime[0]()
    code, body = get(s, path='/api/v1/memory/auth/config')
    assert code == 200
    assert body == {'enabled': True, 'origin': ORIGIN, 'client_id': CLIENT,
                    'hosted_ui_domain': 'https://example.auth.us-west-2.amazoncognito.com', 'redirect_uri': ORIGIN + '/'}
    assert get(s, path='/api/v1/memory/auth/config', host='foreign.example')[0] == 403
    assert get(s, bearer=token(), path='/api/v1/memory/%61uth/config')[0] == 404


def test_bearer_identity_does_not_change_other_dashboard_auth(runtime):
    s, _ = runtime[0](keys={'native': 'viewer'})
    assert get(s, bearer=token(), path='/api/v1/missions')[0] == 401


@pytest.mark.parametrize('extra', [[('Content-Length', '1')], [('Transfer-Encoding', 'chunked')],
                                  [('Content-Length', '0'), ('Content-Length', '0')]])
def test_memory_get_refuses_ambiguous_body_framing(runtime, extra):
    s, adapter = runtime[0]()
    assert get(s, bearer=token(), extra=extra)[0] == 400
    assert not adapter.calls


def test_encoded_path_cannot_bypass_identity_gate(runtime):
    s, adapter = runtime[0](viewers={'open-listener': {'projects': ['alpha'], 'scopes': []}})
    assert get(s, path='/api/v1/%6demory/search?q=note')[0] == 401
    assert not adapter.calls


@pytest.mark.parametrize('origin', ['null', 'https://host:NOTAPORT', 'https://host:65536',
    'https://host:0', 'https://[broken', 'https://host/', 'https://host?', 'https://host#',
    ' https://host', 'http://host', 'https://user@host'])
def test_policy_refuses_malformed_browser_origin(origin):
    from herdr_engineering.control_plane.memory_cognito import parse_cognito
    with pytest.raises(ValueError, match='origin'):
        parse_cognito({'origin': origin})


def test_cognito_policy_can_be_validated_without_exporting_identifiers():
    from herdr_engineering.fleet_memory import parse_memory_section
    policy = {'origin': ORIGIN, 'trusted_proxy_ips': ['127.0.0.1']}
    assert parse_memory_section({'enabled': False, 'cognito': policy})['cognito']['origin'] == ORIGIN


@pytest.mark.parametrize('origin', ['https://host:443', 'https://host:09447'])
def test_config_rejects_ports_normalized_differently_by_browsers(origin):
    from herdr_engineering.control_plane.memory_cognito import parse_cognito
    with pytest.raises(ValueError, match='origin'):
        parse_cognito({'origin': origin})


def test_signed_in_browser_with_explicit_native_key_keeps_that_identity_bounds(runtime):
    """Run the actual browser client against the HTTP gate (an explicitly trusted local TLS proxy fixture)."""
    import subprocess
    s, _ = runtime[0](keys={'native': 'viewer'}, viewers={
        'alice': {'projects': ['alpha'], 'scopes': []}, key_id('native'): {'projects': ['beta'], 'scopes': []}})
    script = r'''
import {MemoryAuth} from './packages/herdr-web-client/memory-auth.js';
import {HerdrClient} from './packages/herdr-web-client/client.js';
const [base, origin, token] = process.argv.slice(1);
const values = new Map();
const storage = {getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
const transport = (path, options) => fetch(base+path, {...options,headers:{...options.headers,Host:new URL(origin).host,Origin:origin}});
const auth = new MemoryAuth({storage,location:new URL(origin),fetchImpl:transport});
await auth.config();auth.tokens.set({access:token,id:token});
const client = new HerdrClient('', {memoryAuth:auth,headers:{'X-API-Key':'native'},fetchImpl:transport});
const data = await client.exploreMemory(new URLSearchParams({q:'note'}));
let denied=0;try {await client.exploreMemory(new URLSearchParams({q:'note',project:'alpha'}));}catch(e){denied=e.status;}
console.log(JSON.stringify({ids:data.results.map(r=>r.memory_id),denied}));
'''
    from pathlib import Path
    p = subprocess.run(['node', '--input-type=module', '-e', script,
                        f'http://127.0.0.1:{s.port}', ORIGIN, token()],
                       cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=12)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == {'ids': ['other'], 'denied': 403}


def test_public_listener_never_exposes_memory_even_with_private_host_and_valid_jwt(runtime):
    s, adapter = runtime[0](public=True)
    public = SimpleNamespace(port=s.public_port)
    for path in ['/api/v1/memory/search?q=note', '/api/v1/memory/status',
                 '/api/v1/memory/auth/config', '/api/v1/%6demory/status']:
        assert get(public, bearer=token(), path=path)[0] == 404
    assert not adapter.calls
