"""Final route contracts: detached credentials, event scope and bucket membership."""
import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from tests.control_plane import test_ci_api as api

server = api.server


@pytest.mark.parametrize('field', ['state', 'status'])
@pytest.mark.parametrize('source', ['history', 'retained'])
def test_summary_outcome_keys_never_expose_credentials_and_preserve_counts(server, field, source):
    credential = 'Z9q7V2m4R8t6N3p5L1w0'
    states = ['success', 'passed', 'failure', 'failed', 'error', 'errored', 'timed_out',
              'crashed', 'running', 'started', 'queued', 'pending', 'waiting', 'blocked',
              'skipped', 'canceled', 'cancelled', 'killed', 'declined', 'unknown',
              credential, f'provider-error-{credential}', 'unsupported', None]
    rows = []
    for number, state in enumerate(states, 1):
        row = api.ci_run(number)
        row.pop('state')
        rows.append({**row, field: state})
    history = api.seed_ci(server, rows if source == 'history' else [])
    if source == 'retained':
        for row in rows:
            server.repo.observe_ci_pipeline(row)
    server.ci_monitor._factory = lambda: SimpleNamespace(token=credential)
    before = server.repo.export_state()
    status, view = api.request(server, 'ci/summary?scope=all')
    assert status == 200
    assert credential not in json.dumps(view)
    assert len(view['projects']) == len(view['charts']) == 1
    for metrics in [view['kpis'], *view['projects'], *view['charts']]:
        assert metrics['outcomes'] == dict(success=2, failure=6, running=2, queued=3,
                                          blocked=1, skipped=1, canceled=4, unknown=5)
        assert sum(metrics['outcomes'].values()) == metrics['pipeline_count'] == 24
        assert metrics['completed_count'] == metrics['duration_seconds']['sample_count'] == 8
        assert metrics['active_count'] == 6
        assert metrics['pass_rate'] == .25
    assert server.repo.export_state() == before
    assert history._view['pipelines'] == (rows if source == 'history' else [])


@pytest.mark.parametrize('credential', ['Z9q7V2m4R8t6N3p5L1w0', 'opaque-\"quoted\\value'])
@pytest.mark.parametrize('field', ['provider_url', 'repository', 'provider_repo_id'])
def test_summary_redacts_retained_identity_without_rewriting_evidence(server, field, credential):
    row = {**api.ci_run(), field: ('https://ci.example/' if field == 'provider_url' else '') + credential}
    history = api.seed_ci(server, [row])
    server.repo.observe_ci_pipeline(row)
    server.ci_monitor._factory = lambda: SimpleNamespace(token=credential)
    before = server.repo.export_state()
    status, view = api.request(server, 'ci/summary')
    assert status == 200
    assert credential not in json.dumps(view)
    for project in view['projects']:
        assert all(credential not in value for value in json.loads(project['project_id']))
    assert server.repo.export_state() == before
    assert history._view['pipelines'] == [row]


@pytest.mark.parametrize('scope,numbers', [('all', [1, 2]), ('pr', [1]), ('default', [2])])
def test_summary_and_tests_share_event_scope_and_half_open_creation_bucket(server, scope, numbers):
    # Reports arrive today for runs from yesterday; membership uses run creation.
    rows = [{**api.ci_run(n), 'event': event, 'created_at': created}
            for n, event, created in [
                (1, 'pull_request', '2026-10-24T00:00:00Z'),
                (2, 'push', '2026-10-24T12:00:00Z'),
                (3, 'pull_request', '2026-10-25T00:00:00Z'),
                (4, 'pull_request', '2026-10-23T23:59:59Z')]]
    api.seed_ci(server, rows)
    for row in rows:
        api.upload_ci_report(server, number=row['run_number'], xml=(
            f'<testsuite><testcase name="run-{row["run_number"]}"><failure/></testcase></testsuite>'))
    query = urlencode(dict(days=7, scope=scope, start='2026-10-24T00:00:00Z', end='2026-10-25T00:00:00Z'))
    status, summary = api.request(server, 'ci/summary?' + query)
    assert status == 200
    assert summary['kpis']['pipeline_count'] == len(numbers)
    status, tests = api.request(server, 'ci/tests?' + query)
    assert status == 200
    assert sorted(row['name'] for row in tests['tests']) == [f'run-{n}' for n in numbers]
    assert tests['scope']['not_reported_run_count'] == 0
    assert tests['scope']['report_count'] == len(numbers)


def test_selected_current_day_http_window_advances_to_now_but_never_beyond_bucket(server):
    rows = [{**api.ci_run(number), 'created_at': created}
            for number, created in [(1, '2026-10-25T11:00:00Z'),
                                    (2, '2026-10-25T13:00:00Z'),
                                    (3, '2026-10-26T00:00:00Z')]]
    api.seed_ci(server, rows)
    for row in rows:
        api.upload_ci_report(server, number=row['run_number'], xml=(
            f'<testsuite><testcase name="run-{row["run_number"]}"/></testsuite>'))
    query = urlencode(dict(days=7, scope='all', start='2026-10-25T00:00:00.000Z',
                           end='2026-10-26T00:00:00.000Z'))
    for now, numbers in [(api.CI_NOW, [1]), (api.CI_NOW + 7200, [1, 2]),
                         (api.CI_NOW + 13 * 3600, [1, 2])]:
        server.repo._now = lambda: now
        status, summary = api.request(server, 'ci/summary?' + query)
        assert status == 200
        assert summary['kpis']['pipeline_count'] == len(numbers)
        status, tests = api.request(server, 'ci/tests?' + query)
        assert status == 200
        assert sorted(test['name'] for test in tests['tests']) == [f'run-{n}' for n in numbers]
        assert tests['scope']['report_count'] == len(numbers)


@pytest.mark.parametrize('days', [7, 14, 30])
def test_missing_report_runs_use_creation_calendar_days(server, days):
    start = api.CI_NOW // 86400 * 86400 - (days - 1) * 86400
    rows = [{**api.ci_run(n), 'created_at': datetime.fromtimestamp(stamp, UTC).isoformat()}
            for n, stamp in enumerate([start - 1, start, start + 1, api.CI_NOW, api.CI_NOW + 1], 1)]
    rows.append({**api.ci_run(6), 'created_at': None})
    api.seed_ci(server, rows)
    for row in rows:
        server.repo.observe_ci_pipeline(row)
    status, tests = api.request(server, f'ci/tests?days={days}')
    assert status == 200
    assert tests['scope']['not_reported_run_count'] == 3
    assert tests['scope']['time_basis'] == 'reported_at'


@pytest.mark.parametrize('query', ['start=garbage&end=2026-10-25T00:00:00Z',
    'start=2026-10-24T00:00:00Z', 'end=2026-10-25T00:00:00Z',
    'start=2026-10-25T00:00:00Z&end=2026-10-24T00:00:00Z', 'scope=bogus'])
@pytest.mark.parametrize('route', ['summary', 'tests'])
def test_selection_routes_reject_invalid_bounds_and_scope(server, route, query):
    api.seed_ci(server, [])
    assert api.request(server, f'ci/{route}?{query}')[0] == 400


def test_last_runs_selection_keeps_exact_old_run_and_reports_without_calendar_cutoff(server):
    rows = [{**api.ci_run(n), 'created_at': '2026-08-01T12:00:00Z'} for n in range(1, 27)]
    rows += [{**rows[-1], 'provider_repo_id': 99},
             {**rows[-1], 'provider_url': 'https://other.example'},
             {**rows[-1], 'repository': 'org/renamed'}]
    api.seed_ci(server, rows)
    for n in (25, 26):
        api.upload_ci_report(server, number=n, reported_at='2026-08-01T13:00:00Z',
                             xml=f'<testsuite><testcase name="old-{n}"/></testsuite>')
    identity = {key: rows[0][key] for key in ('provider_url', 'provider_repo_id', 'repository')}
    query = urlencode(dict(mode='runs', run_number=26, **identity))
    before = server.repo.export_state()
    status, summary = api.request(server, 'ci/summary?' + query)
    assert status == 200
    assert summary['kpis']['pipeline_count'] == 1
    assert summary['scope']['mode'] == 'runs'
    assert summary['scope']['days'] is None
    status, tests = api.request(server, 'ci/tests?' + query)
    assert status == 200
    assert [row['name'] for row in tests['tests']] == ['old-26']
    assert tests['scope']['report_count'] == 1
    assert tests['scope']['not_reported_run_count'] == 0
    assert server.repo.export_state() == before
    status, summary = api.request(server, 'ci/summary?' + urlencode(dict(mode='runs', **identity)))
    assert status == 200
    assert summary['kpis']['pipeline_count'] == 24
    status, tests = api.request(server, 'ci/tests?' + urlencode(dict(mode='runs', **identity)))
    assert status == 200
    assert tests['scope']['not_reported_run_count'] == 22
    # Search may expose a run outside the unfiltered last 24; exact selection wins.
    status, summary = api.request(server, 'ci/summary?' + urlencode(dict(mode='runs', run_number=1, **identity)))
    assert status == 200
    assert summary['kpis']['pipeline_count'] == 1


@pytest.mark.parametrize('route', ['summary', 'tests'])
@pytest.mark.parametrize('query', ['mode=bogus', 'mode=runs&run_number=1',
    'mode=runs&run_number=-1', 'mode=runs&run_number=garbage'])
def test_run_selection_requires_valid_mode_and_exact_identity(server, route, query):
    api.seed_ci(server, [])
    assert api.request(server, f'ci/{route}?{query}')[0] == 400


def test_hour_mode_uses_trailing_24_hours_for_summary_and_tests(server):
    rows = [{**api.ci_run(n), 'created_at': datetime.fromtimestamp(stamp, UTC).isoformat()}
            for n, stamp in enumerate([api.CI_NOW - 86401, api.CI_NOW - 86400,
                                       api.CI_NOW, api.CI_NOW + 1], 1)]
    api.seed_ci(server, rows)
    for row in rows:
        api.upload_ci_report(server, number=row['run_number'],
                             xml=f'<testsuite><testcase name="hour-{row["run_number"]}"/></testsuite>')
    status, summary = api.request(server, 'ci/summary?mode=hours')
    assert status == 200
    assert summary['scope']['days'] is None
    assert summary['kpis']['pipeline_count'] == 2
    status, tests = api.request(server, 'ci/tests?mode=hours')
    assert status == 200
    assert sorted(row['name'] for row in tests['tests']) == ['hour-2', 'hour-3']
    assert tests['scope']['report_count'] == 2


@pytest.mark.parametrize('date,expected', [
    ('2026-10-25', [[1, 2], [1, 2, 3, 4], [1, 2, 3, 4, 5], [2, 3, 4, 5], []]),
    ('2026-10-24', [[2, 3, 4, 5], [4, 5], [], [], []]),
])
def test_selected_hour_stays_shared_across_refresh_rollover_and_expiry(server, date, expected):
    rows = [{**api.ci_run(n), 'created_at': f'{date}T{clock}Z'}
            for n, clock in enumerate(['12:29:00', '12:30:00', '12:40:00',
                                       '12:45:00', '12:59:59', '13:00:00'], 1)]
    api.seed_ci(server, rows)
    for row in rows:
        api.upload_ci_report(server, number=row['run_number'],
                             xml=f'<testsuite><testcase name="hour-{row["run_number"]}"/></testsuite>')
    # Capture actual clicked bounds once; every later consumer retains those bounds.
    script = """
      import {ciHealthModel} from './packages/ci-health.js';
      import {filterCiObservations} from './packages/ci-insights.js';
      import {integrationEntries,selectIntegrations} from './packages/integrations-view.js';
      let input=''; for await (const chunk of process.stdin) input+=chunk;
      const {rows,date}=JSON.parse(input), initial=Date.parse('2026-10-25T12:30:00Z');
      const history={pipelines:rows,repos:[{id:17,repository:'org/app',provider_url:'https://ci.example',default_branch:'main'}]};
      const first=ciHealthModel(history,{mode:'hours',scope:'all'},initial),project=first.projects[0];
      const cell=project.cells.find(c=>c.key===`hour:${Date.parse(date+'T12:00:00Z')}`);
      const selection={projectKey:project.key,bucketKey:cell.key};
      const filters={mode:'hours',scope:'all',start:new Date(cell.start).toISOString(),end:new Date(cell.end).toISOString()};
      const clocks=['2026-10-25T12:30:00Z','2026-10-25T12:45:00Z','2026-10-25T13:00:00Z','2026-10-26T12:30:00Z','2026-10-26T13:00:00Z'];
      const snapshots=clocks.map(clock=>{
        const now=Date.parse(clock); Date.now=()=>now;
        const model=ciHealthModel(history,{mode:'hours',scope:'all',selection},now);
        return {now:now/1000,health:model.selectedRows.map(r=>r.run_number),
          tests:filterCiObservations(history,filters,now).pipelines.map(r=>r.run_number),
          runs:selectIntegrations(integrationEntries({pipelines:{runs:rows}}),{...filters,tab:'all'}).map(e=>e.record.run_number).sort((a,b)=>a-b)};
      });
      console.log(JSON.stringify({filters,snapshots}));
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script],
                            input=json.dumps({'rows': rows, 'date': date}),
                            text=True, capture_output=True, check=True)
    view = json.loads(result.stdout)
    query = urlencode(view['filters'])
    for snapshot, numbers in zip(view['snapshots'], expected, strict=True):
        server.repo._now = lambda: snapshot['now']
        status, summary = api.request(server, 'ci/summary?' + query)
        assert status == 200
        assert summary['kpis']['pipeline_count'] == len(numbers)
        status, tests = api.request(server, 'ci/tests?' + query)
        assert status == 200
        assert sorted(row['name'] for row in tests['tests']) == [f'hour-{n}' for n in numbers]
        assert tests['scope']['report_count'] == len(numbers)
        for consumer in ['health', 'tests', 'runs']:
            assert snapshot[consumer] == numbers, (date, snapshot['now'], consumer)
