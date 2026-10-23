"""Publish bounded JUnit reports using an operator key or explicit trusted-network access."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from .control_plane.ci_reports import MAX_XML_BYTES, parse_junit

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 15


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a same-origin redirect is refused: never forward an operator key.
        return None


def _endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not parsed.port and parsed.netloc.endswith(':')
                or any(character.isspace() for character in value)):
            raise ValueError()
    except ValueError:
        raise ValueError('Control-plane URL must be HTTP(S), without credentials, query or fragment') from None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                   parsed.path.rstrip('/') + '/api/v1/ci/reports', '', ''))


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', help='UTF-8 JUnit XML file (at most 1 MiB)')
    parser.add_argument('--allow-open', action='store_true',
                        help='Allow upload without a key to an intentionally open trusted-network server')
    parser.add_argument('--control-plane', default=os.environ.get('HERDR_ENGINEERING_CONTROL_PLANE_URL', ''))
    for option, environment in [('repository', 'CI_REPO'), ('run-number', 'CI_PIPELINE_NUMBER'),
                                ('branch', 'CI_COMMIT_BRANCH'), ('revision', 'CI_COMMIT_SHA'),
                                ('provider-repo-id', 'CI_REPO_ID')]:
        parser.add_argument('--' + option, default=os.environ.get(environment, ''))
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        endpoint = _endpoint(args.control_plane)
        key = os.environ.get('HERDR_ENGINEERING_API_KEY', '')
        if ((not key and not args.allow_open)
                or (key and (not key.isascii() or any(ord(character) < 33 or ord(character) > 126 for character in key)))):
            raise ValueError('Set HERDR_ENGINEERING_API_KEY to an operator key')
        body = {}
        for name in ('repository', 'branch', 'revision'):
            value = getattr(args, name)
            if not value.strip() or len(value) > 500:
                raise ValueError(f'{name} must be a nonempty value of at most 500 characters')
            body[name] = value
        try:
            number = int(args.run_number)
        except ValueError:
            raise ValueError('run-number must be a positive integer') from None
        if number < 1:
            raise ValueError('run-number must be a positive integer')
        body['run_number'] = number
        if args.provider_repo_id:
            if not args.provider_repo_id.strip() or len(args.provider_repo_id) > 300:
                raise ValueError('provider-repo-id must be at most 300 characters')
            body['provider_repo_id'] = args.provider_repo_id
        try:
            with open(args.report, 'rb') as report:
                raw = report.read(MAX_XML_BYTES + 1)
            if len(raw) > MAX_XML_BYTES:
                raise ValueError('JUnit XML must be at most 1 MiB')
            body['xml'] = raw.decode('utf-8')
        except OSError:
            raise ValueError('Cannot read JUnit report file') from None
        except UnicodeDecodeError:
            raise ValueError('JUnit report must use UTF-8') from None
        parse_junit(body['xml'])
        request = urllib.request.Request(endpoint, method='POST',
                                         data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                         headers={'Content-Type': 'application/json',
                                                  **({'X-API-Key': key} if key else {})})
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError('Control-plane response exceeds the size limit')
            result = json.loads(payload)
            summary = result.get('summary') if isinstance(result, dict) else None
            total = summary.get('total') if isinstance(summary, dict) else None
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError('Control-plane response does not contain a valid test summary')
        except urllib.error.HTTPError as exc:
            # Do not echo provider response bodies, URLs or headers: they may contain secrets.
            status = exc.code
            exc.close()
            raise ValueError(f'Report upload refused (HTTP {status}); redirects are not followed') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ValueError('Cannot reach control plane before the upload timeout') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('Control-plane response is not valid JSON') from None
    except ValueError as exc:
        print(f'CI report upload failed: {exc}', file=sys.stderr)
        return 1
    print(f'Published JUnit report: {total} tests')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
