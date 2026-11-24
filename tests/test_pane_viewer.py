"""The shared pane loop, driven through a real pty with real window-size changes."""
import fcntl
import os
import pty
import selectors
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

REPO = str(Path(__file__).resolve().parents[1])

CHILD = r'''
import os, sys, termios
sys.path.insert(0, {repo!r})
from herdr_engineering.pane_viewer import run_viewer
log = open({log!r}, "a")
N = int(os.environ.get("DOC_LINES", "100"))
def render(w, h):
    log.write(f"{{w}},{{h}}\n"); log.flush()
    if os.environ.get("MODE") == "error":
        raise RuntimeError("boom")
    return [f"L{{i:03d}} w{{w}} " + "x" * max(w - 12, 0) for i in range(N)]
before = termios.tcgetattr(0)
rc = run_viewer(render, scroll=os.environ.get("SCROLL", "1") == "1")
open({result!r}, "w").write(f"{{rc}} {{termios.tcgetattr(0) == before}}")
sys.exit(rc)
'''


class Session:
    def __init__(self, tmp_path, cols=80, rows=24, **env):
        self.log, self.result = tmp_path / "calls.log", tmp_path / "result.txt"
        self.out = b""
        script = CHILD.format(repo=REPO, log=str(self.log), result=str(self.result))
        pid, fd = pty.fork()
        if pid == 0:                                         # child: pty is its controlling terminal
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.execvpe(sys.executable, [sys.executable, "-c", script], {**os.environ, **env})
        self.pid, self.fd = pid, fd

    def read_until(self, needle: bytes, timeout=8.0, since=0):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.out[since:]:
                return True
            if self._readable(0.1):
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.out += chunk
        return needle in self.out[since:]

    def _readable(self, timeout):
        with selectors.DefaultSelector() as sel:             # epoll/poll: select() cannot take fds >= 1024
            sel.register(self.fd, selectors.EVENT_READ)
            return bool(sel.select(timeout))

    def resize(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def send(self, data: bytes):
        os.write(self.fd, data)

    def finish(self, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.read_until(b"\0", timeout=0.2)          # drain what is left
                return os.waitstatus_to_exitcode(status)
            self.read_until(b"\0", timeout=0.1)
        os.kill(self.pid, 9)
        raise AssertionError("viewer did not exit")

    def calls(self):
        return [tuple(map(int, line.split(","))) for line in self.log.read_text().split()]


@pytest.fixture
def make(tmp_path):
    made = []

    def start(**kw):
        s = Session(tmp_path, **kw)
        made.append(s)
        return s
    yield start
    for s in made:
        try:
            os.kill(s.pid, 9)
            os.waitpid(s.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def test_draws_to_the_current_size_and_quits_on_q(make):
    s = make(cols=80, rows=24)
    assert s.read_until(b"L000 w80") and s.read_until(b"q closes this pane")
    assert s.calls()[0] == (80, 23)                              # one row is the footer
    s.send(b"q")
    assert s.finish() == 0
    assert b"\x1b[?1049l" in s.out and b"\x1b[?25h" in s.out     # alternate screen left, cursor back
    assert s.result.read_text() == "0 True"                      # terminal attributes restored


def test_resize_redraws_at_the_new_width(make):
    s = make(cols=80, rows=24)
    assert s.read_until(b"L000 w80")
    mark = len(s.out)
    s.resize(40, 10)
    assert s.read_until(b"w40", since=mark)
    assert (40, 9) in s.calls()
    assert b"w80" not in s.out[s.out.index(b"w40"):]             # nothing stale after the redraw
    s.send(b"q")
    assert s.finish() == 0


def test_scroll_keys_move_through_the_document(make):
    s = make(cols=80, rows=24)
    assert s.read_until(b"L000 w80")
    mark = len(s.out)
    s.send(b"G")
    assert s.read_until(b"L099", since=mark)
    mark = len(s.out)
    s.send(b"g")
    assert s.read_until(b"L000", since=mark)
    s.send(b"q")
    assert s.finish() == 0


def test_resize_keeps_reading_place(make):
    s = make(cols=80, rows=24)
    assert s.read_until(b"L000 w80")
    for _ in range(2):                                           # two pages down: top line 46 of 100
        mark = len(s.out)
        s.send(b" ")
        assert s.read_until(b"w80", since=mark)
    assert s.read_until(b"L046 w80")
    mark = len(s.out)
    s.resize(40, 10)
    assert s.read_until(b"L046 w40", since=mark)                 # the same place, not the top
    s.send(b"q")
    assert s.finish() == 0


def test_terminal_is_restored_when_rendering_fails(make):
    s = make(MODE="error")
    assert s.finish() == 1
    assert b"\x1b[?25h" in s.out and s.result.read_text() == "1 True"


def test_resize_storm_ends_with_one_correct_frame(make):
    s = make(cols=80, rows=24)
    assert s.read_until(b"L000 w80")
    for i in range(20):
        s.resize(60 - i, 20 - i % 5)
    s.resize(50, 12)
    assert s.read_until(b"w50 ")
    time.sleep(0.5)
    s.read_until(b"\0", timeout=0.3)
    assert s.calls()[-1] == (50, 11) and len(s.calls()) <= 22
    assert b"w50" in s.out[-4000:]
    s.send(b"q")
    assert s.finish() == 0


def test_non_scrolling_document_ignores_scroll_keys(make):
    s = make(cols=80, rows=24, SCROLL="0")
    assert s.read_until(b"L000 w80")
    s.send(b"G \x1b[6~")
    time.sleep(0.6)
    s.read_until(b"\0", timeout=0.3)
    assert b"L099" not in s.out and b"L030" not in s.out
    s.send(b"q")
    assert s.finish() == 0


def test_session_helper_works_when_the_test_process_holds_over_a_thousand_descriptors(tmp_path):
    held = []
    try:
        while len(held) < 1100:                              # a long suite leaks enough to push new fds past FD_SETSIZE
            held.append(os.dup(2))
        s = Session(tmp_path)
        assert s.read_until(b"q closes this pane")
        s.send(b"q")
        assert s.finish() == 0
    finally:
        for fd in held:
            os.close(fd)
