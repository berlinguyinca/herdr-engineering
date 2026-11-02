"""Pty relay core: a child on a real pty, output kept as numbered base64 chunks."""
import base64
import os
import sys
import threading
import time

import pytest

from herdr_engineering.control_plane.rakazo_terminal import TerminalSession, attach_argv
from herdr_engineering.control_plane.terminal_attach import AttachRefused

# os.fork in a threaded test process warns; the child only execs.
pytestmark = pytest.mark.filterwarnings('ignore:This process .* is multi-threaded:DeprecationWarning')

LIVE = {'session_id': 'herdr:beast:default:wK%3ApH', 'host_id': 'beast', 'herdr_session': 'default',
        'pane_id': 'wK:pH', 'terminal_id': 'term_65df79f50e51582', 'status': 'idle'}


@pytest.fixture
def make():
    made = []

    def _make(argv, rows=24, cols=80, **kwargs):
        s = TerminalSession(argv, rows, cols, **kwargs)
        made.append(s)
        s.start()
        return s

    yield _make
    for s in made:
        s.close()


def collect(session, *, until, cursor=0, timeout=3.0):
    """Poll chunks_after until ``until(text, chunks)`` holds; returns (text, chunks)."""
    deadline = time.monotonic() + timeout
    text, chunks = b'', []
    while time.monotonic() < deadline:
        for chunk in session.chunks_after(cursor):
            chunks.append(chunk)
            cursor = chunk['seq']
            text += base64.b64decode(chunk.get('data', ''))
        if until(text, chunks):
            break
        time.sleep(0.01)
    return text, chunks


def test_output_arrives_as_base64_chunks_with_increasing_seq(make):
    s = make(['bash', '-c', 'echo one; sleep 0.1; echo two'])
    text, chunks = collect(s, until=lambda t, c: b'two' in t)
    assert b'one' in text and b'two' in text
    seqs = [c['seq'] for c in chunks]
    assert seqs == sorted(set(seqs)) and seqs[0] >= 1
    assert s.snapshot_seq() >= seqs[-1]


def test_write_reaches_the_child_and_echoes(make):
    s = make(['cat'])
    s.write(b'hi\n')
    text, _ = collect(s, until=lambda t, c: t.count(b'hi') >= 2)  # tty echo + cat's output
    assert text.count(b'hi') >= 2


def test_resize_is_seen_by_the_child(make):
    s = make(['bash', '-c', 'read x; stty size'])
    s.resize(40, 120)
    s.write(b'go\n')
    text, _ = collect(s, until=lambda t, c: b'40 120' in t)
    assert b'40 120' in text


def test_initial_size_is_applied(make):
    s = make(['bash', '-c', 'stty size'], rows=30, cols=100)
    text, _ = collect(s, until=lambda t, c: b'30 100' in t)
    assert b'30 100' in text


def test_ctrl_c_interrupts_a_foreground_sleep(make):
    s = make(['bash', '-c', 'sleep 30; echo after'])
    time.sleep(0.2)
    s.write(b'\x03')
    _, chunks = collect(s, until=lambda t, c: any(x.get('ended') for x in c))
    assert chunks[-1].get('ended') is True and not s.alive


def test_buffer_over_the_cap_drops_oldest_and_stale_cursor_gets_reset(make):
    s = make(['bash', '-c', 'for i in $(seq 1 400); do echo line-$i-xxxxxxxxxxxxxxxxxxxx; done'],
             buffer_bytes=2000)
    collect(s, until=lambda t, c: any(x.get('ended') for x in c))
    first = s.chunks_after(0)
    assert first[0].get('reset') is True
    assert sum(len(base64.b64decode(c.get('data', ''))) for c in first) < 4000
    assert not any(c.get('reset') for c in s.chunks_after(first[-1]['seq']))


def test_current_cursor_never_gets_a_reset(make):
    s = make(['bash', '-c', 'echo a; echo b'])
    collect(s, until=lambda t, c: any(x.get('ended') for x in c))
    chunks = s.chunks_after(0)
    assert not any(c.get('reset') for c in chunks)
    assert s.chunks_after(chunks[-1]['seq']) == []


def test_child_exit_yields_ended_chunk_with_exit_code(make):
    s = make(['bash', '-c', 'echo bye; exit 3'])
    _, chunks = collect(s, until=lambda t, c: any(x.get('ended') for x in c))
    ended = chunks[-1]
    assert ended['ended'] is True and ended['exit'] == 3
    assert ended['seq'] == s.snapshot_seq() and s.alive is False


def test_close_kills_a_running_child(make):
    s = make(['sleep', '30'])
    assert s.alive
    s.close()
    assert not s.alive


def test_write_after_exit_is_ignored(make):
    s = make(['bash', '-c', 'exit 0'])
    collect(s, until=lambda t, c: any(x.get('ended') for x in c))
    s.write(b'x')  # must not raise


PROBE = ("import os\nfor f in range(3, 256):\n"
         "    try: os.fstat(f); print('OPEN', f)\n    except OSError: pass\nprint('DONE')")


def test_a_second_session_does_not_inherit_the_first_sessions_fds(make):
    a = make(['cat'])
    b = make([sys.executable, '-c', PROBE])
    text, _ = collect(b, until=lambda t, c: b'DONE' in t)
    assert b'DONE' in text and b'OPEN' not in text, text
    assert a.alive


@pytest.mark.parametrize('rows,cols', [(0, 80), (24, 0), (-1, 80), (24, -5), (70000, 80), (24, 70000),
                                       (501, 80), (24, 1001), ('24', 80), (24.0, 80), (True, 80), (None, 80)])
def test_bad_size_is_rejected_before_forking(monkeypatch, rows, cols):
    forked = []
    monkeypatch.setattr(os, 'fork', lambda: forked.append(1) or 1)
    with pytest.raises(ValueError):
        TerminalSession(['cat'], rows, cols)
    assert forked == []


def test_bad_resize_is_rejected_and_not_stored(make):
    s = make(['cat'], rows=24, cols=80)
    for rows, cols in [(0, 80), (24, 1001), ('x', 80)]:
        with pytest.raises(ValueError):
            s.resize(rows, cols)
    assert (s.rows, s.cols) == (24, 80)


def test_close_is_prompt_with_a_hup_ignoring_grandchild(make):
    s = make(['bash', '-c', "(trap '' HUP; sleep 30) & sleep 30"])
    time.sleep(0.2)
    started = time.monotonic()
    s.close()
    assert time.monotonic() - started < 2
    assert not s.alive and not s._reader.is_alive()
    s.write(b'x')
    s.resize(10, 10)  # no-ops after close, never touching a recycled fd
    assert threading.active_count() >= 1


def test_closed_session_does_not_touch_a_reused_fd(make):
    s = make(['cat'])
    s.close()
    r, w = __import__('os').pipe()
    try:
        s.write(b'leak')
        s.resize(10, 10)
        __import__('os').set_blocking(r, False)
        try:
            leaked = __import__('os').read(r, 10)
        except BlockingIOError:
            leaked = b''
        assert leaked == b''
    finally:
        __import__('os').close(r)
        __import__('os').close(w)


def test_start_twice_raises(make):
    s = make(['cat'])
    with pytest.raises(RuntimeError):
        s.start()


def test_buffer_cap_is_soft_the_newest_chunk_is_always_kept(make):
    s = make(['bash', '-c', 'head -c 5000 /dev/zero | tr "\\0" x'], buffer_bytes=10)
    collect(s, until=lambda t, c: any(x.get('ended') for x in c))
    assert any(base64.b64decode(c.get('data', '')) for c in s.chunks_after(0))


def _fds():
    return len(os.listdir('/dev/fd'))


def test_a_command_not_on_path_is_never_resolved_against_the_cwd(tmp_path, monkeypatch):
    marker = tmp_path / 'ran'
    script = tmp_path / 'zz-not-on-path'
    script.write_text(f'#!/bin/sh\ntouch {marker}\n')
    script.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    before = _fds()
    with pytest.raises(FileNotFoundError):
        TerminalSession(['zz-not-on-path'], 24, 80).start()
    time.sleep(0.1)
    assert not marker.exists() and _fds() == before


def test_failed_openpty_leaks_no_fds(monkeypatch):
    def boom():
        raise OSError('no ptys')
    monkeypatch.setattr(os, 'openpty', boom)
    before = _fds()
    with pytest.raises(OSError):
        TerminalSession(['cat'], 24, 80).start()
    assert _fds() == before


def test_failed_fork_leaks_no_fds(monkeypatch):
    def boom():
        raise OSError('no procs')
    monkeypatch.setattr(os, 'fork', boom)
    before = _fds()
    with pytest.raises(OSError):
        TerminalSession(['cat'], 24, 80).start()
    assert _fds() == before


def test_failed_thread_start_reaps_the_child_and_leaks_nothing(monkeypatch):
    def boom(self):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, 'start', boom)
    before = _fds()
    s = TerminalSession(['sleep', '30'], 24, 80)
    with pytest.raises(RuntimeError):
        s.start()
    assert _fds() == before and not s.alive
    s.close()  # must not join an unstarted thread


def test_concurrent_close_is_safe(make):
    s = make(['cat'])
    errors = []

    def closer():
        try:
            s.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=closer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and not s.alive


def test_child_gets_default_sigpipe(make):
    # Under an ignored SIGPIPE `yes` exits 1 on EPIPE; under the default it dies with 141.
    s = make(['bash', '-c', 'yes | head -1 >/dev/null; echo "C${PIPESTATUS[0]}"'])
    text, _ = collect(s, until=lambda t, c: b'C1' in t)
    assert b'C141' in text


# ------------------------------------------------------------------ attach_argv
def test_local_host_runs_herdr_attach_directly():
    assert attach_argv(LIVE, local_host='beast') == ['herdr', '--session', 'default', 'agent', 'attach', 'wK:pH']


def test_remote_host_goes_through_ssh_with_a_login_shell():
    assert attach_argv(LIVE, local_host='bender') == [
        'ssh', '-tt', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'beast',
        "bash -lc 'herdr --session default agent attach wK:pH'"]


@pytest.mark.parametrize('changes', [{'status': 'stopped'}, {'pane_id': None}, {'terminal_id': ''}])
def test_dead_session_is_refused(changes):
    with pytest.raises(AttachRefused) as exc:
        attach_argv({**LIVE, **changes}, local_host='beast')
    assert exc.value.body['reason'] == 'not_live'


@pytest.mark.parametrize('field,value', [('host_id', '-oProxyCommand=sh'), ('pane_id', 'w1:p1`id`'),
                                         ('herdr_session', "x'$(id)'")])
def test_unsafe_identity_is_refused(field, value):
    with pytest.raises(AttachRefused) as exc:
        attach_argv({**LIVE, field: value}, local_host='bender')
    assert exc.value.status == 409 and exc.value.body['reason'] == 'unsafe_identity'
