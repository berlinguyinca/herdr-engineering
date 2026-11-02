"""Host terminal relay: a fake control plane that speaks the real wait/output contract and a fake herdr client."""
import base64
import copy
import http.server
import logging
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from herdr_engineering import terminal_relay as relay_mod
from herdr_engineering.herdr_pane_client import HerdrError
from herdr_engineering.terminal_relay import ControlPlaneHttp, RelayLocked, TerminalRelay

HOST = "bender"
KEY = "ingest-key-SECRET-9d41"
EPOCH = "0a1b2c3d"
TID = "term_aaaaaaaaaaaaaaaaaaaaaaaa"
TID2 = "term_bbbbbbbbbbbbbbbbbbbbbbbb"
CLEAR = "\x1b[0m\x1b[H\x1b[2J"


def b64(s):
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FakeCP:
    """The broker's host contract: ack by ``after``, redelivery of un-acked commands, replaced waiter []."""

    def __init__(self):
        self.cond = threading.Condition()
        self.queue = []
        self.counter = 0
        self.waits = []          # (host_id, timeout, after) per wait
        self.frames = {}         # terminal -> frames pushed
        self.posts = []          # (terminal, host_id, frame count)
        self.fail_waits = 0      # raise OSError for this many waits
        self.status_waits = []   # statuses to answer the next waits with (401/403/...)
        self.replace = 0         # answer this many waits with [] at once (a newer waiter took over)
        self.post_status = {}    # terminal -> status to answer pushes with
        self.drop_after_read = 0  # lose the response of this many waits that had commands

    def push(self, kind, **fields):
        with self.cond:
            self.counter += 1
            cmd = {"id": f"{EPOCH}-{self.counter}-r{self.counter:04d}", "kind": kind, **fields}
            self.queue.append(cmd)
            self.cond.notify_all()
            return cmd

    def redeliver(self, cmd):
        with self.cond:
            self.queue.append(copy.deepcopy(cmd))

    def request(self, method, path, body=None, timeout=10.0):
        url = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/v1/host/terminal/wait" and method == "GET":
            return self._wait(q)
        parts = url.path.split("/")
        if method == "POST" and len(parts) == 7 and parts[5:] and parts[6] == "output":
            tid = parts[5]
            with self.cond:
                self.posts.append((tid, q.get("host_id"), len(body["frames"])))
                status = self.post_status.get(tid, 200)
                if status == 200:
                    self.frames.setdefault(tid, []).extend(copy.deepcopy(body["frames"]))
                return status, ({"ok": True} if status == 200 else {"error": "x"})
        return 404, None

    def _wait(self, q):
        after = q.get("after")
        with self.cond:
            self.waits.append((q.get("host_id"), q.get("timeout"), after))
            if self.fail_waits:
                self.fail_waits -= 1
                raise OSError("URLError")
            if self.status_waits:
                return self.status_waits.pop(0), None
            if after:
                epoch, counter, _ = after.split("-", 2)
                if epoch == EPOCH:
                    self.queue = [c for c in self.queue if int(c["id"].split("-")[1]) > int(counter)]
            if self.replace:
                self.replace -= 1
                return 200, {"commands": []}
            if not self.queue:
                self.cond.wait(0.05)
            out = copy.deepcopy(self.queue)
            if out and self.drop_after_read:
                self.drop_after_read -= 1
                raise OSError("lost response")
            return 200, {"commands": out}

    def typed_frames(self, tid):
        with self.cond:
            return [f for f in self.frames.get(tid, []) if "data" in f]

    def ended(self, tid):
        with self.cond:
            return [f for f in self.frames.get(tid, []) if f.get("ended")]


class FakeClient:
    def __init__(self, panes=None):
        self.lock = threading.Lock()
        self.screens = dict(panes if panes is not None else {"p1": "$ ", "p2": "# "})
        self.sent = []           # (method, pane, value)
        self.down = False
        self.reads = 0
        self.hook = None
        self.counter = 0

    def list_panes(self):
        if self.down:
            raise OSError("herdr socket")
        with self.lock:
            return [{"pane_id": p, "agent": "x"} for p in self.screens]

    def layout(self, pane):
        return {"width": 80, "height": 24, "rect": {}}

    def read_visible(self, pane):
        if self.down:
            raise OSError("herdr socket")
        with self.lock:
            self.reads += 1
            if pane not in self.screens:
                raise HerdrError("pane_not_found", "no such pane")
            screen = self.screens[pane]
            return screen() if callable(screen) else screen

    def _rec(self, method, pane, value):
        with self.lock:
            self.sent.append((method, pane, value))
        if self.hook:
            self.hook(method, pane, value)

    def send_text(self, pane, text):
        self._rec("text", pane, text)

    def send_keys(self, pane, keys):
        self._rec("keys", pane, list(keys))

    def send_input(self, pane, data):
        self._rec("raw", pane, bytes(data))

    def zoom(self, pane, on):
        self._rec("zoom", pane, on)

    def resize_split(self, pane, direction, amount):
        self._rec("resize_split", pane, (direction, amount))

    def texts(self):
        with self.lock:
            return [v for m, _, v in self.sent if m == "text"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(relay_mod, "_FAIL_PAUSE", 0.01)


@pytest.fixture
def env(tmp_path):
    cfg = SimpleNamespace(host_id=HOST, api_key=KEY, control_plane_url="http://cp.invalid")
    cp, client = FakeCP(), FakeClient()
    started = []

    def make(**kw):
        kw.setdefault("state_dir", tmp_path)
        r = TerminalRelay(cfg, kw.pop("client", client), kw.pop("http", cp), **kw)
        return r

    def start(r):
        t = threading.Thread(target=r.run_forever, daemon=True)
        t.start()
        started.append((r, t))
        return t

    e = SimpleNamespace(cfg=cfg, cp=cp, client=client, make=make, start=start, tmp=tmp_path)
    yield e
    for r, t in started:
        r.stop()
        t.join(5)
    assert not [t for t in threading.enumerate() if t.name.startswith("terminal-")]


def open_cmd(cp, tid=TID, pane="p1"):
    return cp.push("open", terminal=tid, pane_id=pane, rows=24, cols=80)


def inp(cp, n, text, tid=TID):
    return cp.push("input", terminal=tid, n=n, data=b64(text))


def test_open_pushes_first_frame_at_once_then_only_changes(env):
    r = env.make()
    env.start(r)
    cmd = open_cmd(env.cp)
    assert wait_until(lambda: len(env.cp.typed_frames(TID)) == 1)
    first = env.cp.typed_frames(TID)[0]
    assert base64.b64decode(first["data"]).decode().startswith(CLEAR + "$ ")
    assert (first["rows"], first["cols"]) == (24, 80)
    time.sleep(0.4)
    assert len(env.cp.typed_frames(TID)) == 1       # an unchanged screen is never sent twice
    env.client.screens["p1"] = "$ ls"
    assert wait_until(lambda: len(env.cp.typed_frames(TID)) == 2)
    assert all(host == HOST for _, host, _ in env.cp.posts)
    first_wait = env.cp.waits[0]
    assert first_wait == (HOST, "25", None)          # no `after` until something was handled
    assert wait_until(lambda: any(w[2] == cmd["id"] for w in env.cp.waits))


def test_after_is_acknowledged_only_after_the_command_was_handled(env):
    seen = []

    def hook(method, pane, value):
        seen.append(set(w[2] for w in env.cp.waits))
    env.client.hook = hook
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    c1, c2 = inp(env.cp, 1, "a"), inp(env.cp, 2, "b")
    assert wait_until(lambda: any(w[2] == c2["id"] for w in env.cp.waits))
    assert env.client.texts() == ["a", "b"]
    assert c1["id"] not in seen[0] and c2["id"] not in seen[1]
    assert not any(w[2] == c1["id"] for w in env.cp.waits)   # one ack, for the last of the batch


def test_frames_coalesce_to_ten_per_second_on_a_busy_pane(env):
    n = [0]

    def busy():
        n[0] += 1
        return f"line {n[0]}"
    env.client.screens["p1"] = busy
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    time.sleep(1.5)
    count = len(env.cp.typed_frames(TID))
    assert 3 <= count <= 17
    assert max(c for _, _, c in env.cp.posts) <= 20


def test_input_runs_in_order_and_is_deduped_by_id_and_by_terminal_n(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    c1 = inp(env.cp, 1, "ab")
    inp(env.cp, 2, "c")
    env.cp.redeliver(c1)                           # same id again
    env.cp.push("input", terminal=TID, n=2, data=b64("zzz"))   # new id, same (terminal, n)
    inp(env.cp, 3, "d")
    assert wait_until(lambda: env.client.texts() == ["ab", "c", "d"])
    time.sleep(0.2)
    assert env.client.texts() == ["ab", "c", "d"]


def test_a_gap_in_n_ends_that_terminal_and_stops_typing(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    open_cmd(env.cp, TID2, "p2")
    inp(env.cp, 1, "a")
    inp(env.cp, 3, "c")                            # n=2 never came
    inp(env.cp, 2, "late-b")
    inp(env.cp, 1, "other", TID2)
    assert wait_until(lambda: env.cp.ended(TID) and "other" in env.client.texts())
    assert env.cp.ended(TID)[0] == {"ended": True, "exit": None, "reason": "input_gap"}
    time.sleep(0.2)
    assert env.client.texts().count("a") == 1 and "c" not in env.client.texts()
    assert "late-b" not in env.client.texts()
    assert not env.cp.ended(TID2)                  # the other terminal is untouched


def test_a_100kb_paste_is_chunked_and_bounded(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    inp(env.cp, 1, "x" * 65_536)                   # the phone sends at most 64 KB per call
    inp(env.cp, 2, "x" * 34_464)
    assert wait_until(lambda: sum(len(t) for t in env.client.texts()) == 100_000)
    assert max(len(t.encode()) for t in env.client.texts()) <= 4096
    assert len(env.client.texts()) < 100


def test_redelivery_after_a_restart_mid_batch_types_nothing_twice(env):
    r1 = env.make()

    def hook(method, pane, value):
        if value == "b":
            r1.stop()                              # the relay dies after typing the second input
    env.client.hook = hook
    t1 = env.start(r1)
    open_cmd(env.cp)
    inp(env.cp, 1, "a"), inp(env.cp, 2, "b"), inp(env.cp, 3, "c")
    t1.join(5)
    assert not t1.is_alive()
    assert env.client.texts() == ["a", "b"]        # the batch was never acknowledged
    env.client.hook = None
    r2 = env.make()
    env.start(r2)
    assert wait_until(lambda: env.client.texts() == ["a", "b", "c"])
    time.sleep(0.2)
    assert env.client.texts() == ["a", "b", "c"]
    assert env.cp.waits[-1][2] is not None


def test_a_pane_that_disappears_ends_the_terminal(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.typed_frames(TID))
    del env.client.screens["p1"]
    assert wait_until(lambda: env.cp.ended(TID))
    assert env.cp.ended(TID)[0] == {"ended": True, "exit": None, "reason": "pane_closed"}
    assert wait_until(lambda: r.active_terminals() == 0)
    inp(env.cp, 1, "nope")
    time.sleep(0.2)
    assert env.client.texts() == []


def test_herdr_unreachable_ends_with_herdr_unavailable(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.typed_frames(TID))
    env.client.down = True
    assert wait_until(lambda: env.cp.ended(TID))
    assert env.cp.ended(TID)[0]["reason"] == "herdr_unavailable"
    assert wait_until(lambda: r.active_terminals() == 0)


@pytest.mark.parametrize("pane", ["../etc/passwd", "p1; rm -rf /", "", "a" * 80, "w9:p9"])
def test_invalid_or_unlisted_panes_are_refused(env, pane):
    r = env.make()
    env.start(r)
    env.cp.push("open", terminal=TID, pane_id=pane, rows=24, cols=80)
    assert wait_until(lambda: env.cp.ended(TID))
    assert env.cp.ended(TID)[0]["exit"] is None
    assert r.active_terminals() == 0 and env.cp.typed_frames(TID) == []
    assert env.client.reads == 0


def test_a_malformed_terminal_id_is_ignored_without_a_crash(env):
    r = env.make()
    env.start(r)
    env.cp.push("open", terminal="../x", pane_id="p1", rows=24, cols=80)
    open_cmd(env.cp, TID2, "p2")
    assert wait_until(lambda: env.cp.typed_frames(TID2))
    assert list(env.cp.frames) == [TID2]


def test_close_stops_the_mirror_thread_and_forgets_the_terminal(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: any(t.name.startswith("terminal-mirror") for t in threading.enumerate()))
    env.cp.push("close", terminal=TID)
    assert wait_until(lambda: r.active_terminals() == 0)
    assert wait_until(lambda: not any(t.name.startswith("terminal-mirror") for t in threading.enumerate()))
    inp(env.cp, 1, "late")
    time.sleep(0.2)
    assert env.client.texts() == []


def test_at_most_four_mirror_threads(env):
    env.client.screens.update({f"q{i}": "x" for i in range(6)})
    r = env.make()
    env.start(r)
    tids = [f"term_{i:024d}" for i in range(5)]
    for i, t in enumerate(tids):
        open_cmd(env.cp, t, f"q{i}")
    assert wait_until(lambda: env.cp.ended(tids[4]))
    assert env.cp.ended(tids[4])[0]["reason"] == "too_many_terminals"
    mirrors = [t for t in threading.enumerate() if t.name.startswith("terminal-mirror")]
    assert len(mirrors) == 4 and r.active_terminals() == 4


def test_lone_escape_is_flushed_after_silence(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    env.cp.push("input", terminal=TID, n=1, data=b64(b"\x1b"))
    assert wait_until(lambda: ("keys", "p1", ["esc"]) in env.client.sent)


def test_zoom_goes_to_herdr_and_resize_never_touches_the_pane(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    env.cp.push("resize", terminal=TID, rows=40, cols=100)
    env.cp.push("zoom", terminal=TID, zoom=True)
    env.cp.push("zoom", terminal=TID, zoom=False)
    assert wait_until(lambda: [s for s in env.client.sent if s[0] == "zoom"] ==
                      [("zoom", "p1", True), ("zoom", "p1", False)])
    assert [s for s in env.client.sent if s[0] not in ("zoom",)] == []


def test_a_retired_terminal_answered_404_stops_typing_and_its_mirror(env):
    env.cp.post_status[TID] = 404                  # the broker ended it (stale, input_overflow, host_offline)
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.posts and r.active_terminals() == 0)
    inp(env.cp, 1, "x")
    time.sleep(0.2)
    assert env.client.texts() == []


def test_poll_errors_reconnect_with_backoff_and_lose_nothing(env):
    sleeps = []
    env.cp.fail_waits = 5
    env.cp.drop_after_read = 1
    r = env.make(sleep=lambda s: sleeps.append(s), rng=lambda: 0.0)
    open_cmd(env.cp)
    inp(env.cp, 1, "hi")
    env.start(r)
    assert wait_until(lambda: env.client.texts() == ["hi"])
    assert wait_until(lambda: len(sleeps) >= 6)
    assert [round(s, 2) for s in sleeps[:5]] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert sleeps[5] == 8.0                         # the lost response is one more failed poll: still capped
    time.sleep(0.2)
    assert env.client.texts() == ["hi"]             # the redelivery typed nothing twice


def test_backoff_has_jitter_and_stays_within_eight_seconds(env):
    sleeps = []
    env.cp.fail_waits = 12
    r = env.make(sleep=lambda s: sleeps.append(s))
    env.start(r)
    assert wait_until(lambda: len(sleeps) >= 12)
    assert 0.35 <= sleeps[0] <= 0.65 and max(sleeps) <= 8.0
    assert len(set(sleeps[-4:])) > 1                # jitter, not a constant


def test_a_replaced_waiter_polls_again_at_once(env):
    sleeps = []
    env.cp.replace = 3
    r = env.make(sleep=lambda s: sleeps.append(s))
    env.start(r)
    assert wait_until(lambda: len(env.cp.waits) >= 5)
    assert sleeps == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_back_off_to_30s_and_log_once_a_minute(env, caplog, status):
    sleeps = []
    env.cp.status_waits = [status] * 4
    r = env.make(sleep=lambda s: sleeps.append(s), clock=lambda: 1000.0)
    with caplog.at_level(logging.DEBUG, logger="herdr_engineering.terminal_relay"):
        env.start(r)
        assert wait_until(lambda: len(sleeps) >= 4)
    assert all(30 <= s <= 36 for s in sleeps[:4])
    warned = [x for x in caplog.records if x.levelno >= logging.WARNING and str(status) in x.getMessage()]
    assert len(warned) == 1


def test_the_lock_file_refuses_a_second_relay(env):
    a = env.make()
    a.acquire_lock()
    b = env.make()
    with pytest.raises(RelayLocked):
        b.run_forever()
    a.stop()
    c = env.make()
    c.acquire_lock()
    c.stop()


def test_hostile_output_is_only_forwarded_and_capped(env, monkeypatch):
    import os
    import subprocess

    def boom(*a, **k):
        raise AssertionError("the relay must never start a process")
    for obj, name in ((subprocess, "Popen"), (subprocess, "run"), (os, "system"), (os, "execv"), (os, "popen")):
        monkeypatch.setattr(obj, name, boom)
    hostile = "\x1b]52;c;ZWNobyBwd25lZA==\x07\x1b[2J\x1b[?1049h$(touch /tmp/pwned)\n" + "A" * 1_000_000
    env.client.screens["p1"] = hostile
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.typed_frames(TID))
    frame = env.cp.typed_frames(TID)[0]
    assert len(frame["data"]) <= 160 * 1024
    assert max(c for _, _, c in env.cp.posts) <= 20
    assert env.client.sent == []                    # nothing was typed, nothing executed
    assert "subprocess" not in open(relay_mod.__file__).read()


def test_no_screen_input_key_or_terminal_id_reaches_the_logs(env, caplog):
    env.client.screens["p1"] = "SCREEN-SECRET-xyz"
    typed = "TYPED-SECRET-123"
    caplog.set_level(logging.DEBUG)
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    inp(env.cp, 1, typed)
    env.cp.push("input", terminal=TID, n=2, data=b64(b"\xff\xfe"))   # invalid UTF-8: dropped, counted
    inp(env.cp, 4, "gap")                           # a gap: refused, logged
    assert wait_until(lambda: env.cp.ended(TID))
    env.cp.push("close", terminal=TID2)
    env.cp.push("open", terminal=TID2, pane_id="nope", rows=1, cols=1)
    assert wait_until(lambda: env.cp.ended(TID2))
    r.stop()
    text = "\n".join(x.getMessage() for x in caplog.records)
    for secret in ("SCREEN-SECRET", b64("SCREEN-SECRET-xyz"), typed, b64(typed), b64(b"\xff\xfe"), KEY, TID, TID2):
        assert secret not in text
    assert "dropped 2 bytes" in text


class _Redirect(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        if self.server.target:
            self.send_response(302)
            self.send_header("Location", self.server.target + "/stolen")
            self.end_headers()
        else:
            body = b'{"commands": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


def test_control_plane_http_sends_the_key_and_never_follows_a_redirect():
    other, cp = (http.server.HTTPServer(("127.0.0.1", 0), _Redirect) for _ in range(2))
    other.target = None
    cp.target = f"http://127.0.0.1:{other.server_port}"
    for s in (other, cp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    _Redirect.seen = []
    try:
        cfg = SimpleNamespace(host_id=HOST, api_key=KEY, control_plane_url=f"http://127.0.0.1:{cp.server_port}")
        status, body = ControlPlaneHttp(cfg).request("GET", "/api/v1/host/terminal/wait?host_id=bender", timeout=3)
        assert status == 302 and body is None
        assert [p for p, _ in _Redirect.seen] == ["/api/v1/host/terminal/wait?host_id=bender"]   # the key went nowhere else
        assert _Redirect.seen[0][1]["X-Api-Key"] == KEY
        cp.target = None
        assert ControlPlaneHttp(cfg).request("GET", "/api/v1/host/terminal/wait", timeout=3) == (200, {"commands": []})
    finally:
        for s in (other, cp):
            s.shutdown()
            s.server_close()
