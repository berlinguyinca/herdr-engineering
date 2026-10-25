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


def _text(value, limit=500):
    return redact_text(str(value or ''))[:limit]


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def normalize_provider_url(value):
    """Accept only credential-free provider base URLs, before persistence."""
    if (not isinstance(value, str) or not value or len(value) > 1000 or
            not value.isprintable() or any(c.isspace() for c in value) or
            redact_text(value) != value or '[REDACTED]' in value):
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


def parse_junit(xml):
    if not isinstance(xml, str) or len(xml.encode('utf-8')) > MAX_XML_BYTES:
        raise ValueError('JUnit XML must be a string of at most 1 MiB')
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('JUnit DTD and entity declarations are forbidden')
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise ValueError('Malformed JUnit XML') from None
    if _tag(root) not in {'testsuites', 'testsuite'}:
        raise ValueError('JUnit root must be testsuite or testsuites')
    tests, identities = [], {}
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
            name = _text(node.get('name')) or '(unnamed test)'
            classname = _text(node.get('classname'))
            suite = _text(' / '.join(suite_path))
            # Hash full original labels before redaction/truncation so display
            # collisions cannot create false retry or flakiness evidence.
            labels = [suite_path, node.get('classname', ''), node.get('name', '')]
            if node.get('file'):
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
                              message=_text(detail.get('message'), 2000) if detail is not None else '',
                              trace=_text(''.join(detail.itertext()), 4000) if detail is not None else ''))
            if node.get('file'):
                tests[-1]['file'] = _text(node.get('file'))
        else:
            pending.extend((child, suite_path) for child in reversed(list(node)))
    summary = {state: sum(test['status'] == state for test in tests)
               for state in ('passed', 'failed', 'errored', 'skipped')}
    summary.update(total=len(tests), duration_ms=sum(test['duration_ms'] for test in tests))
    parsed = dict(summary=summary, tests=tests)
    if len(json.dumps(parsed).encode()) > MAX_XML_BYTES:
        raise ValueError('Normalized JUnit report exceeds 1 MiB')
    return parsed


def normalize_report(body, stamp):
    if not isinstance(body, dict):
        raise ValueError('Report must be a JSON object')
    values = {}
    if body.get('provider_url') is not None:
        values['provider_url'] = normalize_provider_url(body['provider_url'])
    for key in ('repository', 'branch', 'revision'):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f'{key} must be a nonempty string of at most 500 characters')
        values[key] = _text(value)
    number = body.get('run_number')
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError('run_number must be a positive integer')
    provider = body.get('provider_repo_id')
    if provider is not None and (isinstance(provider, bool) or not isinstance(provider, (str, int)) or not str(provider) or len(str(provider)) > 300):
        raise ValueError('provider_repo_id must be a string or integer')
    if isinstance(provider, str):
        provider = _text(provider)
    reported = body.get('reported_at', stamp)
    try:
        if not isinstance(reported, str) or len(reported) > 80 or datetime.fromisoformat(reported.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('reported_at must be an ISO timestamp with timezone') from None
    values.update(provider_repo_id=provider, run_number=number, **parse_junit(body.get('xml')))
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return dict(values, report_id=digest, reported_at=reported)


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
