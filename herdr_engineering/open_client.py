"""Task-scoped offers and opening on the automatically discovered operator client."""
from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.avif',
              '.html', '.htm'}
EXPLICIT_EXTENSIONS = EXTENSIONS | {'.md', '.markdown'}
EXCLUDED = {'.git', '.omx', '.venv', 'venv', 'node_modules', '__pycache__', '.cache'}
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 10000


def _save(state: Path, data: dict) -> None:
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=state.parent, prefix='.open-client-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, state)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(state: Path) -> dict:
    if state.is_symlink() or state.stat().st_mode & 0o077:
        raise ValueError('task state must be a private regular file')
    return json.loads(state.read_text())


def _read(root: Path, name: str) -> bytes:
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('source must remain inside the task root without symlinks')
    if any(part.is_symlink() for part in path.parents if part != root.parent):
        raise ValueError('source contains a symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('source must be a regular file')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('file exceeds 32 MiB preview limit')
    return data


def _snapshot(root: Path) -> dict:
    files = {}
    count = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        # Hidden directories (.pi-eng test output, .worktrees, tool caches) are never deliverables.
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.')
                         and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            count += 1
            if count > MAX_FILES:
                raise ValueError('task root exceeds 10000 files; choose a smaller output root')
            path = Path(directory) / name
            if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                continue
            relative = str(path.relative_to(root))
            try:
                data = _read(root, relative)
            except (OSError, ValueError):
                continue
            files[relative] = hashlib.sha256(data).hexdigest()
    return files


def begin(root: Path, state: Path) -> dict:
    root, state = Path(root).resolve(), Path(state).absolute()
    if not root.is_dir():
        raise ValueError('root must be a directory')
    data = {'root': str(root), 'baseline': _snapshot(root), 'offered': [], 'pending': {}}
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
    return {'state': str(state), 'root': str(root)}


def discover(state: Path, urls: list[str] | tuple[str, ...] = ()) -> dict:
    state = Path(state)
    data = _load(state)
    candidates = []
    current = _snapshot(Path(data['root']))
    # Remove stale versions from pending, including files deleted after an offer.
    data['pending'] = {key: value for key, value in data['pending'].items()
                       if value.get('kind') == 'url' or current.get(value['name']) == value['sha256']}
    seen_content = set()
    duplicates = 0
    for name, digest in sorted(current.items()):
        if data['baseline'].get(name) == digest:
            continue
        key = hashlib.sha256((name + '\0' + digest).encode()).hexdigest()[:16]
        if key in data['offered']:
            continue
        data['offered'].append(key)
        # The same bytes under several names are one thing to look at, not many.
        if digest in seen_content:
            duplicates += 1
            continue
        seen_content.add(digest)
        item = {'id': key, 'kind': 'file', 'name': name, 'sha256': digest}
        candidates.append(item)
        data['pending'][key] = item
    skipped_urls = []
    for url in dict.fromkeys(urls):
        key = 'u' + hashlib.sha256(url.encode()).hexdigest()[:15]
        if key in data['offered']:
            continue
        try:
            _check_url(url)
        except ValueError as error:
            skipped_urls.append({'url': url, 'reason': str(error)})
            continue
        data['offered'].append(key)
        item = {'id': key, 'kind': 'url', 'url': url}
        candidates.append(item)
        data['pending'][key] = item
    if candidates:
        data['pending'] = {item['id']: item for item in candidates}
    _save(state, data)
    files = sum(1 for item in candidates if item['kind'] == 'file')
    links = len(candidates) - files
    if not candidates:
        prompt = None
    elif not links:
        prompt = f'There are {files} files to view. Open all, select files, or skip?'
    else:
        what = ' and '.join(part for part in (f'{files} file{"s" * (files != 1)}' if files else '',
                                              f'{links} link{"s" * (links != 1)}') if part)
        prompt = f'There are {what} to open on your device. Open all, select, or skip?'
    result = {'candidates': candidates, 'prompt': prompt, 'destination': 'automatic'}
    if duplicates:
        result['duplicates_collapsed'] = duplicates
    if skipped_urls:
        result['skipped_urls'] = skipped_urls
    return result


def dismiss(state: Path) -> dict:
    state = Path(state)
    data = _load(state)
    data['pending'] = {}
    _save(state, data)
    return {'dismissed': True}


def markdown_preview(source: bytes) -> bytes:
    """Small escaped Markdown subset; raw HTML and remote assets stay inert."""
    lines = ['<!doctype html><meta charset="utf-8"><title>Markdown preview</title>',
             '<style>body{max-width:80ch;margin:3em auto;padding:1em;font:18px system-ui}',
             'pre{white-space:pre-wrap;background:#eee;padding:1em}</style>']
    fenced = False
    for line in source.decode('utf-8', errors='replace').splitlines():
        if line.startswith('```'):
            lines.append('</code></pre>' if fenced else '<pre><code>')
            fenced = not fenced
        elif fenced:
            lines.append(html.escape(line) + '\n')
        else:
            heading = re.match(r'^(#{1,6})\s+(.*)', line)
            if heading:
                level = len(heading[1])
                lines.append(f'<h{level}>{html.escape(heading[2])}</h{level}>')
            elif line:
                lines.append('<p>' + html.escape(line) + '</p>')
    if fenced:
        lines.append('</code></pre>')
    return '\n'.join(lines).encode()


def discover_target() -> dict:
    from .open_client_discovery import discover_target as discover
    return discover()


def _destination(target: str | None) -> str:
    if target:
        return target
    result = discover_target()
    if result.get('target'):
        return result['target']
    raise ValueError('operator target could not be discovered automatically; '
                     + result.get('error', result.get('reason', 'no active viewing client')))


def _file_payload(root: Path, name: str, content: bytes) -> dict:
    basename = Path(name).name
    if Path(name).suffix.lower() in {'.md', '.markdown'}:
        content = markdown_preview(content)
        basename += '.html'
    payload = {'name': basename, 'data': base64.b64encode(content).decode(),
               'sha256': hashlib.sha256(content).hexdigest()}
    if Path(name).suffix.lower() in {'.html', '.htm'}:
        from .open_client_assets import collect
        payload['name'] = name
        payload['assets'] = collect(root, name, content, _read)
    return payload


def open_file(path: Path, *, target: str | None = None) -> dict:
    """An explicit user open request, including Markdown; no additional offer."""
    path = Path(path).absolute()
    if path.suffix.lower() not in EXPLICIT_EXTENSIONS:
        raise ValueError('unsupported preview file type')
    root = path.parent.resolve()
    content = _read(root, path.name)
    target = _destination(target)
    return {'target': target, **remote_request(target, _file_payload(root, path.name, content))}


def remote_request(target: str, payload: dict) -> dict:
    if target != '@local' and (not target or not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*', target)):
        raise ValueError('target must be an explicit SSH host or user@host')
    from .open_client_remote import REMOTE_SCRIPT
    command = ([sys.executable, '-c', REMOTE_SCRIPT] if target == '@local' else
               ['ssh', '-oBatchMode=yes', '-oConnectTimeout=10', target,
                'python3 -c ' + shlex.quote(REMOTE_SCRIPT)])
    result = subprocess.run(
        command,
        input=json.dumps(payload), text=True, capture_output=True, timeout=90, check=False)
    if result.returncode:
        raise ValueError('client transport failed; verify SSH and desktop availability')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ValueError('client returned an invalid dispatch receipt') from None


def show(state: Path, selected: list[str], *, target: str | None = None) -> dict:
    state = Path(state)
    data = _load(state)
    target = _destination(target)
    keys = list(data['pending']) if selected == ['all'] else list(dict.fromkeys(selected))
    if not keys or any(key not in data['pending'] for key in keys):
        raise ValueError('selection must contain pending file or link IDs or all')
    prepared = []
    for key in keys:
        item = data['pending'][key]
        if item.get('kind') == 'url':
            _check_url(item['url'])
            prepared.append((key, {'url': item['url']}))
            continue
        content = _read(Path(data['root']), item['name'])
        if hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ValueError('selected source changed; discover and ask again')
        payload = _file_payload(Path(data['root']), item['name'], content)
        prepared.append((key, payload))
    # A subset selection skips other members of this offer.
    data['pending'] = {key: data['pending'][key] for key in keys}
    _save(state, data)
    results = []
    for key, payload in prepared:
        receipt = remote_request(target, payload)
        results.append({'id': key, 'target': target, **receipt})
        if receipt.get('dispatch') == 'requested':
            data['pending'].pop(key, None)
        data['target'] = target
        _save(state, data)
    return {'results': results}


def _check_url(url: str) -> None:
    """Raise ValueError unless `url` may be opened on the operator's device."""
    parts = urlsplit(url)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('only HTTP(S) URLs without embedded credentials may be opened')
    try:
        host = unquote(parts.hostname).encode('idna').decode('ascii').lower().rstrip('.')
    except UnicodeError:
        raise ValueError('invalid website hostname') from None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            address = None
    local = address and (address.is_loopback or address.is_unspecified
                         or (getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback))
    if host == 'localhost' or host.endswith('.localhost') or local:
        raise ValueError('executor-local URL requires an existing client-reachable fabric URL')


def open_url(url: str, *, target: str | None = None) -> dict:
    _check_url(url)
    return remote_request(_destination(target), {'url': url})


def register_parser(sub) -> None:
    parser = sub.add_parser('open-client', help='offer generated files on the operator desktop')
    commands = parser.add_subparsers(dest='open_action', required=True)
    for action in ('begin', 'discover', 'dismiss', 'show', 'url', 'file', 'target'):
        command = commands.add_parser(action)
        if action in {'url', 'file'}:
            command.add_argument('url' if action == 'url' else 'path')
            command.add_argument('--target')
        elif action != 'target':
            command.add_argument('--state', required=True, type=Path)
        if action == 'begin':
            command.add_argument('--root', required=True, type=Path)
        if action == 'discover':
            command.add_argument('--url', action='append', default=[],
                                 help='a link this task produced; offered alongside generated files')
        if action == 'show':
            command.add_argument('--select', required=True, nargs='+')
            command.add_argument('--target')
        command.set_defaults(func=_command)


def _command(args) -> int:
    if args.open_action == 'begin':
        result = begin(args.root, args.state)
    elif args.open_action == 'discover':
        result = discover(args.state, args.url)
    elif args.open_action == 'dismiss':
        result = dismiss(args.state)
    elif args.open_action == 'show':
        result = show(args.state, args.select, target=args.target)
    elif args.open_action == 'file':
        result = open_file(args.path, target=args.target)
    elif args.open_action == 'target':
        result = discover_target()
    else:
        result = open_url(args.url, target=args.target)
    print(json.dumps(result, ensure_ascii=False))
    receipts = result.get('results', [result])
    return int(any(item.get('dispatch') == 'failed' for item in receipts)
               or (args.open_action == 'target' and not result.get('target')))
