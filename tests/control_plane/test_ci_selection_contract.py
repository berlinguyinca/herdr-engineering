"""Final route contracts: detached credentials, event scope and bucket membership."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from tests.control_plane import test_ci_api as api

server = api.server


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
