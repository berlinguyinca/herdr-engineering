"""Whole-branch regressions: ranking, run windows and report-volume read costs."""
import copy
import json
import subprocess
import time
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.ci_reports import normalize_report
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane import test_ci_api as ci_api
from tests.control_plane.test_ci_api import CI_NOW, ci_run, request, upload_ci_report

server = ci_api.server


def test_tests_rank_failure_and_slowest_before_response_cap(server):
    server.repo._now = lambda: CI_NOW
    # Put both interesting IDs after the first 200 identity-ordered aggregates.
    body = {**ci_run(), 'xml': '<testsuite>' + ''.join(
        f'<testcase name="case-{i}" time="1"/>' for i in range(250)) + '</testsuite>'}
    parsed = normalize_report(body, '2026-10-25T12:00:00Z')
    ordered = sorted(parsed['tests'], key=lambda row: row['id'])
    failure, slow = ordered[-1]['name'], ordered[-2]['name']
    xml = '<testsuite>' + ''.join(
        f'<testcase name="{row["name"]}" time="{100 if row["name"] == slow else 1}">'
        + ('<failure/>' if row['name'] == failure else '') + '</testcase>'
        for row in parsed['tests']) + '</testsuite>'
    upload_ci_report(server, xml=xml)
    status, view = request(server, 'ci/tests')
    assert status == 200
    assert [row['name'] for row in view['tests'][:2]] == [failure, slow]
    assert view['scope']['omitted_test_count'] == 250 - len(view['tests'])
    assert view['scope']['omitted_failure_test_count'] == 0
    assert view['scope']['ranking'] == 'failures_then_duration_then_identity'
    assert len(view['tests']) <= 200
    assert len(json.dumps(view).encode()) < 256 * 1024
    assert request(server, 'ci/tests')[1]['tests'] == view['tests']


@pytest.mark.parametrize('days', [7, 14, 30])
def test_summary_heatmap_and_run_list_share_creation_calendar_boundaries(days):
    start = CI_NOW // 86400 * 86400 - (days - 1) * 86400
    rows = [{**ci_run(i), 'created_at': datetime.fromtimestamp(stamp, UTC).isoformat(),
             'finished_at': datetime.fromtimestamp(CI_NOW, UTC).isoformat()}
            for i, stamp in enumerate([start - 1, start, start + 1, CI_NOW, CI_NOW + 1], 1)]
    rows += [{**ci_run(6), 'created_at': None},
             {**ci_run(7), 'created_at': datetime.fromtimestamp(start, UTC).isoformat(),
              'finished_at': datetime.fromtimestamp(CI_NOW + 100, UTC).isoformat()}]
    repo = ControlPlaneRepo(now=lambda: CI_NOW)
    view = repo.ci_summary(pipelines=rows, days=days)
    assert view['kpis']['pipeline_count'] == 4
    assert view['scope']['time_basis'] == 'created_at'
    assert view['scope']['window']['start'] == datetime.fromtimestamp(start, UTC).isoformat()
    assert view['scope']['unknown_time_run_count'] == 1
    script = """
      import {ciHealthModel} from './packages/ci-health.js';
      import {integrationEntries,selectIntegrations} from './packages/integrations-view.js';
      let input=''; for await (const chunk of process.stdin) input+=chunk;
      const {rows,days,now}=JSON.parse(input); Date.now=()=>now*1000;
      const model=ciHealthModel({pipelines:rows,repos:[{id:17,repository:'org/app',provider_url:'https://ci.example',default_branch:'main'}]}, {mode:String(days),scope:'all'},now*1000);
      console.log(JSON.stringify({heatmap:model.counts.total,runs:selectIntegrations(integrationEntries({pipelines:{runs:rows}}),{days,tab:'all'}).map(e=>e.record.run_number).sort()}));
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script],
                            input=json.dumps({'rows': rows, 'days': days, 'now': CI_NOW}),
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {'heatmap': 4, 'runs': [2, 3, 4, 7]}


def test_report_volume_reads_reuse_compact_projection_and_bound_http_payload(server, monkeypatch):
    from herdr_engineering.control_plane.ci_observability import CIObservationStore

    repo = server.repo
    repo._now = lambda: CI_NOW
    base = normalize_report({**ci_run(), 'xml': '<testsuite>' + ''.join(
        f'<testcase name="case-{i}" time="{i % 10}"><failure message="failed">'
        + 'stack frame\n' * 20 + '</failure></testcase>' for i in range(1000))
        + '</testsuite>'}, '2026-10-25T12:00:00Z')
    for number in range(1, 21):
        report = {**copy.deepcopy(base), 'run_number': number, 'report_id': f'{number:064x}'}
        repo.record_event('ci_report', report['report_id'], 'CITestReportReceived', report)
        repo.observe_ci_pipeline(ci_run(number))
    repo._trim_ci_history()
    assert len(json.dumps(repo.export_state()).encode()) > 8 * 1024 * 1024
    start = time.monotonic()
    first = repo.ci_tests(redact=lambda v: v)
    cold = time.monotonic() - start
    assert first['scope']['report_count'] == 20
    assert first['tests'][0]['failure_count'] == 20

    def forbidden(*args, **kwargs):
        raise AssertionError('A summary read reconstructed retained full reports')
    compact = repo.ci_reports(compact=True)
    assert len(json.dumps(compact).encode()) < 64 * 1024
    assert all('tests' not in row for row in compact['reports'])
    monkeypatch.setattr(repo, 'ci_reports', forbidden)
    monkeypatch.setattr(CIObservationStore, 'record_report', forbidden)
    monkeypatch.setattr('herdr_engineering.control_plane.ci_observability.project_report', forbidden)
    timings = []
    for _ in range(3):
        start = time.monotonic()
        view = repo.ci_tests(redact=lambda v: v)
        timings.append(time.monotonic() - start)
        assert view == first
        assert len(json.dumps(view).encode()) < 256 * 1024
        assert repo.ci_summary()['kpis']['pipeline_count'] == 20
        assert repo.ci_performance()['scope']['record_count'] == 0
    repo.observe_ci_pipeline({**ci_run(20), 'state': 'success'})
    start = time.monotonic()
    status, http_view = request(server, 'ci/tests?days=14')
    http_elapsed = time.monotonic() - start
    assert status == 200
    assert http_view['tests'][0]['failure_count'] == 20
    assert len(json.dumps(http_view).encode()) < 256 * 1024
    assert http_elapsed < 1, http_elapsed
    assert max(timings) < 1, timings
    print(f'report-volume: 20 reports x 1000 cases; cold={cold:.3f}s; warm={timings}; HTTP={http_elapsed:.3f}s')


def test_compact_reports_omit_case_payloads_and_detail_is_exact(server):
    server.repo._now = lambda: CI_NOW
    upload_ci_report(server)
    upload_ci_report(server, provider_url='https://other.example')
    server.repo.observe_ci_pipeline(ci_run())
    status, compact = request(server, 'ci/reports?compact=1')
    assert status == 200
    assert len(compact['reports']) == 2
    assert all('tests' not in row for row in compact['reports'])
    assert compact['scope']['test_details'] == 'omitted'
    assert compact['reports'][0]['summary']['failed'] == 1
    status, detail = request(server, 'ci/reports?provider_url=https%3A%2F%2Fci.example&provider_repo_id=17&repository=org%2Fapp&run_number=1')
    assert status == 200
    assert len(detail['reports']) == 1
    assert detail['reports'][0]['tests'][0]['status'] == 'failed'
    assert 'secret-value' not in json.dumps(detail)
    # Existing consumers retain their full compatibility contract.
    assert len(request(server, 'ci/reports')[1]['reports']) == 2


def test_cached_test_projection_invalidates_on_report_observation_eviction_restore_and_time():
    now = [CI_NOW]
    repo = ControlPlaneRepo(now=lambda: now[0])
    def upload(number, **values):
        repo.ingest_ci_report({**ci_run(number), 'xml': '<testsuite><testcase name="case"/></testsuite>', **values})
    upload(1, provider_url=None)
    assert repo.ci_test_history()['scope']['unjoined_report_count'] == 1
    repo.observe_ci_pipeline(ci_run())
    assert repo.ci_test_history()['tests'][0]['passed'] == 1
    upload(2)
    saved = repo.export_state()
    assert repo.ci_test_history()['tests'][0]['passed'] == 2
    repo.observe_ci_pipeline({**ci_run(), 'provider_url': 'https://other.example'})
    assert repo.ci_test_history()['tests'][0]['passed'] == 1  # legacy binding is now ambiguous
    repo.import_state(saved)
    assert repo.ci_test_history()['tests'][0]['passed'] == 2
    now[0] += 31 * 86400
    assert repo.ci_test_history()['tests'] == []
    now[0] = CI_NOW
    repo.import_state({})
    assert repo.ci_test_history()['tests'] == []


@pytest.mark.parametrize('query', [
    'compact=2', 'compact=1&compact=0', 'run_number=1',
    'provider_url=https%3A%2F%2Fuser%3Asecret%40ci.example&provider_repo_id=17&repository=org%2Fapp&run_number=1',
    'provider_url=https%3A%2F%2Fci.example&provider_repo_id=17&repository=org%2Fapp&run_number=0',
])
def test_report_projections_reject_invalid_or_partial_run_identity(server, query):
    assert request(server, 'ci/reports?' + query)[0] == 400
    assert request(server, 'ci/reports?compact=1', key='invalid')[0] == 401


def test_source_eviction_removes_warm_cached_report_evidence(monkeypatch):
    from herdr_engineering.control_plane import ci_reports

    repo = ControlPlaneRepo(now=lambda: CI_NOW)
    for number in (1, 2):
        repo.ingest_ci_report({**ci_run(number), 'xml': '<testsuite><testcase name="case"/></testsuite>'})
    assert repo.ci_test_history()['tests'][0]['passed'] == 2
    monkeypatch.setattr(ci_reports, 'REPORT_LIMIT', 1)
    repo._trim_ci_history('ci_report')
    view = repo.ci_test_history()
    assert view['tests'][0]['passed'] == 1
    assert view['tests'][0]['representative_runs'][0]['run_number'] == 2
    assert view['scope']['source_truncated'] is True


def test_compact_legacy_counts_bind_only_unambiguous_observed_provider(server):
    server.repo._now = lambda: CI_NOW
    upload_ci_report(server, provider_url=None)
    server.repo.observe_ci_pipeline(ci_run())
    compact = request(server, 'ci/reports?compact=1')[1]
    assert compact['reports'][0]['provider_url'] == 'https://ci.example'
    server.repo.observe_ci_pipeline({**ci_run(), 'provider_url': 'https://other.example'})
    assert request(server, 'ci/reports?compact=1')[1]['reports'][0].get('provider_url') is None
