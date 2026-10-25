# ruff: noqa: F811  (pytest fixtures imported from sibling test modules)
"""HTTP adapters for the skill library: list, draft review, publish and download.

Real Postgres; only the S3 transport is doubled. Publication authority is the registry's, the
package stays signed and untrusted, and the control plane never executes or installs a skill."""
from __future__ import annotations

import hashlib
import importlib
import time

import pytest

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from tests.control_plane.test_skill_registry import (  # noqa: F401
    agent,
    env,
    metadata,
    publish,
    user,
)

NAMES = ('alice', 'bob', 'admin', 'maintainer', 'pending')


@pytest.fixture
def web(env):
    module = importlib.import_module('herdr_engineering.control_plane.skill_transfer')
    registry, run, conn, s3, _, invalid = env
    s3.grants = []

    def presign(operation, *, Params, ExpiresIn, HttpMethod):
        s3.grants.append((operation, Params, ExpiresIn, HttpMethod))
        return 'https://storage.invalid/package?X-Amz-Signature=SECRET'
    s3.generate_presigned_url = presign
    service = CollabService(
        StaticTokenAuthenticator({n: hashlib.sha256(f'{n}-token'.encode()).hexdigest() for n in NAMES}),
        skill_service=module.SkillTransfer(registry), skill_runner=run)

    def call(method, parts, body=None, who='alice', query=None, **kw):
        return service.dispatch(method, parts, query or {}, body or {}, f'Bearer {who}-token', None, **kw)

    return type('Web', (), dict(service=service, call=call, run=run, conn=conn, s3=s3, registry=registry,
                                 invalid=invalid, env=env))


def draft(web, owner='alice', **changes):
    return web.run(web.registry.create_skill_draft(user(owner), metadata(**changes)))


# ---------------------------------------------------------------------- library
def test_library_lists_published_versions_with_status_and_own_drafts_only(web):
    _, version = publish(web.env)
    pending = draft(web, version='2.0.0')
    draft(web, 'bob', version='3.0.0')                                  # someone else's draft
    status, body = web.call('GET', ['skills'], query={'scope': 'private'})
    assert status == 200 and set(body) == {'skills', 'drafts'}
    [skill] = body['skills']
    assert (skill['skill_id'], skill['version'], skill['status']) == (version.skill_id, '1.0.0', 'published')
    assert skill['manifest']['skill_id'] == skill['skill_id'] and skill['manifest']['version'] == '1.0.0'
    assert skill['manifest']['trust'] == 'untrusted' and skill['manifest']['execution'] == 'harness_only'
    [mine] = body['drafts']
    assert (mine['draft_id'], mine['status'], mine['manifest']['creator_id']) == (pending.draft_id, 'draft', 'alice')
    assert 'storage_key' not in str(body) and 'signature' not in str(body.get('drafts'))
    # another person's private skill and draft are invisible
    other = web.call('GET', ['skills'], who='bob', query={'scope': 'private'})[1]
    assert other['skills'] == [] and [d['manifest']['creator_id'] for d in other['drafts']] == ['bob']


def test_library_scopes_and_validation(web):
    publish(web.env)
    publish(web.env, 'room')
    publish(web.env, 'shared', user('maintainer'))
    assert [s['manifest']['scope'] for s in web.call('GET', ['skills'], query={'scope': 'shared'})[1]['skills']] == ['shared']
    assert [s['manifest']['scope'] for s in web.call('GET', ['skills'], who='bob', query={'scope': 'room'})[1]['skills']] == ['room']
    assert web.call('GET', ['skills'], query={'scope': 'world'})[0] == 400
    assert web.call('GET', ['skills'], query={'scope': 'shared', 'q': 'x' * 201})[0] == 400
    assert web.call('GET', ['skills'], query={'scope': 'shared'})[1]['drafts'] == []      # drafts only on the private tab


def test_a_published_draft_leaves_the_draft_list(web):
    d, _ = publish(web.env)
    assert web.call('GET', ['skills'], query={'scope': 'private'})[1]['drafts'] == []
    assert d.draft_id


# ------------------------------------------------------------------ draft review
def test_draft_review_reports_publication_authority_from_the_registry(web):
    d = draft(web, room_id='room_one')
    status, body = web.call('GET', ['skills', 'drafts', d.draft_id])
    assert status == 200 and body['status'] == 'draft' and body['draft_id'] == d.draft_id
    assert body['manifest']['creator_id'] == 'alice' and body['target_room_id'] == 'room_one'
    assert body['publish_scopes'] == ['private', 'room']                  # room owner, not a maintainer
    assert set(body) >= {'skill_id', 'version', 'sha256', 'manifest', 'publish_scopes'}
    m = draft(web, 'maintainer')
    scopes = web.call('GET', ['skills', 'drafts', m.draft_id], who='maintainer')[1]['publish_scopes']
    assert scopes == ['private', 'shared']
    b = draft(web, 'bob', room_id='room_one')
    assert web.call('GET', ['skills', 'drafts', b.draft_id], who='bob')[1]['publish_scopes'] == ['private']


def test_draft_review_is_creator_only_and_fails_closed(web):
    d = draft(web)
    assert web.call('GET', ['skills', 'drafts', d.draft_id], who='bob')[0] == 404
    assert web.call('GET', ['skills', 'drafts', 'nope'])[0] == 404
    web.invalid.add('user:alice')
    assert web.call('GET', ['skills', 'drafts', d.draft_id])[0] == 403
    web.invalid.clear()
    web.run(web.conn.execute("UPDATE collab_skills SET status='revoked'"))
    assert web.call('GET', ['skills', 'drafts', d.draft_id])[0] == 404


def test_tampered_draft_package_is_never_presented_for_review(web):
    d = draft(web)
    key = web.run(web.conn.fetchval('SELECT storage_key FROM collab_skill_drafts WHERE draft_id=$1', d.draft_id))
    original = web.s3.objects['skills', key]
    web.s3.objects['skills', key] = original.replace(b'Summarize CSV', b'Summarize EVL')
    assert web.s3.objects['skills', key] != original                       # the tamper really happened
    status, body = web.call('GET', ['skills', 'drafts', d.draft_id])
    assert status == 409 and 'manifest' not in body


# ------------------------------------------------------------------------ publish
def test_publish_creates_an_immutable_signed_version_and_reports_real_state(web):
    d = draft(web)
    status, body = web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], {'scope': 'private'})
    assert status == 200 and body == {'skill_id': d.skill_id, 'version': '1.0.0', 'status': 'published',
                                      'sha256': body['sha256']}
    assert web.run(web.conn.fetchval("SELECT status FROM collab_skill_versions WHERE skill_id=$1", d.skill_id)) == 'published'
    assert web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], {'scope': 'private'}) == (200, body)   # retry
    assert web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], {'scope': 'shared'})[0] in (403, 409)  # immutable scope


@pytest.mark.parametrize(('who', 'scope', 'expected'), [
    ('bob', 'shared', 403), ('alice', 'shared', 403), ('bob', 'room', 404), ('maintainer', 'shared', 200)])
def test_publication_authority_is_the_registrys_not_the_clients(web, who, scope, expected):
    d = draft(web, who, room_id='room_one')
    assert web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], {'scope': scope}, who=who)[0] == expected


def test_publish_rejects_other_creators_bad_bodies_and_never_publishes_on_error(web):
    d = draft(web)
    assert web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], {'scope': 'private'}, who='bob')[0] == 404
    for body in ({}, {'scope': 'world'}, {'scope': 'private', 'version': '9'}, {'scope': ['private']}):
        status, _ = web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], body)
        assert status in (400, 404), body
    web.s3.fail = True
    status, out = web.call('POST', ['skills', 'drafts', d.draft_id, 'publish'], {'scope': 'private'})
    web.s3.fail = False
    assert status >= 500 and 'secret' not in str(out).lower() and 'status' not in out
    assert web.run(web.conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0


# ----------------------------------------------------------------------- download
def test_download_returns_a_short_lived_https_grant_for_an_authorized_version(web):
    _, version = publish(web.env)
    status, grant = web.call('POST', ['skills', version.skill_id, 'versions', '1.0.0', 'download'])
    assert status == 200
    assert set(grant) == {'skill_id', 'version', 'sha256', 'size_bytes', 'expires_at', 'url'}
    assert (grant['skill_id'], grant['version'], grant['sha256']) == (version.skill_id, '1.0.0', version.sha256)
    assert grant['url'].startswith('https://') and 0 < grant['expires_at'] - time.time() <= 60
    assert web.s3.grants[-1][0] == 'get_object' and web.s3.grants[-1][2] <= 60
    assert web.run(web.conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='skill.download_requested'")) == 1
    assert web.run(web.conn.fetchval('SELECT count(*) FROM collab_skill_installations')) == 0      # download is not install


def test_download_is_scoped_and_fails_closed(web):
    _, version = publish(web.env)
    path = ['skills', version.skill_id, 'versions', '1.0.0', 'download']
    assert web.call('POST', path, who='bob')[0] == 404                       # another person's private skill
    assert web.call('POST', ['skills', version.skill_id, 'versions', '9.9.9', 'download'])[0] == 404
    assert web.call('POST', path, {'agent': 'x'})[0] == 400
    assert web.call('POST', path, who='pending')[0] == 403
    web.run(web.conn.execute("UPDATE collab_skill_versions SET status='revoked'"))
    assert web.call('POST', path)[0] == 404


def test_agents_are_refused_on_the_browser_skill_routes(web):
    _, version = publish(web.env)
    info = web.service.connect_agent(web.service.authenticate('Bearer alice-token', None), {'harness_type': 'python'})
    for method, parts, body in (('GET', ['skills'], {}),
                                ('POST', ['skills', version.skill_id, 'versions', '1.0.0', 'download'], {})):
        status, out = web.service.dispatch(method, parts, {'scope': 'private'}, body,
                                           'Agent ' + info['credential'], info['device_id'])
        assert status == 403 and 'url' not in out


def test_skill_routes_fail_closed_without_a_service_and_runner(web):
    tokens = {'alice': hashlib.sha256(b'alice-token').hexdigest()}
    for service in (CollabService(StaticTokenAuthenticator(tokens)),
                    CollabService(StaticTokenAuthenticator(tokens), skill_service=web.service.skill_service)):
        for method, parts, body in (('GET', ['skills'], {}), ('GET', ['skills', 'drafts', 'd'], {}),
                                    ('POST', ['skills', 'drafts', 'd', 'publish'], {'scope': 'private'}),
                                    ('POST', ['skills', 's', 'versions', '1', 'download'], {})):
            assert service.dispatch(method, parts, {'scope': 'private'}, body, 'Bearer alice-token', None)[0] == 503


def test_operator_listener_serves_the_same_library(web):
    publish(web.env)
    public = web.call('GET', ['skills'], query={'scope': 'private'})
    dashboard = web.service.dispatch('GET', ['skills'], {'scope': 'private'}, {}, '', None, operator='alice')
    assert dashboard == public and dashboard[1]['skills']
