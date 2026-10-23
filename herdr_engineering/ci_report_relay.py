"""Relay native Hive JUnit bundles through a trusted host's existing SSH access."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .ci_report_upload import MAX_RESPONSE_BYTES, REQUEST_TIMEOUT, _endpoint, _NoRedirect
from .control_plane.ci_reports import MAX_XML_BYTES, parse_junit

MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_BATCH = 10
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_CHECKPOINTS = 5000
MAX_REMOTE_BYTES = MAX_BATCH * (MAX_BUNDLE_BYTES * 4 // 3 + 1024) + 4096
BASENAME = re.compile(r'([1-9][0-9]{0,15})-([0-9a-f]{40})-hive-slurm\.json\Z')
HOST = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
FIELDS = {'schema_version', 'repository', 'provider_repo_id', 'run_number', 'branch',
          'revision', 'suite', 'runner_exit_code', 'reported_at', 'xml'}

# This exact script runs both locally in tests and remotely. Source paths and
# checkpoint metadata travel as JSON on stdin, never as shell fragments.
SOURCE_READER = r'''
import base64, errno, json, os, re, stat, sys
config = json.loads(sys.stdin.buffer.read(2097153))
root, known, batch = config['root'], config['known'], config['batch']
pattern = re.compile(r'[1-9][0-9]{0,15}-[0-9a-f]{40}-hive-slurm\.json\Z')
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    entries, failures = [], 0
    with os.scandir(root_fd) as iterator:
        for index, entry in enumerate(iterator):
            if index >= 20000:
                break
            if not pattern.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
            if 0 < info.st_size <= 2097152:
                entries.append((info.st_mtime_ns, entry.name, info.st_size))
    entries.sort(reverse=True)
    entries = entries[:5000]
    after = config.get('cursor')
    positions = [index for index, row in enumerate(entries) if row[1] == after]
    if positions:
        start = positions[0] + 1
        entries = entries[start:] + entries[:start]
    results, visited, cursor = [], 0, after
    for modified, name, size in entries:
        if visited >= batch:
            break
        previous = known.get(name, {})
        if previous.get('mtime_ns') == modified and previous.get('size') == size:
            continue
        cursor = name
        visited += 1
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 2097152:
                    continue
                data = stream.read(2097153)
                after = os.fstat(stream.fileno())
            if (len(data) > 2097152 or before.st_size != len(data)
                    or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                continue
            results.append({'name': name, 'size': len(data), 'mtime_ns': after.st_mtime_ns,
                            'content': base64.b64encode(data).decode('ascii')})
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ELOOP):
                failures += 1
    print(json.dumps({'files': results, 'read_failures': failures, 'cursor': cursor}, separators=(',', ':')))
finally:
    os.close(root_fd)
'''
BOOTSTRAP = 'import sys; exec(sys.stdin.buffer.read(int(sys.argv[1])))'


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate bundle field')
        result[key] = value
    return result


def validate_bundle(name, raw):
    match = BASENAME.fullmatch(name) if isinstance(name, str) else None
    if not match or not isinstance(raw, bytes) or len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError('Invalid bundle filename or size')
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Bundle must contain UTF-8 JSON') from None
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Bundle fields do not match schema version 1')
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['repository'] != 'InferWeave/inferweave'
            or type(value['provider_repo_id']) is not int or value['provider_repo_id'] != 116
            or type(value['run_number']) is not int or value['run_number'] != int(match[1])
            or value['revision'] != match[2] or value['suite'] != 'hive-slurm'
            or type(value['runner_exit_code']) is not int):
        raise ValueError('Bundle execution identity is outside the allowlist')
    if not isinstance(value['branch'], str) or not value['branch'].strip() or len(value['branch']) > 500:
        raise ValueError('Bundle branch is required and bounded')
    try:
        reported = value['reported_at']
        if not isinstance(reported, str) or len(reported) > 80 or datetime.fromisoformat(reported.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('Bundle timestamp must include a timezone') from None
    xml = value['xml']
    if not isinstance(xml, str) or len(xml.encode('utf-8')) > MAX_XML_BYTES:
        raise ValueError('Bundle JUnit XML exceeds the size limit')
    parse_junit(xml)
    return value


def _run_reader(command, payload):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + 60
    try:
        # Bound output while writing the bounded checkpoint input concurrently.
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        sent = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Source read timed out')
                for key, _ in selector.select(min(remaining, 1)):
                    if key.fileobj is process.stdin:
                        try:
                            sent += os.write(process.stdin.fileno(), payload[sent:sent + 65536])
                        except BrokenPipeError:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            continue
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            selector.unregister(process.stdout)
                        output.extend(chunk)
                        if len(output) > MAX_REMOTE_BYTES:
                            raise ValueError('Source response exceeds the batch limit')
        if process.wait(timeout=max(0.1, deadline - time.monotonic())):
            raise ValueError('Cannot read the configured source')
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Cannot read the configured source') from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def read_source(source_dir, checkpoints, *, batch_size=MAX_BATCH, host=None, cursor=None, with_status=False):
    root = PurePosixPath(source_dir)
    if not root.is_absolute() or '..' in root.parts or '\x00' in source_dir or str(root) == '/':
        raise ValueError('Source directory must be an absolute configured directory')
    if not 1 <= batch_size <= MAX_BATCH:
        raise ValueError('Batch size must be between 1 and 10')
    script = SOURCE_READER.encode()
    if cursor is not None and (not isinstance(cursor, str) or not BASENAME.fullmatch(cursor)):
        raise ValueError('Invalid scan cursor')
    config = json.dumps({'root': source_dir, 'known': checkpoints, 'batch': batch_size, 'cursor': cursor}).encode()
    if len(config) > MAX_STATE_BYTES:
        raise ValueError('Source checkpoint request exceeds the size limit')
    if host is None:
        command = [sys.executable, '-c', BOOTSTRAP, str(len(script))]
    else:
        if not HOST.fullmatch(host):
            raise ValueError('Invalid configured SSH host')
        remote = 'python3 -c ' + shlex.quote(BOOTSTRAP) + ' ' + str(len(script))
        command = ['ssh', '-T', '-oBatchMode=yes', '-oConnectTimeout=10', '--', host, remote]
    try:
        result = json.loads(_run_reader(command, script + config))
        files = result['files']
        if not isinstance(files, list) or len(files) > batch_size:
            raise ValueError()
        for row in files:
            if not isinstance(row, dict) or not BASENAME.fullmatch(row.get('name', '')):
                raise ValueError()
            row['content'] = base64.b64decode(row['content'], validate=True)
            if (type(row.get('mtime_ns')) is not int or row['mtime_ns'] < 0
                    or type(row.get('size')) is not int or row['size'] != len(row['content'])
                    or row['size'] > MAX_BUNDLE_BYTES):
                raise ValueError()
        if (type(result.get('read_failures')) is not int or not 0 <= result['read_failures'] <= MAX_BATCH
                or result.get('cursor') is not None and not BASENAME.fullmatch(result['cursor'])):
            raise ValueError()
        return result if with_status else files
    except (KeyError, TypeError, ValueError):
        raise ValueError('Source returned an invalid bounded bundle batch') from None


def _write_private(path, raw):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class Relay:
    def __init__(self, control_plane, state_dir, *, key='', allow_open=False):
        if not key and not allow_open:
            raise ValueError('Set an operator key or explicitly allow the open trusted-network server')
        if key and (not key.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in key)):
            raise ValueError('Invalid operator key')
        self.endpoint = _endpoint(control_plane)
        self.key = key
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir() or self.state_dir.stat().st_uid != os.getuid():
            raise ValueError('State directory must be a private owned directory')
        self.state_dir.chmod(0o700)
        self.state_file = self.state_dir / 'checkpoint.json'
        self.checkpoints = {}
        try:
            descriptor = os.open(self.state_file, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        except OSError:
            raise ValueError('Cannot safely read relay checkpoint') from None
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Checkpoint must be a private owned regular file')
            data = stream.read(MAX_STATE_BYTES + 1)
        try:
            state = json.loads(data)
            reports = state['reports']
            if len(data) > MAX_STATE_BYTES or state['version'] != 1 or not isinstance(reports, dict) or len(reports) > MAX_CHECKPOINTS:
                raise ValueError()
            for name, row in reports.items():
                if (not BASENAME.fullmatch(name) or not isinstance(row, dict)
                        or not re.fullmatch('[0-9a-f]{64}', row.get('sha256', ''))
                        or type(row.get('size')) is not int or not 0 <= row['size'] <= MAX_BUNDLE_BYTES
                        or type(row.get('mtime_ns')) is not int or row['mtime_ns'] < 0):
                    raise ValueError()
            self.checkpoints = reports
        except (KeyError, TypeError, ValueError):
            raise ValueError('Relay checkpoint is invalid or exceeds its limit') from None

    def _request(self, endpoint, body=None):
        headers = {'Accept': 'application/json', **({'X-API-Key': self.key} if self.key else {})}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(endpoint, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError('Control-plane response exceeds its size limit')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError('Control-plane response is not a JSON object')
            return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ValueError(f'Control-plane request refused (HTTP {status})') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ValueError('Control-plane request could not be completed') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('Control-plane response is invalid JSON') from None

    def _checkpoint(self, name, digest, size, modified):
        updated = {key: value for key, value in self.checkpoints.items() if key != name}
        updated[name] = dict(sha256=digest, size=size, mtime_ns=modified,
                             acknowledged_at=datetime.now(UTC).isoformat())
        updated = dict(list(updated.items())[-MAX_CHECKPOINTS:])
        raw = json.dumps({'version': 1, 'reports': updated}).encode()
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('Relay checkpoint exceeds its size limit')
        _write_private(self.state_file, raw)
        self.checkpoints = updated

    def _scan_cursor(self):
        try:
            descriptor = os.open(self.state_dir / 'scan-cursor.json', os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Scan cursor must be a private owned regular file')
            raw = stream.read(1025)
        try:
            value = json.loads(raw)
            after = value['after']
            if len(raw) > 1024 or value['version'] != 1 or after is not None and not BASENAME.fullmatch(after):
                raise ValueError()
            return after
        except (KeyError, TypeError, ValueError):
            raise ValueError('Invalid relay scan cursor') from None

    def process_source(self, source_dir, *, host=None):
        batch = read_source(source_dir, self.checkpoints, host=host,
                            cursor=self._scan_cursor(), with_status=True)
        published, failed = 0, batch['read_failures']
        for row in batch['files']:
            try:
                published += self.consume(row['name'], row['content'], mtime_ns=row['mtime_ns'])
            except (ValueError, OSError):
                failed += 1
        # This cursor records scan position only, never successful delivery.
        _write_private(self.state_dir / 'scan-cursor.json', json.dumps(
            {'version': 1, 'after': batch['cursor']}).encode())
        return published, failed

    def consume(self, name, raw, *, mtime_ns=0):
        value = validate_bundle(name, raw)
        digest = hashlib.sha256(raw).hexdigest()
        previous = self.checkpoints.get(name, {})
        if previous.get('sha256') == digest:
            if previous.get('mtime_ns') != mtime_ns:
                self._checkpoint(name, digest, len(raw), mtime_ns)
            return False
        query = urllib.parse.urlencode({'repository': 116, 'number': value['run_number']})
        observed = self._request(self.endpoint.removesuffix('/reports') + '/pipeline?' + query)
        pipeline = observed.get('pipeline')
        if (observed.get('available') is not True or not isinstance(pipeline, dict)
                or pipeline.get('number') != value['run_number']
                or pipeline.get('commit') != value['revision'] or pipeline.get('branch') != value['branch']):
            raise ValueError('Bundle does not match the observed provider run identity')
        body = {key: value[key] for key in ('repository', 'provider_repo_id', 'run_number',
                                           'branch', 'revision', 'reported_at', 'xml')}
        acknowledged = self._request(self.endpoint, body)
        summary = acknowledged.get('summary')
        if (not isinstance(summary, dict) or type(summary.get('total')) is not int or summary['total'] < 0
                or not isinstance(acknowledged.get('report_id'), str) or not acknowledged['report_id']
                or any(acknowledged.get(key) != body[key] for key in
                       ('repository', 'provider_repo_id', 'run_number', 'branch', 'revision'))):
            raise ValueError('Control plane did not acknowledge this report identity')
        self._checkpoint(name, digest, len(raw), mtime_ns)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-host', required=True)
    parser.add_argument('--source-dir', required=True)
    parser.add_argument('--control-plane', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--allow-open', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    while True:
        published, failed = 0, 0
        try:
            relay = Relay(args.control_plane, args.state_dir,
                          key=os.environ.get('HERDR_ENGINEERING_API_KEY', ''), allow_open=args.allow_open)
            published, failed = relay.process_source(args.source_dir, host=args.source_host)
        except (ValueError, OSError):
            failed += 1
        # No filenames, URLs, credentials, source contents or server responses.
        print(f'CI report relay: {published} published, {failed} failed', flush=True)
        if args.once:
            return 1 if failed else 0
        time.sleep(120)


if __name__ == '__main__':
    raise SystemExit(main())
