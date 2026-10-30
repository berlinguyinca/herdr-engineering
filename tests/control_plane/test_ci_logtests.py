"""Test counts read from CI log text. The excerpts in tests/fixtures/ci_logs are trimmed from real Woodpecker step logs
(paths, hosts and organisation names replaced); the runners marked synthetic below are written from each runner's documented
output because no log of them was available."""
from pathlib import Path

import pytest

from herdr_engineering.control_plane.ci_logtests import (
    MAX_INDEX,
    clean_lines,
    combine_steps,
    parse_step_log,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'ci_logs'


def parsed(name):
    return parse_step_log((FIXTURES / name).read_text(), 'step')


@pytest.mark.parametrize('name, runner, counts', [
    ('pytest.log', 'pytest', (411, 1, 56, 0)),
    ('node-tap.log', 'node-test', (784, 0, 0, 0)),
    ('node-tap-failure.log', 'node-test', (291, 7, 0, 0)),
    ('cargo.log', 'cargo', (3, 0, 0, 0)),
    ('go-verbose.log', 'go', (4, 1, 0, 0)),
    ('scalatest.log', 'scalatest', (1, 0, 0, 0)),
    ('vitest.log', 'jest', (721, 0, 0, 0)),
    ('karma.log', 'karma', (30, 0, 0, 0)),
    ('unittest.log', 'unittest', (16, 0, 0, 0)),
])
def test_real_excerpts_give_the_runners_own_totals(name, runner, counts):
    result = parsed(name)
    assert result['runners'] == [runner]
    assert tuple(result['counts'][k] for k in ('passed', 'failed', 'errored', 'skipped')) == counts


def test_go_verbose_index_names_each_test_with_its_package_and_log_lines():
    tests = {t['name']: t for t in parsed('go-verbose.log')['tests']}
    failing = tests['TestCompoundReaderUsesReadOnlyTransactionAndThreeSecondTimeout']
    assert failing['status'] == 'failed' and failing['duration_ms'] == 32990
    assert failing['suite'].endswith('go-query/query/assistant')
    assert failing['line_start'] == 4 and failing['line_end'] == 8
    assert tests['TestBackfillCoordinatorPublishesGroupsAndAdvancesKeyset']['status'] == 'passed'


def test_tap_index_covers_a_failing_subtest_block_with_its_duration():
    failing = [t for t in parsed('node-tap-failure.log')['tests'] if t['status'] == 'failed'][0]
    assert failing['name'].startswith('the baselines do not depend on the host')
    assert failing['duration_ms'] == 154577
    assert failing['line_end'] - failing['line_start'] > 10      # the whole `---` block with the error text
    assert parsed('node-tap-failure.log')['complete'] is False   # an excerpt: fewer entries than the totals


def test_a_trimmed_log_is_not_called_complete():
    assert parsed('node-tap.log')['complete'] is False
    assert parsed('pytest.log')['complete'] is False


def test_pytest_failure_summary_names_the_failing_test():
    test = parsed('pytest.log')['tests'][0]
    assert (test['status'], test['suite']) == ('failed', 'tests/unit/test_dagster_pipeline.py')
    assert test['name'].startswith('TestReferenceLibraryIngestSensor.test_run_key')


def test_vitest_file_lines_are_not_tests():
    assert parsed('vitest.log')['tests'] == []


def test_output_without_a_runner_summary_is_not_guessed_at():
    assert parse_step_log('Tests are fun\n12 passed\nok  all good\n', 'x') is None
    assert parse_step_log('', 'x') is None and parse_step_log(None, 'x') is None


def test_ansi_sequences_never_hide_a_summary_and_never_move_line_numbers():
    text = '\x1b[32mrunning 2 tests\x1b[0m\ntest a ... \x1b[32mok\x1b[0m\ntest b ... ok\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\r\n'
    assert len(clean_lines(text)) == len(text.split('\n'))
    result = parse_step_log(text, 'x')
    assert result['counts']['passed'] == 2 and [t['line_start'] for t in result['tests']] == [2, 3]


def test_every_run_in_a_step_is_summed_not_just_the_last():
    two = 'Ran 16 tests in 0.1s\nOK\nRan 21 tests in 0.2s\nFAILED (failures=2, errors=1)\n'
    assert parse_step_log(two, 'x')['counts'] == {'passed': 34, 'failed': 2, 'errored': 1, 'skipped': 0}
    vitest = '      Tests  721 passed (721)\n   Duration 1s\n      Tests  703 passed | 1 skipped (704)\n'
    assert parse_step_log(vitest, 'x')['counts'] == {'passed': 1424, 'failed': 0, 'errored': 0, 'skipped': 1}


# Synthetic: written from each runner's documented output.
@pytest.mark.parametrize('text, runner, counts, names', [
    ('[INFO] Running a.B\n[ERROR] testBad  Time elapsed: 0.01 s  <<< FAILURE!\n[WARNING] Tests run: 5, Failures: 1, Errors: 0, Skipped: 1\n',
     'surefire', (3, 1, 0, 1), ['testBad']),
    ('a.FooTest > testOk PASSED\na.FooTest > testBad FAILED\n\n5 tests completed, 1 failed, 1 skipped\n',
     'gradle', (3, 1, 0, 1), ['testOk', 'testBad']),
    ('1..3\nok 1 first works\nnot ok 2 second fails\nok 3 third # skip not on ci\n', 'tap', (1, 1, 0, 1),
     ['first works', 'second fails', 'third']),
    ('PASS src/a.test.js\n    ✓ adds (3 ms)\n    ✕ subtracts (1 ms)\nTests:       1 failed, 1 passed, 2 total\n',
     'jest', (1, 1, 0, 0), ['adds', 'subtracts']),
    ('t.py::test_ok PASSED   [ 50%]\nt.py::test_bad FAILED  [100%]\n===== 1 failed, 1 passed in 0.02s =====\n',
     'pytest', (1, 1, 0, 0), ['test_ok', 'test_bad']),
    ('running 2 tests\ntest a ... ok\ntest b ... FAILED\ntest result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n',
     'cargo', (1, 1, 0, 0), ['a', 'b']),
])
def test_other_runners(text, runner, counts, names):
    result = parse_step_log(text, 'x')
    assert result['runners'] == [runner]
    assert tuple(result['counts'][k] for k in ('passed', 'failed', 'errored', 'skipped')) == counts
    assert [t['name'] for t in result['tests']] == names


def test_steps_of_one_run_are_summed_and_indexed_by_step():
    a = parse_step_log('running 1 test\ntest a ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n', 'rust')
    b = parse_step_log('Ran 4 tests in 1s\nOK\n', 'scripts')
    combined = combine_steps([(11, 'rust', a), (12, 'lint', None), (13, 'scripts', b)])
    assert combined['summary'] == {'passed': 5, 'failed': 0, 'errored': 0, 'skipped': 0, 'total': 5}
    assert combined['runners'] == ['cargo', 'unittest']
    assert combined['tests'][0]['step_id'] == '11' and combined['tests'][0]['step'] == 'rust'
    assert combine_steps([(1, 'x', None)]) is None


def test_the_index_is_capped_and_says_so():
    text = 'running 6000 tests\n' + ''.join(f'test t{i} ... ok\n' for i in range(6000)) + \
        'test result: ok. 6000 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n'
    result = parse_step_log(text, 'x')
    assert result['counts']['passed'] == 6000 and len(result['tests']) == MAX_INDEX and result['complete'] is False
