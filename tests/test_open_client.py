import json

import pytest

from herdr_engineering import open_client as oc


def start(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    state = tmp_path / 'state' / 'task.json'
    oc.begin(root, state)
    return root, state


def test_discovers_only_new_and_changed_content_once(tmp_path):
    root, state = start(tmp_path)
    (root / 'hello ü;$(x).html').write_text('# Hello')
    (root / 'ignored.py').write_text('pass')
    result = oc.discover(state)
    assert [x['name'] for x in result['candidates']] == ['hello ü;$(x).html']
    assert result['prompt']
    assert oc.discover(state)['candidates'] == []
    (root / 'hello ü;$(x).html').write_text('# Changed')
    assert len(oc.discover(state)['candidates']) == 1


def test_baseline_unchanged_deleted_and_symlinks_excluded(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'old.html').write_text('old')
    state = tmp_path / 'state.json'
    oc.begin(root, state)
    (root / 'old.html').unlink()
    (tmp_path / 'secret.html').write_text('secret')
    (root / 'link.html').symlink_to(tmp_path / 'secret.html')
    (root / 'node_modules').mkdir()
    (root / 'node_modules' / 'noise.md').write_text('noise')
    assert oc.discover(state)['candidates'] == []
    assert state.stat().st_mode & 0o077 == 0


def test_requires_resolved_destination_and_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, 'discover_target', lambda: {'target': None})
    root, state = start(tmp_path)
    (root / 'a.png').write_bytes(b'picture')
    candidate = oc.discover(state)['candidates'][0]
    with pytest.raises(ValueError, match='target'):
        oc.show(state, [candidate['id']], target=None)
    with pytest.raises(ValueError, match='selection'):
        oc.show(state, [], target='macbook')
    with pytest.raises(ValueError, match='selection'):
        oc.show(state, ['unknown'], target='macbook')


def test_revalidates_source_before_transfer(tmp_path):
    root, state = start(tmp_path)
    (root / 'a.png').write_text('one')
    candidate = oc.discover(state)['candidates'][0]
    (root / 'a.png').write_text('two')
    with pytest.raises(ValueError, match='changed'):
        oc.show(state, [candidate['id']], target='macbook')


def test_selected_file_transferred_and_dispatch_reported(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'safe ü;$(touch bad).html').write_text('# Hello <script>')
    candidate = oc.discover(state)['candidates'][0]
    calls = []

    def remote(target, payload):
        calls.append((target, payload))
        return {'transfer': 'verified', 'dispatch': 'requested', 'path': '/cache/view.html'}

    monkeypatch.setattr(oc, 'remote_request', remote)
    result = oc.show(state, [candidate['id']], target='me@macbook')
    assert result['results'][0]['dispatch'] == 'requested'
    assert calls[0][0] == 'me@macbook'
    assert calls[0][1]['name'].endswith('.html')
    assert json.loads(state.read_text())['pending'] == {}


def test_rejects_unsafe_targets_and_loopback_urls(monkeypatch):
    for target in ['-oProxyCommand=bad', 'host;touch bad', 'host\nother']:
        with pytest.raises(ValueError):
            oc.remote_request(target, {})
    for url in ['file:///etc/passwd', 'http://localhost:3000', 'http://127.0.0.1:3000', 'http://[::1]/']:
        with pytest.raises(ValueError):
            oc.open_url(url, target='macbook')


def test_remote_failure_keeps_selection(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'a.png').write_bytes(b'picture')
    candidate = oc.discover(state)['candidates'][0]
    monkeypatch.setattr(oc, 'remote_request', lambda *_: {'transfer': 'verified', 'dispatch': 'failed'})
    assert oc.show(state, ['all'], target='macbook')['results'][0]['dispatch'] == 'failed'
    assert candidate['id'] in json.loads(state.read_text())['pending']


def test_begin_never_overwrites_live_task(tmp_path):
    root, state = start(tmp_path)
    with pytest.raises(FileExistsError):
        oc.begin(root, state)


def test_markdown_preview_escapes_active_content():
    content = oc.markdown_preview(b'# Title\n<script>alert(1)</script>\n```\ncode\n```')
    assert b'<h1>Title</h1>' in content
    assert b'<script>' not in content
    assert b'&lt;script&gt;' in content
    assert b'<pre><code>' in content


def test_remote_program_refuses_headless_desktop(tmp_path):
    import os
    import subprocess
    import sys

    from herdr_engineering.open_client_remote import REMOTE_SCRIPT

    env = {key: value for key, value in os.environ.items()
           if key not in {'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR'}}
    script = 'import platform; platform.system = lambda: "Linux"\n' + REMOTE_SCRIPT
    completed = subprocess.run([sys.executable, '-c', script], input='{"url":"https://example.com"}',
                               text=True, capture_output=True, env=env, check=True)
    receipt = json.loads(completed.stdout)
    assert receipt['dispatch'] == 'failed'
    assert receipt['transfer'] == 'not_needed'


def test_remote_program_materializes_integrity_checked_file(tmp_path):
    import base64
    import hashlib
    import subprocess
    import sys

    from herdr_engineering.open_client_remote import REMOTE_SCRIPT

    content = b'<html>preview</html>'
    payload = {'name': 'test ü;$(touch bad).html', 'data': base64.b64encode(content).decode(),
               'sha256': hashlib.sha256(content).hexdigest()}
    prefix = (
        'import platform, subprocess, os\nfrom pathlib import Path\n'
        f'Path.home = classmethod(lambda cls: Path({str(tmp_path)!r}))\n'
        'platform.system = lambda: "Linux"\n'
        'os.environ.update(DISPLAY=":0", XDG_RUNTIME_DIR="/run/user/test")\n'
        'from types import SimpleNamespace\n'
        'subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)\n')
    completed = subprocess.run([sys.executable, '-c', prefix + REMOTE_SCRIPT],
                               input=json.dumps(payload), text=True, capture_output=True, check=True)
    receipt = json.loads(completed.stdout)
    assert receipt['transfer'] == 'verified'
    assert receipt['dispatch'] == 'requested'
    path = __import__('pathlib').Path(receipt['path'])
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o077 == 0
    assert not (tmp_path / 'bad').exists()
    payload['sha256'] = 'bad'
    completed = subprocess.run([sys.executable, '-c', prefix + REMOTE_SCRIPT],
                               input=json.dumps(payload), text=True, capture_output=True, check=True)
    assert json.loads(completed.stdout)['dispatch'] == 'failed'


def test_all_opens_only_the_current_offer(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'a.png').write_bytes(b'a')
    (root / 'b.png').write_bytes(b'b')
    first = oc.discover(state)['candidates']
    opened = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        opened.append(payload['name']) or {'dispatch': 'requested'})
    oc.show(state, [first[0]['id']], target='macbook')
    (root / 'c.png').write_bytes(b'c')
    assert [item['name'] for item in oc.discover(state)['candidates']] == ['c.png']
    oc.show(state, ['all'], target='macbook')
    assert opened == ['a.png', 'c.png']


def test_discovery_skips_non_regular_files(tmp_path):
    import os

    root, state = start(tmp_path)
    os.mkfifo(root / 'pipe.html')
    # Opening a FIFO without O_NONBLOCK would hang; inspect the scanner in a child.
    import subprocess
    import sys

    result = subprocess.run([sys.executable, '-m', 'herdr_engineering.cli', 'open-client',
                             'discover', '--state', str(state)],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert json.loads(result.stdout)['candidates'] == []


def test_html_transfers_only_bounded_local_assets(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'site').mkdir()
    (root / 'site' / 'index.html').write_text('<link href="style.css"><img src="../a.png"><a href="../private.md">doc</a>')
    (root / 'site' / 'style.css').write_text('body { background: url(../b.png) }')
    (root / 'a.png').write_bytes(b'a')
    (root / 'b.png').write_bytes(b'b')
    (root / 'private.md').write_text('not selected')
    candidate = next(item for item in oc.discover(state)['candidates'] if item['name'].endswith('.html'))
    payloads = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        payloads.append(payload) or {'dispatch': 'requested'})
    oc.show(state, [candidate['id']], target='macbook')
    assert payloads[0]['name'] == 'site/index.html'
    assert {item['name'] for item in payloads[0]['assets']} == {'site/style.css', 'a.png', 'b.png'}


def test_rejects_numeric_loopback_aliases():
    for url in ['http://2130706433:3000', 'http://[0:0:0:0:0:0:0:1]:3000',
                'http://[::ffff:127.0.0.1]:3000']:
        with pytest.raises(ValueError, match='fabric'):
            oc.open_url(url, target='macbook')


def test_html_navigation_links_are_not_required_assets(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'report.html').write_text('<a href="downloads/example.png">Download</a><a href="../README.md">Docs</a>')
    candidate = oc.discover(state)['candidates'][0]
    payloads = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        payloads.append(payload) or {'dispatch': 'requested'})
    oc.show(state, [candidate['id']], target='macbook')
    assert payloads[0]['assets'] == []


def test_markdown_is_never_an_automatic_offer(tmp_path):
    root, state = start(tmp_path)
    (root / 'notes.md').write_text('# Notes')
    (root / 'report.markdown').write_text('# Report')
    (root / 'image.png').write_bytes(b'image')
    assert [item['name'] for item in oc.discover(state)['candidates']] == ['image.png']


def test_explicit_markdown_request_uses_automatically_discovered_client(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    path = root / 'notes.md'
    path.write_text('# Notes')
    monkeypatch.setattr(oc, 'discover_target', lambda: {'target': 'active-mac', 'source': 'live_bridge'})
    sent = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        sent.append((target, payload)) or {'dispatch': 'requested'})
    receipt = oc.open_file(path)
    assert receipt['target'] == 'active-mac'
    assert sent[0][0] == 'active-mac'
    assert sent[0][1]['name'] == 'notes.md.html'


def test_selected_image_uses_automatic_client_without_a_question(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'image.png').write_bytes(b'image')
    candidate = oc.discover(state)['candidates'][0]
    monkeypatch.setattr(oc, 'discover_target', lambda: {'target': 'active-mac', 'source': 'live_bridge'})
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        {'target': target, 'dispatch': 'requested'})
    result = oc.show(state, [candidate['id']], target=None)
    assert result['results'][0]['target'] == 'active-mac'


def test_cached_target_is_not_reused_after_the_attached_client_changes(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'image.png').write_bytes(b'image')
    candidate = oc.discover(state)['candidates'][0]
    data = json.loads(state.read_text())
    data['target'] = 'old-device'
    oc._save(state, data)
    monkeypatch.setattr(oc, 'discover_target', lambda: {'target': 'new-device'})
    sent = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        sent.append(target) or {'dispatch': 'requested'})
    oc.show(state, [candidate['id']])
    assert sent == ['new-device']


def test_explicit_file_cli_needs_no_state_or_machine(tmp_path, monkeypatch, capsys):
    from herdr_engineering.cli import main

    path = tmp_path / 'notes.md'
    path.write_text('# Notes')
    monkeypatch.setattr(oc, 'discover_target', lambda: {'target': 'active-device'})
    monkeypatch.setattr(oc, 'remote_request', lambda *_: {'dispatch': 'requested'})
    assert main(['open-client', 'file', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['target'] == 'active-device'


def test_rejects_browser_normalized_loopback_hosts(monkeypatch):
    def unexpected_dispatch(*_):
        raise AssertionError('loopback must never reach dispatch')
    monkeypatch.setattr(oc, 'remote_request', unexpected_dispatch)
    for url in ['http://%31%32%37.0.0.1:3000', 'http://localhost%2e:3000',
                'http://127。0。0。1:3000']:
        with pytest.raises(ValueError, match='fabric'):
            oc.open_url(url, target='macbook')


def test_html_css_assets_with_quoted_spaces_are_preserved(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'index.html').write_text('<link href="style.css">')
    (root / 'style.css').write_text('body { background: url("my picture.png") }')
    (root / 'my picture.png').write_bytes(b'picture')
    candidate = next(item for item in oc.discover(state)['candidates'] if item['name'] == 'index.html')
    payloads = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload:
                        payloads.append(payload) or {'dispatch': 'requested'})
    oc.show(state, [candidate['id']], target='macbook')
    assert {item['name'] for item in payloads[0]['assets']} == {'style.css', 'my picture.png'}


def test_css_escaped_asset_names():
    from herdr_engineering.open_client_assets import css_unescape

    assert css_unescape(r'my\ picture.png') == 'my picture.png'
    assert css_unescape(r'my\20 picture.png') == 'my picture.png'


def test_hidden_directories_are_never_offered(tmp_path):
    root, state = start(tmp_path)
    for hidden in ('.pi-eng/cav/ui-test/out', '.worktrees/wt', '.cache'):
        (root / hidden).mkdir(parents=True)
        (root / hidden / 'screenshot.png').write_bytes(b'test-suite output')
    (root / 'report.html').write_text('<p>deliverable</p>')
    assert [x['name'] for x in oc.discover(state)['candidates']] == ['report.html']


def test_identical_content_under_many_names_is_offered_once(tmp_path):
    root, state = start(tmp_path)
    for index in range(6):
        (root / f'shot-{index}.png').write_bytes(b'the same pixels')
    (root / 'other.png').write_bytes(b'different pixels')
    result = oc.discover(state)
    assert len(result['candidates']) == 2
    assert result['duplicates_collapsed'] == 5
    assert oc.discover(state)['candidates'] == [], 'collapsed copies are not re-offered later'


def test_links_are_offered_with_files_and_only_once(tmp_path):
    root, state = start(tmp_path)
    (root / 'a.html').write_text('<p>a</p>')
    result = oc.discover(state, ['https://github.com/o/r/pull/7', 'https://github.com/o/r/pull/7', 'https://ci.example.org/run/9'])
    kinds = [x['kind'] for x in result['candidates']]
    assert kinds.count('file') == 1 and kinds.count('url') == 2
    assert result['prompt'] == 'There are 1 file and 2 links to open on your device. Open all, select, or skip?'
    assert oc.discover(state, ['https://github.com/o/r/pull/7'])['candidates'] == []
    (tmp_path / 'second').mkdir()
    only_links = oc.discover(start(tmp_path / 'second')[1], ['https://github.com/o/r/pull/8'])
    assert only_links['prompt'] == 'There are 1 link to open on your device. Open all, select, or skip?'


def test_unsafe_links_are_reported_not_offered(tmp_path):
    _, state = start(tmp_path)
    result = oc.discover(state, ['http://localhost:3000', 'file:///etc/passwd', 'https://user:pw@example.org/x',
                                 'http://127.0.0.1/', 'https://example.org/ok'])
    assert [x['url'] for x in result['candidates']] == ['https://example.org/ok']
    assert {x['url'] for x in result['skipped_urls']} == {
        'http://localhost:3000', 'file:///etc/passwd', 'https://user:pw@example.org/x', 'http://127.0.0.1/'}
    assert all(x['reason'] for x in result['skipped_urls'])


def test_selected_link_opens_on_the_discovered_client(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'a.png').write_bytes(b'picture')
    result = oc.discover(state, ['https://github.com/o/r/pull/7'])
    link = next(x for x in result['candidates'] if x['kind'] == 'url')
    calls = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload: calls.append((target, payload)) or {'dispatch': 'requested'})
    shown = oc.show(state, [link['id']], target='me@macbook')
    assert calls == [('me@macbook', {'url': 'https://github.com/o/r/pull/7'})]
    assert shown['results'][0]['dispatch'] == 'requested'
    assert json.loads(state.read_text())['pending'] == {}, 'skipping the file dropped it from the offer'


def test_all_opens_files_and_links_of_the_current_offer(tmp_path, monkeypatch):
    root, state = start(tmp_path)
    (root / 'a.png').write_bytes(b'picture')
    oc.discover(state, ['https://example.org/one'])
    calls = []
    monkeypatch.setattr(oc, 'remote_request', lambda target, payload: calls.append(payload) or {'dispatch': 'requested'})
    oc.show(state, ['all'], target='macbook')
    assert sorted('url' in payload for payload in calls) == [False, True], 'one file and one link, both in this offer'


def test_dismiss_drops_pending_links(tmp_path):
    _, state = start(tmp_path)
    oc.discover(state, ['https://example.org/one'])
    oc.dismiss(state)
    assert json.loads(state.read_text())['pending'] == {}
    assert oc.discover(state, ['https://example.org/one'])['candidates'] == [], 'a dismissed link is not asked again'


def test_discover_cli_accepts_repeated_url_flags(tmp_path, capsys):
    import argparse
    root, state = start(tmp_path)
    parser = argparse.ArgumentParser()
    oc.register_parser(parser.add_subparsers())
    args = parser.parse_args(['open-client', 'discover', '--state', str(state),
                              '--url', 'https://example.org/a', '--url', 'https://example.org/b'])
    assert oc._command(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert [x['url'] for x in out['candidates']] == ['https://example.org/a', 'https://example.org/b']
