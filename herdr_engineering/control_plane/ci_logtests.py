"""Test counts and a per-test index read from the text of a CI step log.

Pure and side-effect free: text in, numbers and line ranges out. Nothing here keeps log text; the index holds names,
statuses, durations and 1-based line ranges only, so a later reader can slice the live log. A runner is recognised only
by its own distinctive summary line, and output that matches no parser yields ``None`` rather than a guess.
"""
from __future__ import annotations

import bisect
import re
import time

MAX_INDEX = 5000          # per-test entries kept for one step
MAX_NAME = 300
MAX_LINE = 2000
TAP_YAML_LINES = 80       # a TAP diagnostic block longer than this is not read to its end
PARSE_SECONDS = 20.0      # per step: parsers run one after another and the rest are skipped once this is spent
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\r')
STATES = ('passed', 'failed', 'errored', 'skipped')


def clean_lines(text):
    """Lines with terminal control sequences removed; the count of lines is unchanged, so numbers stay valid."""
    # Very long lines (minified output, progress bars) are cut: no summary line is that long, and the patterns stay fast.
    return [ANSI.sub('', line[:MAX_LINE]) for line in str(text or '').split('\n')]


def _entry(name, status, line, end=None, suite='', duration_ms=None):
    return {'name': name.strip()[:MAX_NAME], 'suite': suite.strip()[:MAX_NAME], 'status': status,
            'duration_ms': duration_ms, 'line_start': line, 'line_end': end or line}


def _counts(**kw):
    return {state: int(kw.get(state, 0)) for state in STATES}


# --- pytest -----------------------------------------------------------------------------------------------------------------
_PYTEST_SUMMARY = re.compile(r'^=+ (.*?) in [\d.]+s?(?: \(.*?\))? =+$')
_PYTEST_WORD = re.compile(r'(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)')
_PYTEST_STATUS = re.compile(r' (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b')
_PYTEST_NODE = re.compile(r'::\S')


def _pytest_verbose(text):
    """(node id, status) of a `pytest -v` line, or None. Plain string steps: no regex backtracks over the whole line."""
    if '::' not in text or text[:1].isspace():
        return None
    m = _PYTEST_STATUS.search(text)
    if m is None or not _PYTEST_NODE.search(text[:m.start()].split(' ', 1)[0]):
        return None
    return text[:m.start()], m.group(1)


_PYTEST_FAILED = re.compile(r'^(FAILED|ERROR) (\S+::\S.*?)(?: - .*)?$')
_PYTEST_HEAD = re.compile(r'^_{3,} (.+?) _{3,}$')


def parse_pytest(lines):
    totals = None
    for text in lines:                  # a step may run pytest more than once; each run prints its own summary
        if not (text.startswith('=') and text.endswith('=')):
            continue
        m = _PYTEST_SUMMARY.match(text.strip())
        if m and _PYTEST_WORD.search(m.group(1)):
            words = {}
            for count, word in _PYTEST_WORD.findall(m.group(1)):
                words[word.rstrip('s') if word.startswith('error') else word] = int(count)
            run = _counts(passed=words.get('passed', 0) + words.get('xpassed', 0), failed=words.get('failed', 0),
                          errored=words.get('error', 0), skipped=words.get('skipped', 0) + words.get('xfailed', 0))
            totals = run if totals is None else {state: totals[state] + run[state] for state in STATES}
    if totals is None:
        return None
    heads = [(i + 1, m.group(1)) for i, text in enumerate(lines) if text.startswith('___') and (m := _PYTEST_HEAD.match(text))]
    tests, seen = [], set()
    for i, text in enumerate(lines):
        verbose = _pytest_verbose(text)
        if verbose:
            node, word = verbose
            status = {'PASSED': 'passed', 'XPASS': 'passed', 'FAILED': 'failed', 'ERROR': 'errored'}.get(word, 'skipped')
            seen.add(node)
            tests.append(_entry(node.split('::', 1)[-1].replace('::', '.'), status, i + 1, suite=node.split('::', 1)[0]))
    for i, text in enumerate(lines):
        m = _PYTEST_FAILED.match(text) if text[:1] in 'FE' and text else None
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            tests.append(_entry(m.group(2).split('::', 1)[-1].replace('::', '.'),
                                'failed' if m.group(1) == 'FAILED' else 'errored', i + 1, suite=m.group(2).split('::', 1)[0]))
    # A failing test's own output block (the `____ name ____` section) is the range worth reading.
    def head_key(name):
        short = name.split('.')[-1]
        return short.split('[')[0], short.split('[')[-1]
    by_key = {}
    for index, (line, name) in enumerate(heads):
        by_key.setdefault((name.split('.')[-1].split('[')[0], name.split('[')[-1]), index)
    rules = [i + 1 for i, text in enumerate(lines) if text.startswith('=====')]       # 1-based, ascending
    for t in tests:
        if t['status'] in ('failed', 'errored'):
            index = by_key.get(head_key(t['name']))
            if index is not None:
                line = heads[index][0]
                nxt = heads[index + 1][0] if index + 1 < len(heads) else None
                if nxt is None:
                    at = bisect.bisect_right(rules, line)           # first rule below the head (0-based list index == line)
                    nxt = rules[at] if at < len(rules) else len(lines) + 1
                t['line_start'], t['line_end'] = line, nxt - 1
    total = sum(totals.values())
    return {'runner': 'pytest', 'counts': totals, 'tests': tests, 'complete': len(tests) == total}


# --- TAP: node --test, bats, prove -----------------------------------------------------------------------------------------
_TAP_SUM = re.compile(r'^# (tests|suites|pass|fail|cancelled|skipped|todo) (\d+)\s*$')
_TAP_RESULT = re.compile(r'^(\s*)(not ok|ok) (\d+)(?: - | )?(.*)$')
_TAP_PLAN = re.compile(r'^\d+\.\.(\d+)\s*$')


def _tap_entries(lines):
    results = []          # (index, indent, ok, name, directive)
    for i, text in enumerate(lines):
        m = _TAP_RESULT.match(text)
        if m:
            name, directive = m.group(4), ''
            if re.search(r'\s#\s*(skip|todo)', name, re.I):
                name, _, directive = re.split(r'\s#\s*', name, 1)[0], '', 'skip'
            results.append((i, len(m.group(1)), m.group(2) == 'ok', name, directive))
    tests = []
    for n, (i, indent, ok, name, directive) in enumerate(results):
        # A parent's own line follows its children (the preceding result is deeper); it is a suite, not a test.
        if n > 0 and results[n - 1][1] > indent:
            continue
        end, duration = i + 1, None
        j = i + 1
        if j < len(lines) and lines[j].strip() == '---':
            # The diagnostic block is indented under its result and closed by `...`. Stop at the first line that is not
            # inside it (or after a bounded number of lines) so a missing `...` cannot make every result read to the end.
            limit = min(len(lines), j + TAP_YAML_LINES)
            j += 1
            closed = False
            while j < limit:
                line = lines[j]
                if line.strip() == '...':
                    closed = True
                    break
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                if 'duration_ms:' in line:
                    m = re.match(r'^\s*duration_ms: ([\d.]+)', line)
                    if m:
                        duration = round(float(m.group(1)))
                j += 1
            end = min(j + 1, len(lines)) if closed else j
        tests.append(_entry(name or f'test {n + 1}', 'skipped' if directive else ('passed' if ok else 'failed'), i + 1, end,
                            duration_ms=duration))
    return tests


def parse_tap(lines):
    groups, current = [], None
    for text in lines:
        m = _TAP_SUM.match(text)
        if not m:
            continue
        key, value = m.group(1), int(m.group(2))
        if key == 'tests':
            current = {'tests': value}
            groups.append(current)
        elif current is not None:
            current[key] = value
    runner = 'node-test'
    if not groups:
        plan = [int(m.group(1)) for text in lines if (m := _TAP_PLAN.match(text))]
        tests = _tap_entries(lines)
        if not plan or not tests:
            return None
        top = [t for t in tests if not lines[t['line_start'] - 1].startswith(' ')]
        counts = _counts(passed=sum(t['status'] == 'passed' for t in top), failed=sum(t['status'] == 'failed' for t in top),
                         skipped=sum(t['status'] == 'skipped' for t in top))
        return {'runner': 'tap', 'counts': counts, 'tests': top, 'complete': True}
    counts = _counts()
    for g in groups:
        bad = g.get('fail', 0)
        skipped = g.get('skipped', 0) + g.get('todo', 0)
        cancelled = g.get('cancelled', 0)
        counts['failed'] += bad + cancelled
        counts['skipped'] += skipped
        counts['passed'] += max(0, g['tests'] - bad - skipped - cancelled)
    tests = _tap_entries(lines)
    return {'runner': runner, 'counts': counts, 'tests': tests, 'complete': len(tests) == sum(counts.values())}


# --- cargo test -------------------------------------------------------------------------------------------------------------
_CARGO_RESULT = re.compile(r'^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;')
_CARGO_TEST = re.compile(r'^test (\S+) \.\.\. (ok|FAILED|ignored)(?:, .*)?$')
_CARGO_RUN = re.compile(r'^\s*(?:Running|Doc-tests) (.+?)(?: \(.*\))?$')
_CARGO_FAIL_HEAD = re.compile(r'^---- (\S+) stdout ----$')


def parse_cargo(lines):
    counts, seen = _counts(), False
    for text in lines:
        m = _CARGO_RESULT.match(text)
        if m:
            seen = True
            counts['passed'] += int(m.group(1))
            counts['failed'] += int(m.group(2))
            counts['skipped'] += int(m.group(3))
    if not seen:
        return None
    tests, suite = [], ''
    heads = {m.group(1): i + 1 for i, text in enumerate(lines) if (m := _CARGO_FAIL_HEAD.match(text))}
    boundaries = sorted([i + 1 for i, text in enumerate(lines) if _CARGO_FAIL_HEAD.match(text) or text.startswith('failures:')
                         or text.startswith('test result:')])
    for i, text in enumerate(lines):
        run = _CARGO_RUN.match(text)
        if run:
            suite = run.group(1)
            continue
        m = _CARGO_TEST.match(text)
        if m:
            status = {'ok': 'passed', 'FAILED': 'failed', 'ignored': 'skipped'}[m.group(2)]
            start, end = i + 1, i + 1
            if status == 'failed' and m.group(1) in heads:
                start = heads[m.group(1)]
                end = next((b - 1 for b in boundaries if b > start), len(lines))
            tests.append(_entry(m.group(1), status, start, end, suite=suite))
    return {'runner': 'cargo', 'counts': counts, 'tests': tests, 'complete': len(tests) == sum(counts.values())}


# --- go test -v -------------------------------------------------------------------------------------------------------------
_GO_RESULT = re.compile(r'^--- (PASS|FAIL|SKIP): (\S+) \(([\d.]+)s\)')
_GO_RUN = re.compile(r'^=== RUN\s+(\S+)$')
_GO_PKG = re.compile(r'^(?:ok|FAIL)\s+(\S+)\s')


def parse_go(lines):
    """Only `go test -v` output has per-test lines; package-only output is not a test count and yields None."""
    tests, runs = [], {}
    pending = []
    for i, text in enumerate(lines):
        m = _GO_RUN.match(text)
        if m:
            runs.setdefault(m.group(1), i + 1)
            continue
        m = _GO_RESULT.match(text)
        if m:
            end = i + 1
            while end < len(lines) and (lines[end].startswith(' ') or lines[end].startswith('\t')):
                end += 1
            status = {'PASS': 'passed', 'FAIL': 'failed', 'SKIP': 'skipped'}[m.group(1)]
            entry = _entry(m.group(2), status, runs.get(m.group(2), i + 1), end, duration_ms=round(float(m.group(3)) * 1000))
            pending.append(entry)
            tests.append(entry)
            continue
        m = _GO_PKG.match(text)
        if m:
            for entry in pending:
                entry['suite'] = m.group(1)
            pending = []
    if not tests:
        return None
    counts = _counts()
    for t in tests:
        counts[t['status']] += 1
    return {'runner': 'go', 'counts': counts, 'tests': tests, 'complete': True}


# --- jest / vitest ----------------------------------------------------------------------------------------------------------
_JEST_SUM = re.compile(r'^\s*Tests:\s+(.*?)(?:,\s*)?(\d+) total\s*$')
_VITEST_SUM = re.compile(r'^\s*Tests\s{2,}(.*?)\((\d+)\)\s*$')
_JS_WORD = re.compile(r'(\d+) (passed|failed|skipped|todo|pending)')
_JS_FILE = re.compile(r'\(\d+ tests?\)')       # vitest's per-file line, not a test
_JS_TEST = re.compile(r'^\s+(✓|✔|√|✕|✗|×|○|-)\s+(.+?)(?: \((\d+) ?ms\))?$')


def parse_jest(lines):
    counts, found = _counts(), False
    for text in lines:                  # one summary per run; a step may run several suites
        m = _JEST_SUM.match(text) or _VITEST_SUM.match(text)
        if m:
            found = True
            words = {w: int(n) for n, w in _JS_WORD.findall(m.group(1))}
            counts['passed'] += words.get('passed', 0)
            counts['failed'] += words.get('failed', 0)
            counts['skipped'] += words.get('skipped', 0) + words.get('todo', 0) + words.get('pending', 0)
    if not found:
        return None
    tests = []
    for i, text in enumerate(lines):
        m = _JS_TEST.match(text)
        if m and m.group(1) != '-' and not _JS_FILE.search(m.group(2)):
            status = {'✓': 'passed', '✔': 'passed', '√': 'passed', '○': 'skipped'}.get(m.group(1), 'failed')
            tests.append(_entry(m.group(2), status, i + 1, duration_ms=int(m.group(3)) if m.group(3) else None))
    return {'runner': 'jest', 'counts': counts, 'tests': tests, 'complete': len(tests) == sum(counts.values())}


# --- JVM: surefire, ScalaTest/sbt, gradle -----------------------------------------------------------------------------------
_SURE = re.compile(r'^(?:\[(?:INFO|ERROR|WARNING)\] )?Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$')
_SCALA = re.compile(r'^Tests: succeeded (\d+), failed (\d+), canceled (\d+), ignored (\d+), pending (\d+)')
_SCALA_SUITE = re.compile(r'^([A-Z][\w$]*(?:\.[A-Za-z_][\w$]*)*):$')
_SCALA_TEST = re.compile(r'^\s*- (.+?)( \*\*\* (FAILED|IGNORED) \*\*\*)?$')
_SURE_ELAPSED = re.compile(r'Time elapsed: ([\d.]+) s\s+<<< (FAILURE|ERROR)!')
_SURE_PREFIX = re.compile(r'^\[(?:ERROR|WARNING)\] ')


def _sure_failure(text):
    """(case, seconds, FAILURE|ERROR) of a surefire failure line. Split at the marker; no pattern scans the line twice."""
    at = text.find(' Time elapsed: ')
    if at < 1:
        return None
    m = _SURE_ELAPSED.match(text, at + 1)
    name = _SURE_PREFIX.sub('', text[:at].rstrip(), count=1)
    if m is None or not name or name[0].isspace():
        return None
    return name, m.group(1), m.group(2)
_GRADLE_TEST = re.compile(r'^(\S+) > (.+?) (PASSED|FAILED|SKIPPED)$')
_GRADLE = re.compile(r'^(\d+) tests completed, (\d+) failed(?:, (\d+) skipped)?')


def parse_jvm(lines):
    runners, counts, tests = [], _counts(), []
    sure = [m for text in lines if (m := _SURE.match(text))]
    if sure:
        runners.append('surefire')
        for m in sure:
            total, fails, errs, skips = (int(m.group(n)) for n in (1, 2, 3, 4))
            counts['passed'] += max(0, total - fails - errs - skips)
            counts['failed'] += fails
            counts['errored'] += errs
            counts['skipped'] += skips
    for i, text in enumerate(lines):
        f = _sure_failure(text)          # surefire prints only the failing cases; the passing ones are counted, not named
        if f:
            tests.append(_entry(f[0], 'failed' if f[2] == 'FAILURE' else 'errored', i + 1,
                                duration_ms=round(float(f[1]) * 1000)))
    scala = [m for text in lines if (m := _SCALA.match(text))]
    if scala:
        runners.append('scalatest')
        for m in scala:
            counts['passed'] += int(m.group(1))
            counts['failed'] += int(m.group(2)) + int(m.group(3))
            counts['skipped'] += int(m.group(4)) + int(m.group(5))
        suite = ''
        for i, text in enumerate(lines):
            s = _SCALA_SUITE.match(text) if text.endswith(':') else None
            # A suite header is followed by its tests (` - name`), possibly after a scope line; any other `Word:` is prose.
            if s and any(_SCALA_TEST.match(nxt) for nxt in lines[i + 1:i + 4]):
                suite = s.group(1)
                continue
            t = _SCALA_TEST.match(text)
            if t and suite:
                status = 'failed' if t.group(3) == 'FAILED' else 'skipped' if t.group(3) == 'IGNORED' else 'passed'
                tests.append(_entry(t.group(1), status, i + 1, suite=suite))
    for i, text in enumerate(lines):
        g = _GRADLE_TEST.match(text)
        if g:
            tests.append(_entry(g.group(2), {'PASSED': 'passed', 'FAILED': 'failed'}.get(g.group(3), 'skipped'), i + 1,
                                suite=g.group(1)))
    for text in lines:
        m = _GRADLE.match(text)
        if m:
            runners.append('gradle')
            counts['failed'] += int(m.group(2))
            counts['skipped'] += int(m.group(3) or 0)
            counts['passed'] += max(0, int(m.group(1)) - int(m.group(2)) - int(m.group(3) or 0))
            break
    if not runners:
        return None
    return {'runner': '+'.join(runners), 'counts': counts, 'tests': tests,
            'complete': bool(tests) and len(tests) == sum(counts.values())}


# --- Karma / Angular --------------------------------------------------------------------------------------------------------
_KARMA = re.compile(r'Executed (\d+) of (\d+)(?: \(skipped (\d+)\))?(?: \((\d+) FAILED\))?(?: \(skipped (\d+)\))?')


def parse_karma(lines):
    last = None
    for text in lines:
        m = _KARMA.search(text)
        if m:
            last = m
    if last is None:
        return None
    total, failed = int(last.group(2)), int(last.group(4) or 0)
    skipped = int(last.group(3) or last.group(5) or 0)
    return {'runner': 'karma', 'counts': _counts(passed=max(0, total - failed - skipped), failed=failed, skipped=skipped),
            'tests': [], 'complete': False}


# --- Python unittest ---------------------------------------------------------------------------------------------------------
_UT_RAN = re.compile(r'^Ran (\d+) tests? in [\d.]+s\s*$')
_UT_V = re.compile(r'^(\S+) \(([\w.]+)\)(?:\n)? \.\.\. (ok|FAIL|ERROR|skipped.*)$')
_UT_FAIL = re.compile(r'^FAILED \((.*)\)$')


def parse_unittest(lines):
    counts, tests, found = _counts(), [], False
    start = 0
    for i, text in enumerate(lines):
        m = _UT_RAN.match(text)
        if not m:
            continue
        found = True
        total = int(m.group(1))
        failed = errors = skipped = 0
        for tail in lines[i + 1:i + 6]:
            if _UT_RAN.match(tail):
                break               # the next run's own verdict follows
            f = _UT_FAIL.match(tail.strip())
            if f:
                for key, value in re.findall(r'(failures|errors|skipped)=(\d+)', f.group(1)):
                    if key == 'failures':
                        failed = int(value)
                    elif key == 'errors':
                        errors = int(value)
                    else:
                        skipped = int(value)
            ok = re.match(r'^OK(?: \(skipped=(\d+)\))?$', tail.strip())
            if ok and ok.group(1):
                skipped = int(ok.group(1))
        counts['passed'] += max(0, total - failed - errors - skipped)
        counts['failed'] += failed
        counts['errored'] += errors
        counts['skipped'] += skipped
        for j in range(start, i):
            v = _UT_V.match(lines[j])
            if v:
                status = {'ok': 'passed', 'FAIL': 'failed', 'ERROR': 'errored'}.get(v.group(3), 'skipped')
                tests.append(_entry(v.group(1), status, j + 1, suite=v.group(2)))
        start = i + 1
    if not found:
        return None
    return {'runner': 'unittest', 'counts': counts, 'tests': tests, 'complete': len(tests) == sum(counts.values())}


PARSERS = (parse_pytest, parse_tap, parse_cargo, parse_go, parse_jest, parse_jvm, parse_karma, parse_unittest)


def parse_step_log(text, step_name=''):
    """One step's tests: {runners, counts, tests, complete}, or None when no runner's summary is present."""
    lines = clean_lines(text)
    found = []
    deadline = time.monotonic() + PARSE_SECONDS
    exhausted = False
    for parser in PARSERS:
        if time.monotonic() > deadline:
            exhausted = True            # what the earlier parsers found stands; the rest are not attempted
            break
        try:
            result = parser(lines)
        except Exception:           # a parser bug must never take the collector down
            result = None
        if result and sum(result['counts'].values()) > 0:
            found.append(result)
    if not found:
        return None
    counts = _counts()
    tests = []
    for result in found:
        for state in STATES:
            counts[state] += result['counts'][state]
        tests.extend(result['tests'])
    return {'runners': [name for r in found for name in r['runner'].split('+')], 'counts': counts, 'tests': tests[:MAX_INDEX],
            'complete': all(r['complete'] for r in found) and len(tests) <= MAX_INDEX and not exhausted, 'step': step_name}


def combine_steps(steps):
    """Sum the parsed steps of one run. `steps`: [(step_id, step_name, parsed)] with parsed from parse_step_log."""
    counts, runners, index, complete = _counts(), [], [], True
    seen = set()
    for step_id, name, parsed in steps:
        if parsed is None:
            continue
        # The same step listed twice (a restarted or duplicated workflow) prints the same tests twice. Only a step with the
        # same name, runners, counts and identical named tests is taken for a repeat; anything less certain is summed.
        fingerprint = (name, tuple(parsed['runners']), tuple(parsed['counts'].values()),
                       tuple((t['suite'], t['name'], t['status']) for t in parsed['tests'])) if parsed['tests'] else None
        if fingerprint is not None:
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
        for state in STATES:
            counts[state] += parsed['counts'][state]
        for runner in parsed['runners']:
            if runner not in runners:
                runners.append(runner)
        complete = complete and parsed['complete']
        for t in parsed['tests']:
            if len(index) < MAX_INDEX:
                index.append({**t, 'step_id': str(step_id), 'step': name})
            else:
                complete = False
    total = sum(counts.values())
    if not runners or total == 0:
        return None
    return {'runners': runners, 'summary': {**counts, 'total': total}, 'tests': index, 'complete': complete}
