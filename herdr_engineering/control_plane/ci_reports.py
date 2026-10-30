"""Bounded JUnit normalization. Raw XML and system-out are never retained.

Reports describe uploaded cases, not the provider's complete test inventory.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .redact import redact_text

MAX_XML_BYTES = 1024 * 1024
MAX_CASES = 5000
REPORT_LIMIT = 200
PIPELINE_LIMIT = 2000
HISTORY_BYTES = 16 * 1024 * 1024


def _text(value, limit=500, *, redact=None):
    value = str(value or '')
    if redact is not None:
        value = redact(value)
    return redact_text(value)[:limit]


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def normalize_provider_url(value, *, redact=None):
    """Accept only credential-free provider base URLs, before persistence."""
    if (not isinstance(value, str) or not value or len(value) > 1000 or
            not value.isprintable() or any(c.isspace() for c in value) or
            redact_text(value) != value or '[REDACTED]' in value or
            (redact is not None and redact(value) != value)):
        raise ValueError('Invalid provider_url')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError('Invalid provider_url') from None
    return value.rstrip('/')


class _JUnitTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Enforce this at the parser boundary, independently of the text scan.
        # Reject before any declared entities can be used in the document.
        raise ValueError('JUnit DTD and entity declarations are forbidden')


def parse_junit(xml, *, redact=None, _identity_version=2):
    if not isinstance(xml, str) or len(xml) > MAX_XML_BYTES:
        raise ValueError('JUnit XML must be a string of at most 1 MiB')
    try:
        encoded = xml.encode('utf-8')
    except UnicodeError:
        raise ValueError('JUnit XML must contain valid Unicode') from None
    if '\x00' in xml:
        raise ValueError('JUnit XML must not contain NUL characters')
    if len(encoded) > MAX_XML_BYTES:
        raise ValueError('JUnit XML must be a string of at most 1 MiB')
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('JUnit DTD and entity declarations are forbidden')
    try:
        root = ElementTree.fromstring(xml, parser=ElementTree.XMLParser(target=_JUnitTreeBuilder()))
    except ElementTree.ParseError:
        raise ValueError('Malformed JUnit XML') from None
    if _tag(root) not in {'testsuites', 'testsuite'}:
        raise ValueError('JUnit root must be testsuite or testsuites')
    tests, identities = [], {}
    case_groups = {}
    # Iterative traversal also handles nested suites without recursion limits.
    pending = [(root, ())]
    while pending:
        node, suite_path = pending.pop()
        if _tag(node) == 'testsuite':
            raw_suite = node.get('name', '')
            if raw_suite:
                suite_path = (*suite_path, raw_suite)
        if _tag(node) == 'testcase':
            if len(tests) >= MAX_CASES:
                raise ValueError(f'JUnit report exceeds {MAX_CASES} test cases')
            try:
                seconds = float(node.get('time', '0'))
            except ValueError:
                raise ValueError('JUnit case time must be a finite nonnegative number') from None
            if not math.isfinite(seconds) or seconds < 0 or seconds > 31536000:
                raise ValueError('JUnit case time must be finite and between zero and one year')
            name = _text(node.get('name'), redact=redact) or '(unnamed test)'
            classname = _text(node.get('classname'), redact=redact)
            suite = _text(' / '.join(suite_path), redact=redact)
            # Hash full original labels before redaction/truncation so display
            # collisions cannot create false retry or flakiness evidence.
            labels = [suite_path, node.get('classname', ''), node.get('name', '')]
            if _identity_version >= 2 and node.get('file'):
                labels.append(node.get('file'))
            identity = hashlib.sha256(json.dumps(
                labels,
                ensure_ascii=True).encode()).hexdigest()
            occurrence = identities.get(identity, 0)
            identities[identity] = occurrence + 1
            case_id = hashlib.sha256(f'{identity}:{occurrence}'.encode()).hexdigest()[:24]
            outcomes = {_tag(child): child for child in node}
            status = next((s for s in ('error', 'failure', 'skipped') if s in outcomes), None)
            detail = outcomes.get(status)
            tests.append(dict(id=case_id, name=name, suite=suite, classname=classname,
                              status={'error': 'errored', 'failure': 'failed'}.get(status, status or 'passed'),
                              duration_ms=round(seconds * 1000),
                              message=_text(detail.get('message'), 2000, redact=redact) if detail is not None else '',
                              trace=_text(''.join(detail.itertext()), 4000, redact=redact) if detail is not None else ''))
            if _identity_version >= 2 and node.get('file'):
                tests[-1]['file'] = _text(node.get('file'), redact=redact)
            case_groups.setdefault(identity, []).append(tests[-1])
        else:
            pending.extend((child, suite_path) for child in reversed(list(node)))
    for cases in case_groups.values():
        if _identity_version >= 2 and len(cases) > 1:
            # Occurrence ordinals keep rows distinct, but do not identify a
            # physical test across reports when duplicate labels reorder.
            for case in cases:
                case['identity_ambiguous'] = True
    summary = {state: sum(test['status'] == state for test in tests)
               for state in ('passed', 'failed', 'errored', 'skipped')}
    summary.update(total=len(tests), duration_ms=sum(test['duration_ms'] for test in tests))
    parsed = dict(summary=summary, tests=tests)
    if len(json.dumps(parsed).encode()) > MAX_XML_BYTES:
        raise ValueError('Normalized JUnit report exceeds 1 MiB')
    return parsed


def normalize_report(body, stamp, *, redact=None, _identity_version=2):
    if not isinstance(body, dict):
        raise ValueError('Report must be a JSON object')
    values = {}
    if body.get('provider_url') is not None:
        values['provider_url'] = normalize_provider_url(body['provider_url'], redact=redact)
    for key in ('repository', 'branch', 'revision'):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f'{key} must be a nonempty string of at most 500 characters')
        values[key] = _text(value, redact=redact)
    number = body.get('run_number')
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError('run_number must be a positive integer')
    provider = body.get('provider_repo_id')
    if provider is not None and (isinstance(provider, bool) or not isinstance(provider, (str, int)) or not str(provider) or len(str(provider)) > 300):
        raise ValueError('provider_repo_id must be a string or integer')
    if isinstance(provider, str):
        provider = _text(provider, redact=redact)
    reported = body.get('reported_at', stamp)
    try:
        if not isinstance(reported, str) or len(reported) > 80 or datetime.fromisoformat(reported.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('reported_at must be an ISO timestamp with timezone') from None
    values.update(provider_repo_id=provider, run_number=number,
                  **parse_junit(body.get('xml'), redact=redact, _identity_version=_identity_version))
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    # The version is receipt metadata, outside the digest, so already-uploaded
    # file-aware reports retain their hashes too. Version 1 is lookup-only.
    return dict(values, report_id=digest, reported_at=reported, identity_version=_identity_version)


LOG_SOURCE = 'log_parse'
LOG_REPORT_LIMIT = 400          # analysed runs retained, separately from uploaded reports so they never evict a JUnit upload
LOG_HISTORY_BYTES = 64 * 1024 * 1024
LOG_INDEX_RUNS = 40             # newest analysed runs that keep their per-test index; older ones keep counts only
LOG_OUTCOMES = ('counted', 'no_tests')


def log_run_key(provider_url, provider_repo_id, run_number):
    """Stable event id of one run's log analysis; re-analysing a run replaces its record."""
    return hashlib.sha256(json.dumps([provider_url, str(provider_repo_id), int(run_number)],
                                     separators=(',', ':')).encode()).hexdigest()


def normalize_log_report(body, stamp, *, redact=None):
    """A report built from CI log text: counts, runner names and a per-test index, never log text.

    `body` carries the run identity and `steps`, the output of `ci_logtests.combine_steps`. Names are redacted and
    bounded; the index shrinks until the record fits, and says so in `index_complete`.
    """
    if not isinstance(body, dict):
        raise ValueError('Log report must be a JSON object')
    values = {'provider_url': normalize_provider_url(body.get('provider_url'), redact=redact)}
    for key in ('repository', 'branch', 'revision'):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f'{key} must be a nonempty string of at most 500 characters')
        values[key] = _text(value, redact=redact)
    number = body.get('run_number')
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError('run_number must be a positive integer')
    provider = body.get('provider_repo_id')
    if isinstance(provider, bool) or not isinstance(provider, (str, int)) or not str(provider) or len(str(provider)) > 300:
        raise ValueError('provider_repo_id must be a string or integer')
    outcome = body.get('outcome')
    if outcome not in LOG_OUTCOMES:
        raise ValueError('outcome must be counted or no_tests')
    summary = body.get('summary') or {}
    counts = {state: max(0, int(summary.get(state, 0))) for state in ('passed', 'failed', 'errored', 'skipped')}
    if outcome == 'no_tests':
        counts = dict.fromkeys(counts, 0)
    counts.update(total=sum(counts.values()), duration_ms=0)
    seen, tests, occurrences = set(), [], {}
    for raw in (body.get('tests') or [])[:MAX_CASES] if outcome == 'counted' else []:
        label = [raw.get('step_id', ''), raw.get('suite', ''), raw.get('name', '')]
        base = hashlib.sha256(json.dumps(label, ensure_ascii=True).encode()).hexdigest()
        occurrences[base] = occurrences.get(base, 0) + 1
        case_id = hashlib.sha256(f'{base}:{occurrences[base] - 1}'.encode()).hexdigest()[:24]
        if case_id in seen or raw.get('status') not in ('passed', 'failed', 'errored', 'skipped'):
            continue
        seen.add(case_id)
        ms = raw.get('duration_ms')
        # Compact on purpose: a big suite is thousands of these. The step's name lives once in `steps`, and absent
        # fields mean "none" (no message or trace is ever kept for a parsed test).
        row = dict(id=case_id, name=_text(raw.get('name'), 500, redact=redact), status=raw['status'],
                   duration_ms=int(ms) if isinstance(ms, (int, float)) and ms >= 0 else 0,
                   step_id=_text(raw.get('step_id'), 40), line_start=max(1, int(raw.get('line_start') or 1)),
                   line_end=max(1, int(raw.get('line_end') or raw.get('line_start') or 1)))
        if raw.get('suite'):
            row['suite'] = _text(raw.get('suite'), 500, redact=redact)
        if isinstance(ms, (int, float)) and ms >= 0:
            row['timed'] = True
        if occurrences[base] > 1:
            row['identity_ambiguous'] = True
        tests.append(row)
    steps = [{'step_id': _text(row.get('step_id'), 40), 'step': _text(row.get('step'), 200, redact=redact),
              'runners': [_text(r, 40) for r in (row.get('runners') or [])][:8],
              'indexed': bool(row.get('indexed'))} for row in (body.get('steps') or [])[:100]]
    skipped = [{'step': _text(row.get('step'), 200, redact=redact), 'reason': _text(row.get('reason'), 80)}
               for row in (body.get('skipped_steps') or [])[:100]]
    complete = bool(body.get('index_complete')) and len(tests) == counts['total']
    record = dict(values, provider_repo_id=provider if isinstance(provider, int) else _text(provider, redact=redact),
                  run_number=number, source=LOG_SOURCE, outcome=outcome, summary=counts, tests=tests,
                  runners=[_text(r, 40) for r in (body.get('runners') or [])][:16], steps=steps, skipped_steps=skipped,
                  index_complete=complete, run_finished_at=_text(body.get('run_finished_at'), 40),
                  provisional=body.get('provisional') is True)
    while len(json.dumps(record).encode()) > MAX_XML_BYTES and record['tests']:
        record['tests'] = record['tests'][:max(0, len(record['tests']) - max(1, len(record['tests']) // 8))]
        record['index_complete'] = False
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    return dict(record, report_id=digest, reported_at=stamp, ingested_at=stamp, analysed_at=stamp, identity_version=2,
                run_key=log_run_key(values['provider_url'], provider, number))


def matches_legacy_report(receipt, candidate):
    """Require the complete historical normalization, never display labels alone.

    Legacy reports did not retain files. Returning their original receipts must
    not imply that we recovered file identity or enrich their case evidence.
    """
    if receipt.get('identity_version', 1) != 1 or receipt.get('provider_url') is not None:
        return False
    metadata = {'reported_at', 'ingested_at', 'attempt', 'identity_version'}
    return ({key: value for key, value in receipt.items() if key not in metadata} ==
            {key: value for key, value in candidate.items() if key not in metadata})


def reconcile_report_identities(reports, pipelines):
    """Bind omitted provider IDs from unambiguous observed execution identity.

    Reports arrive newest first by server ingestion order. This enriches
    detached projections and assigns effective retry ordinals when a later poll
    binds uploaded identities. Uploaded payloads/hashes stay unchanged; receipt
    ordinals remain available as uploaded_attempt. Revision prefixes and
    repository names alone are insufficient evidence.
    """
    candidates = {}
    fields = ('repository', 'run_number', 'branch', 'revision')
    for pipeline in pipelines:
        provider_id = pipeline.get('provider_repo_id')
        if provider_id is None or not str(provider_id):
            continue
        if any(not isinstance(pipeline.get(field), str) or not pipeline[field]
               for field in ('repository', 'branch', 'revision')):
            continue
        identity = tuple(pipeline.get(field) for field in fields)
        candidates.setdefault(identity, {})[(pipeline.get('provider_url'), str(provider_id))] = provider_id
    for report in reports:
        provided = report.get('provider_repo_id')
        report['uploaded_provider_repo_id'] = provided
        if provided is not None:
            report['identity_status'] = 'provided'
            continue
        matches = candidates.get(tuple(report.get(field) for field in fields), {})
        if report.get('provider_url') is not None:
            matches = {key: value for key, value in matches.items()
                       if key[0] == report['provider_url']}
        if len(matches) == 1:
            report['provider_repo_id'] = next(iter(matches.values()))
            report['identity_status'] = 'matched_observation'
        else:
            report['identity_status'] = 'ambiguous' if matches else 'unmatched'

    # Callers provide newest-first server ingestion order. Later observations
    # can merge previously separate uploaded identities; effective ordinals must
    # then follow actual arrival order, while preserving the original receipt.
    attempts = {}
    for report in reversed(reports):
        identity = (report.get('provider_url'), report['repository'],
                    str(report.get('provider_repo_id')), report['run_number'])
        uploaded = report.get('attempt', 1)
        report['uploaded_attempt'] = uploaded
        report['attempt'] = max(uploaded, attempts.get(identity, 0) + 1)
        attempts[identity] = report['attempt']
