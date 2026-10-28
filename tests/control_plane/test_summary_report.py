"""Dashboard full /summary report: one background run per session, local sessions only, durable result."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering import summary_report as runner
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.summary_report import (
    ReportConfig, ReportRefused, SummaryReportService)

SID = 'herdr:bender:eng:w1%3Ap1'
BASE = '/api/v1/sessions/' + urllib.parse.quote(SID, safe='')
REPORT = '# Session summary\n**Overall status:** completed\n\nEvidence coverage: complete\n'
ACTOR = {'role': 'operator', 'via': 'api_key'}


def native(repo, sid=SID, **changes):
    kwargs = {'host_id': 'bender', 'terminal_id': 't', 'herdr_session': 'eng', 'agent_role': 'claude',
              'agent_session': {'agent': 'claude', 'kind': 'id', 'value': 'abc-123'}, 'status': 'active'}
    kwargs.update(changes)
    repo.upsert_session(sid, **kwargs)


def service(repo, run, *, enabled=True, **kw):
    calls = []
    return SummaryReportService(repo, ReportConfig(enabled=enabled), run=run, hostnames={'bender'},
                                spawn=kw.get('spawn') or (lambda fn: calls.append(fn))), calls


def ok_run(agent, session, **kw):
    return {'ok': True, 'report': REPORT, 'harness': kw['harness'], 'generated_at': 'now', 'duration_s': 1.5}


def test_disabled_by_default_and_config_is_validated():
    assert ReportConfig().enabled is False
    with pytest.raises(ValueError):
        ReportConfig(harness='codex')           # not exposed until it has run live
    with pytest.raises(ValueError):
        ReportConfig(timeout_seconds=1)
    with pytest.raises(ValueError):
        ReportConfig(enabled='yes')


def test_start_runs_in_background_and_result_is_durable(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc, jobs = service(repo, ok_run)
    status, body = svc.start(SID, actor=ACTOR)
    assert status == 202 and body['status'] == 'running' and svc.get(SID)['state'] == 'running'
    jobs.pop()()                                # the thread body
    done = svc.get(SID)
    assert done['state'] == 'done' and done['report'] == REPORT and done['harness'] == 'claude'
    # A new service (a restart) still finds it in the event log.
    again, _ = service(repo, ok_run)
    assert again.get(SID)['state'] == 'done' and again.get(SID)['report'] == REPORT
    kinds = [e['event_type'] for e in repo.stream('session', SID)]
    assert 'SummaryReportRequested' in kinds and 'SummaryReportCompleted' in kinds


def test_second_click_while_running_does_not_start_another(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc, jobs = service(repo, ok_run)
    first = svc.start(SID, actor=ACTOR)[1]
    status, second = svc.start(SID, actor=ACTOR)
    assert status == 202 and second['replayed'] and second['operation_id'] == first['operation_id']
    assert len(jobs) == 1


def test_global_limit(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svc = SummaryReportService(repo, ReportConfig(enabled=True, max_concurrent=1), run=ok_run,
                               hostnames={'bender'}, spawn=lambda fn: None)
    for sid in ('s1', 's2'):
        native(repo, sid)
    svc.start('s1', actor=ACTOR)
    with pytest.raises(ReportRefused) as err:
        svc.start('s2', actor=ACTOR)
    assert err.value.status == 429


def test_failures_are_recorded_with_a_stable_code(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)

    def failing(agent, session, **kw):
        raise runner.ReportError('timeout', 'did not finish within 300s')
    svc, jobs = service(repo, failing)
    svc.start(SID, actor=ACTOR)
    jobs.pop()()
    out = svc.get(SID)
    assert out['state'] == 'failed' and out['error'] == 'timeout'
    # Unexpected exceptions never leak their text.
    svc2, jobs2 = service(repo, lambda *a, **k: (_ for _ in ()).throw(RuntimeError('secret sk-abc123456789012345')))
    svc2.start(SID, actor=ACTOR)
    jobs2.pop()()
    assert 'secret' not in json.dumps(svc2.get(SID))


@pytest.mark.parametrize('changes,reason', [
    ({'host_id': 'beast'}, 'report_host_remote'),
    ({'agent_session': None}, 'no_identity'),
    ({'agent_session': {'agent': 'integration', 'kind': 'id', 'value': 'x'}}, 'no_identity'),
    ({'agent_session': {'agent': 'claude', 'kind': 'id', 'value': '--resume'}}, 'no_identity'),
])
def test_refusals_never_start_a_run(acknowledged_sink, changes, reason):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo, **changes)
    svc, jobs = service(repo, ok_run)
    with pytest.raises(ReportRefused) as err:
        svc.start(SID, actor=ACTOR)
    assert err.value.status == 409 and err.value.body['reason'] == reason and not jobs


def test_remote_refusal_names_the_command_to_run_there(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo, host_id='beast')
    svc, _ = service(repo, ok_run)
    with pytest.raises(ReportRefused) as err:
        svc.start(SID, actor=ACTOR)
    assert err.value.body['command'] == 'herdr-eng summary report --agent claude --session abc-123'


def test_path_identity_uses_the_file_stem(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo, agent_session={'agent': 'pi', 'kind': 'path', 'value': '/h/.pi/agent/sessions/x/2026-10-28T19_01a1.jsonl'})
    seen = []
    svc, jobs = service(repo, lambda a, s, **k: seen.append((a, s)) or ok_run(a, s, **k))
    svc.start(SID, actor=ACTOR)
    jobs.pop()()
    assert seen == [('pi', '2026-10-28T19_01a1')]


def test_disabled_service_refuses(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc, _ = service(repo, ok_run, enabled=False)
    with pytest.raises(ReportRefused) as err:
        svc.start(SID, actor=ACTOR)
    assert err.value.body['reason'] == 'reports_disabled'


def test_unsaved_outcome_stays_readable(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    native(repo)
    svc, jobs = service(repo, ok_run)
    svc.start(SID, actor=ACTOR)
    real = repo.record_event

    def refuse(entity, eid, event_type, *a, **k):
        if event_type == 'SummaryReportCompleted':
            raise RuntimeError('db down')
        return real(entity, eid, event_type, *a, **k)
    repo.record_event = refuse
    jobs.pop()()
    out = svc.get(SID)
    assert out['state'] == 'done' and out['persisted'] is False and out['report'] == REPORT
    assert svc.start(SID, actor=ACTOR)[0] == 202           # not stuck "running"


# -- HTTP ----------------------------------------------------------------------------------------
class Http:
    def __init__(self, sink, enabled=True):
        self.repo = ControlPlaneRepo(sink=sink)
        native(self.repo)
        self.server = ControlPlaneServer(
            self.repo, api_keys={'read': 'viewer', 'write': 'operator'},
            summary_config={'enabled': False}, summary_report_config={'enabled': enabled})
        self.server.summary_report._local.add('bender')
        self.server.summary_report._run = ok_run
        self.server.summary_report._spawn = lambda fn: fn()
        threading.Thread(target=self.server.serve, daemon=True).start()
        self.server.wait_until_ready()

    def call(self, suffix, key='write', method='POST', headers=None):
        hdrs = {'Content-Type': 'application/json', 'X-API-Key': key, **(headers or {})}
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.port}{BASE}{suffix}',
                                     data=b'{}' if method == 'POST' else None, headers=hdrs, method=method)
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
    assert h.call('/summary/report', key='read')[0] == 403                       # viewers cannot start runs
    assert h.call('/summary/report', headers={'Origin': 'https://evil.example'})[0] == 403
    assert h.call('/summary/report', headers={'Content-Type': 'text/plain'})[0] == 415
    status, body = h.call('/summary/report')
    assert status == 202 and body['ok']
    status, summary = h.call('/summary', key='read', method='GET')
    assert status == 200 and summary['full_report']['state'] == 'done' and summary['full_report']['report'] == REPORT
    caps = h.call('', key='write', method='GET')[1]['capabilities']
    assert caps['summary_report'] is True
    assert h.call('', key='read', method='GET')[1]['capabilities']['summary_report'] is False


def test_http_disabled_hides_the_feature(http):
    h = http(enabled=False)
    status, body = h.call('/summary/report')
    assert status == 409 and body['reason'] == 'reports_disabled'
    assert 'full_report' not in h.call('/summary', key='read', method='GET')[1]
    assert h.call('', key='write', method='GET')[1]['capabilities']['summary_report'] is False
