"""Dashboard full /summary report: a durable queue that the owning host's worker claims and finishes."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane import summary_report as sr
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.summary_report import ReportConfig, ReportRefused, SummaryReportService

SID = 'herdr:bender:eng:w1%3Ap1'
BASE = '/api/v1/sessions/' + urllib.parse.quote(SID, safe='')
REPORT = '# Session summary\n**Overall status:** completed\n\nEvidence coverage: complete\n'
STORED = REPORT.strip()      # validation trims surrounding whitespace
ACTOR = {'role': 'operator', 'via': 'api_key'}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def native(repo, sid=SID, **changes):
    kwargs = {'host_id': 'bender', 'terminal_id': 't', 'herdr_session': 'eng', 'agent_role': 'claude',
              'agent_session': {'agent': 'claude', 'kind': 'id', 'value': 'abc-123'}, 'status': 'active'}
    kwargs.update(changes)
    repo.upsert_session(sid, **kwargs)


def service(repo, clock=None, **cfg):
    return SummaryReportService(repo, ReportConfig(enabled=cfg.pop('enabled', True), **cfg), clock=clock or Clock())


def ok(job, **extra):
    return {'ok': True, 'report': REPORT, 'harness': 'claude', 'duration_s': 1.5,
            'session_id': job['session_id'], 'operation_id': job['operation_id'], **extra}


def test_disabled_by_default_and_config_is_validated():
    assert ReportConfig().enabled is False
    for bad in ({'harness': 'codex'}, {'timeout_seconds': 1}, {'enabled': 'yes'}, {'max_concurrent': 9},
                {'local_host_ids': ['x']}):
        with pytest.raises((ValueError, TypeError)):
            ReportConfig(**bad)


def test_request_claim_finish_is_durable(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    status, body = svc.start(SID, actor=ACTOR)
    assert status == 202 and body['status'] == 'queued' and svc.get(SID)['state'] == 'queued'
    assert svc.claim('beast') == []                                  # another host never sees it
    (job,) = svc.claim('bender')
    assert job['agent'] == 'claude' and job['target'] == 'abc-123' and job['harness'] == 'claude'
    assert svc.get(SID)['state'] == 'running' and svc.claim('bender') == []
    assert svc.finish('bender', ok(job))[1]['status'] == 'done'
    done = svc.get(SID)
    assert done['state'] == 'done' and done['report'] == STORED and 'host_id' not in done
    again = service(repo)                                            # a restart finds it in the event log
    assert again.get(SID)['report'] == STORED
    kinds = [e['event_type'] for e in repo.stream('session', SID)]
    assert kinds.count('SummaryReportRequested') == 1 and 'SummaryReportCompleted' in kinds


def test_second_click_while_open_returns_the_same_job(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    first = svc.start(SID, actor=ACTOR)[1]
    status, second = svc.start(SID, actor=ACTOR)
    assert status == 202 and second['replayed'] and second['operation_id'] == first['operation_id']
    assert [e['event_type'] for e in repo.stream('session', SID)].count('SummaryReportRequested') == 1


def test_restart_readopts_an_open_request_so_a_host_can_still_claim_it(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    clock = Clock()
    first = service(repo, clock)
    first.start(SID, actor=ACTOR)
    second = service(repo, clock)                                    # control plane restarted
    assert second.get(SID)['state'] == 'queued'
    (job,) = second.claim('bender')
    assert second.finish('bender', ok(job))[1]['status'] == 'done'


def test_unclaimed_and_stuck_jobs_fail_with_a_code(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    clock = Clock()
    svc = service(repo, clock, timeout_seconds=60)
    svc.start(SID, actor=ACTOR)
    clock.now += sr.QUEUE_GRACE_S + 1
    out = svc.get(SID)
    assert out['state'] == 'failed' and out['error'] == 'host_unreachable'
    svc.start(SID, actor=ACTOR)
    svc.claim('bender')
    clock.now += 60 + sr.RUN_GRACE_S + 1
    assert svc.get(SID)['error'] == 'timeout'
    assert svc.start(SID, actor=ACTOR)[0] == 202                     # a failed run never blocks a retry


def test_limit_counts_open_jobs_only(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svc = service(repo, max_concurrent=1)
    for sid in ('s1', 's2'):
        native(repo, sid)
    svc.start('s1', actor=ACTOR)
    with pytest.raises(ReportRefused) as err:
        svc.start('s2', actor=ACTOR)
    assert err.value.status == 429


def test_host_cannot_finish_another_hosts_job_and_stale_posts_are_ignored(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    (job,) = svc.claim('bender')
    with pytest.raises(ReportRefused) as err:
        svc.finish('beast', ok(job))
    assert err.value.status == 403 and svc.get(SID)['state'] == 'running'
    assert svc.finish('bender', ok(job, operation_id='some-older-run'))[1]['replayed']
    assert svc.get(SID)['state'] == 'running'
    svc.finish('bender', ok(job))
    assert svc.finish('bender', ok(job))[1]['replayed']              # a retried post stores nothing twice
    assert [e['event_type'] for e in repo.stream('session', SID)].count('SummaryReportCompleted') == 1


def test_server_revalidates_and_redacts_what_the_host_sends(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    (job,) = svc.claim('bender')
    svc.finish('bender', ok(job, report='Sure! here you go'))
    assert svc.get(SID)['error'] == 'malformed_report'
    svc.start(SID, actor=ACTOR)
    (job,) = svc.claim('bender')
    svc.finish('bender', ok(job, report=REPORT + '\nkey sk-ant-api03-SECRETSECRETSECRETSECRET1234\n'))
    assert 'SECRETSECRET' not in json.dumps(svc.get(SID))


def test_host_failures_keep_a_known_code_and_never_leak_unknown_text(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    for code, expected in (('timeout', 'timeout'), ('<script>', 'summarizer_failed')):
        svc.start(SID, actor=ACTOR)
        (job,) = svc.claim('bender')
        svc.finish('bender', {'ok': False, 'error': code, 'detail': 'tok sk-ant-api03-SECRETSECRETSECRET1234',
                              'session_id': job['session_id'], 'operation_id': job['operation_id']})
        out = svc.get(SID)
        assert out['state'] == 'failed' and out['error'] == expected and 'SECRETSECRET' not in json.dumps(out)


@pytest.mark.parametrize('changes,reason', [
    ({'agent_session': None}, 'no_identity'),
    ({'agent_session': {'agent': 'integration', 'kind': 'id', 'value': 'x'}}, 'no_identity'),
    ({'agent_session': {'agent': 'claude', 'kind': 'id', 'value': '--resume'}}, 'no_identity'),
    ({'host_id': ''}, 'no_host'),
])
def test_refusals_queue_nothing(acknowledged_sink, changes, reason):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo, **changes)
    svc = service(repo)
    with pytest.raises(ReportRefused) as err:
        svc.start(SID, actor=ACTOR)
    assert err.value.status == 409 and err.value.body['reason'] == reason and svc.claim('bender') == []


def test_any_host_can_be_the_target_and_path_identity_uses_the_file_stem(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo, host_id='beast', agent_session={'agent': 'pi', 'kind': 'path',
                                                 'value': '/h/.pi/agent/sessions/x/2026-10-28T19_01a1.jsonl'})
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    assert svc.claim('bender') == []
    (job,) = svc.claim('beast')
    assert (job['agent'], job['target']) == ('pi', '2026-10-28T19_01a1')


def test_disabled_service_refuses_and_hands_out_nothing(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo, enabled=False)
    with pytest.raises(ReportRefused) as err:
        svc.start(SID, actor=ACTOR)
    assert err.value.body['reason'] == 'reports_disabled' and svc.claim('bender') == []


def test_unsaved_outcome_stays_readable_and_does_not_block_a_retry(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    (job,) = svc.claim('bender')
    real = repo.record_event

    def refuse(entity, eid, event_type, *a, **k):
        if event_type == 'SummaryReportCompleted':
            raise RuntimeError('db down')
        return real(entity, eid, event_type, *a, **k)
    repo.record_event = refuse
    svc.finish('bender', ok(job))
    out = svc.get(SID)
    assert out['state'] == 'done' and out['persisted'] is False and out['report'] == STORED
    repo.record_event = real
    assert svc.start(SID, actor=ACTOR)[0] == 202


# -- HTTP ----------------------------------------------------------------------------------------
class Http:
    def __init__(self, sink, enabled=True, **server_kwargs):
        self.repo = ControlPlaneRepo(sink=sink)
        native(self.repo)
        self.server = ControlPlaneServer(
            self.repo, api_keys={'read': 'viewer', 'write': 'operator', 'host': 'ingest', **server_kwargs.pop('extra_keys', {})},
            summary_config={'enabled': False}, summary_report_config={'enabled': enabled}, **server_kwargs)
        threading.Thread(target=self.server.serve, daemon=True).start()
        self.server.wait_until_ready()

    def call(self, path, body=None, key='write', method='POST', headers=None):
        hdrs = {'Content-Type': 'application/json', 'X-API-Key': key, **(headers or {})}
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.port}{path}',
                                     data=json.dumps(body if body is not None else {}).encode() if method == 'POST' else None,
                                     headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            return err.code, json.load(err)


@pytest.fixture
def http(acknowledged_sink):
    made = []
    yield lambda **kw: made.append(Http(acknowledged_sink, **kw)) or made[-1]
    for h in made:
        h.server.shutdown()


def test_http_flow_auth_and_full_report_in_get(http):
    h = http()
    start = BASE + '/summary/report'
    assert h.call(start, key='read')[0] == 403                                   # viewers cannot start runs
    assert h.call(start, key='host')[0] == 403                                   # nor can a host token
    assert h.call(start, headers={'Origin': 'https://evil.example'})[0] == 403
    assert h.call(start, headers={'Content-Type': 'text/plain'})[0] == 415
    assert h.call(start)[1]['status'] == 'queued'
    assert h.call('/api/v1/host/summary-reports/claim', {'host_id': 'bender'}, key='read')[0] == 403
    assert h.call('/api/v1/host/summary-reports/claim', {'host_id': 'bender'}, key='write')[0] == 200   # operator may too
    status, claim = h.call('/api/v1/host/summary-reports/claim', {'host_id': 'bender'}, key='host')
    assert status == 200 and claim['jobs'] == []                                 # already handed out above
    status, summary = h.call(BASE + '/summary', key='read', method='GET')
    assert summary['full_report']['state'] == 'running'
    job = h.server.summary_report._jobs[SID]
    posted = {'host_id': 'bender', 'session_id': SID, 'operation_id': job['operation_id'], 'ok': True,
              'report': REPORT, 'harness': 'claude', 'duration_s': 2}
    assert h.call('/api/v1/host/summary-reports/result', posted, key='read')[0] == 403
    assert h.call('/api/v1/host/summary-reports/result', {**posted, 'host_id': 'beast'}, key='host')[0] == 403
    assert h.call('/api/v1/host/summary-reports/result', {k: v for k, v in posted.items() if k != 'host_id'},
                  key='host')[0] == 400
    assert h.call('/api/v1/host/summary-reports/result', posted, key='host')[1]['status'] == 'done'
    summary = h.call(BASE + '/summary', key='read', method='GET')[1]
    assert summary['full_report']['state'] == 'done' and summary['full_report']['report'] == STORED
    caps = h.call(BASE, key='write', method='GET')[1]['capabilities']
    assert caps['summary_report'] is True
    assert h.call(BASE, key='read', method='GET')[1]['capabilities']['summary_report'] is False


def test_http_bound_keys_act_only_for_their_hosts(http):
    h = http(architecture_options={'agent_hosts': {'host': ['bender'], 'agent-b': ['beast']},
                                   'require_host_binding': True},
             extra_keys={'agent-b': 'agent', 'agent-free': 'agent'})
    assert h.call(BASE + '/summary/report')[1]['status'] == 'queued'
    claim = '/api/v1/host/summary-reports/claim'
    assert h.call(claim, {'host_id': 'bender'}, key='agent-b')[0] == 403          # bound to another host
    assert h.call(claim, {'host_id': 'bender'}, key='agent-free')[0] == 403       # unbound agent key refused
    assert h.call(claim, {'host_id': 'beast'}, key='host')[0] == 403              # bound ingest key, wrong host
    for path in ('result', 'publish'):
        assert h.call(f'/api/v1/host/summary-reports/{path}', {'host_id': 'bender'}, key='agent-b')[0] == 403
    status, got = h.call(claim, {'host_id': 'bender'}, key='host')
    assert status == 200 and len(got['jobs']) == 1                                # its own host works
    assert h.call(claim, {'host_id': 'anything'}, key='write')[0] == 200          # operators stay trusted


def test_concurrent_duplicate_results_store_once(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    (job,) = svc.claim('bender')
    real = repo.record_event

    def slow(*args, **kwargs):
        time.sleep(0.05)                      # widen the window between adopting the job and storing its outcome
        return real(*args, **kwargs)

    repo.record_event = slow
    results = []
    gate = threading.Barrier(4)

    def post():
        gate.wait()
        results.append(svc.finish('bender', ok(job))[1])

    threads = [threading.Thread(target=post) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [e['event_type'] for e in repo.stream('session', SID)].count('SummaryReportCompleted') == 1
    assert sum(1 for r in results if r.get('status') == 'done') == 1


def test_http_disabled_hides_the_feature(http):
    h = http(enabled=False)
    status, body = h.call(BASE + '/summary/report')
    assert status == 409 and body['reason'] == 'reports_disabled'
    assert h.call('/api/v1/host/summary-reports/claim', {'host_id': 'bender'}, key='host')[1] == {'ok': True, 'jobs': []}
    assert 'full_report' not in h.call(BASE + '/summary', key='read', method='GET')[1]
    assert h.call(BASE, key='write', method='GET')[1]['capabilities']['summary_report'] is False


def test_report_records_how_much_of_the_session_it_covers(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    repo.inspector.ingest(SID, {'kind': 'message', 'role': 'user', 'text': 'one', 'message_id': 'm0'})
    repo.inspector.ingest(SID, {'kind': 'message', 'role': 'assistant', 'text': 'two', 'message_id': 'm1'})
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    seq = repo.inspector.newest_seq(SID)
    assert seq is not None and seq >= 1
    (job,) = svc.claim('bender')
    svc.finish('bender', ok(job))
    assert svc.get(SID)['basis_seq'] == seq                 # the dashboard compares it with the newest event it sees
    assert service(repo).get(SID)['basis_seq'] == seq        # and it survives a restart


def test_terminal_publish_lands_on_the_matching_agent(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    native(repo, 'other', agent_session={'agent': 'claude', 'kind': 'id', 'value': 'zzz'})
    svc = service(repo)
    status, body = svc.publish('bender', {'agent': 'claude', 'target': 'abc-123', 'report': REPORT})
    assert status == 200 and body['session_id'] == SID
    done = svc.get(SID)
    assert done['state'] == 'done' and done['source'] == 'terminal' and done['report'] == STORED
    assert svc.get('other')['state'] == 'idle'


def test_terminal_publish_refusals(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    native(repo, 'twin')                                      # same harness session on the same host
    svc = service(repo)
    for body, status in (({'agent': 'claude', 'target': 'abc-123', 'report': REPORT}, 409),
                         ({'agent': 'claude', 'target': 'nope', 'report': REPORT}, 404),
                         ({'agent': 'claude', 'target': 'abc-123', 'report': 'hi'}, 422),
                         ({'agent': 'gemini', 'target': 'abc-123', 'report': REPORT}, 400),
                         ({'agent': 'claude', 'target': '--x', 'report': REPORT}, 400)):
        with pytest.raises(ReportRefused) as err:
            svc.publish('bender', body)
        assert err.value.status == status
    with pytest.raises(ReportRefused):
        svc.publish('beast', {'agent': 'claude', 'target': 'abc-123', 'report': REPORT})   # another host's agent
    off = service(repo, enabled=False)
    with pytest.raises(ReportRefused) as err:
        off.publish('bender', {'agent': 'claude', 'target': 'abc-123', 'report': REPORT})
    assert err.value.body['reason'] == 'reports_disabled'


def test_terminal_publish_does_not_cancel_a_running_job(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc = service(repo)
    svc.start(SID, actor=ACTOR)
    (job,) = svc.claim('bender')
    svc.publish('bender', {'agent': 'claude', 'target': 'abc-123', 'report': REPORT})
    assert svc.get(SID)['state'] == 'running'                  # the open run is still tracked
    assert svc.finish('bender', ok(job))[1]['status'] == 'done'


def test_http_terminal_publish_needs_a_host_token(http):
    h = http()
    body = {'host_id': 'bender', 'agent': 'claude', 'target': 'abc-123', 'report': REPORT}
    assert h.call('/api/v1/host/summary-reports/publish', body, key='read')[0] == 403
    status, out = h.call('/api/v1/host/summary-reports/publish', body, key='host')
    assert status == 200 and out['session_id'] == SID
    full = h.call(BASE + '/summary', key='read', method='GET')[1]['full_report']
    assert full['state'] == 'done' and full['source'] == 'terminal'
