"""Host terminal relay: a fake control plane that speaks the real wait/output contract and a fake herdr client."""
import base64
import copy
import http.server
import json
import logging
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from herdr_engineering import terminal_relay as relay_mod
from herdr_engineering.herdr_pane_client import HerdrError
from herdr_engineering.terminal_relay import ControlPlaneHttp, RelayLocked, TerminalRelay
from tests.control_plane.test_rakazo_bridge import make_server  # noqa: F401  (fixture for the real-broker tests)

HOST = "bender"
TOKEN = "host-token-SECRET-9d41"   # host.json's token: the relay's only credential
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
        self.epoch = EPOCH
        self.dead = set()        # terminals the broker no longer knows or has ended: pushes answer 404
        self.gate = None         # an Event: pushes (for gate_tids, or all) block until it is set or their timeout passes
        self.gate_tids = None
        self.blocked = 0         # pushes that reached a closed gate

    def push(self, kind, **fields):
        with self.cond:
            self.counter += 1
            cmd = {"id": f"{self.epoch}-{self.counter}-r{self.counter:04d}", "kind": kind, **fields}
            self.queue.append(cmd)
            self.cond.notify_all()
            return cmd

    def retire(self, *tids, host_offline=False):
        """The broker ended these terminals (or lost them): their queued commands go (the close too when the host
        was offline), and a push for them answers 404."""
        with self.cond:
            self.dead.update(tids)
            self.queue = [c for c in self.queue if c.get("terminal") not in tids
                          or (c["kind"] == "close" and not host_offline)]

    def restart(self, epoch="ffeeddcc"):
        """A new control-plane process: a new epoch, an empty queue, and no terminal known."""
        with self.cond:
            self.epoch, self.queue, self.counter = epoch, [], 0
            self.dead.update(self.frames)

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
            gate = self.gate
            if gate is not None and (self.gate_tids is None or tid in self.gate_tids) and not gate.is_set():
                self.blocked += 1
                if not gate.wait(timeout):
                    raise OSError("timed out")     # a blocked POST: the relay's own timeout ends it
            with self.cond:
                self.posts.append((tid, q.get("host_id"), len(body["frames"])))
                status = 404 if tid in self.dead else self.post_status.get(tid, 200)
                if status == 200:
                    self.frames.setdefault(tid, []).extend(copy.deepcopy(body["frames"]))
                    if any(f.get("ended") for f in body["frames"]):
                        self.retire(tid)
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
                if epoch == self.epoch:
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
        self.agents = {}         # pane -> agent name (None: a plain shell pane)
        self.counter = 0

    def list_panes(self):
        if self.down:
            raise OSError("herdr socket")
        with self.lock:
            return [{"pane_id": p, "agent": self.agents.get(p, "claude")} for p in self.screens]

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

    def texts(self):
        with self.lock:
            return [v for m, _, v in self.sent if m == "text"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(relay_mod, "_FAIL_PAUSE", 0.01)


@pytest.fixture
def env(tmp_path):
    cfg = SimpleNamespace(host_id=HOST, control_plane_url="http://cp.invalid")
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
    assert all(host is None for _, host, _ in env.cp.posts)    # the host id comes from the token, never the query
    first_wait = env.cp.waits[0]
    assert first_wait == (None, "25", None)          # no `after` until something was handled
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


def test_hostile_output_and_input_start_no_process(env, monkeypatch):
    import os
    import subprocess

    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("the relay must never start a process")
    for obj, name in ((subprocess, "Popen"), (subprocess, "run"), (subprocess, "check_output"), (os, "system"),
                      (os, "popen"), (os, "execv"), (os, "execvp"), (os, "fork"), (os, "spawnv")):
        monkeypatch.setattr(obj, name, boom)
    hostile = "\x1b]52;c;ZWNobyBwd25lZA==\x07\x1b[2J\x1b[?1049h$(touch /tmp/pwned)\n" + "A" * 1_000_000
    env.client.screens["p1"] = hostile
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    env.cp.push("open", terminal=TID2, pane_id="$(touch /tmp/pwned2)", rows=1, cols=1)
    inp(env.cp, 1, "$(touch /tmp/pwned3)\x1b]0;x\x07`id`\r")
    assert wait_until(lambda: env.cp.typed_frames(TID) and env.cp.ended(TID2) and env.client.sent)
    frame = env.cp.typed_frames(TID)[0]
    assert len(frame["data"]) <= 160 * 1024
    assert max(c for _, _, c in env.cp.posts) <= 20
    assert calls == []
    assert not os.path.exists("/tmp/pwned") and not os.path.exists("/tmp/pwned3")


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
    for secret in ("SCREEN-SECRET", b64("SCREEN-SECRET-xyz"), typed, b64(typed), b64(b"\xff\xfe"), TOKEN, TID, TID2):
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


def test_control_plane_http_sends_the_host_token_and_never_follows_a_redirect():
    other, cp = (http.server.HTTPServer(("127.0.0.1", 0), _Redirect) for _ in range(2))
    other.target = None
    cp.target = f"http://127.0.0.1:{other.server_port}"
    for s in (other, cp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    _Redirect.seen = []
    try:
        cfg = SimpleNamespace(host_id=HOST, control_plane_url=f"http://127.0.0.1:{cp.server_port}")
        status, body = ControlPlaneHttp(cfg, token=TOKEN).request("GET", "/api/v1/host/terminal/wait", timeout=3)
        assert status == 302 and body is None
        assert [p for p, _ in _Redirect.seen] == ["/api/v1/host/terminal/wait"]   # the token went nowhere else
        headers = {k.lower(): v for k, v in _Redirect.seen[0][1].items()}
        assert headers["x-host-token"] == TOKEN
        assert "x-api-key" not in headers and "authorization" not in headers    # never an API key or a bearer
        cp.target = None
        assert ControlPlaneHttp(cfg, token=TOKEN).request(
            "GET", "/api/v1/host/terminal/wait", timeout=3) == (200, {"commands": []})
    finally:
        for s in (other, cp):
            s.shutdown()
            s.server_close()


def test_the_first_successful_poll_logs_one_connected_line(env, caplog):
    env.cp.fail_waits = 2
    r = env.make(sleep=lambda s: time.sleep(0.01))
    with caplog.at_level(logging.INFO, logger="herdr_engineering.terminal_relay"):
        env.start(r)
        assert wait_until(lambda: len(env.cp.waits) >= 6)
    lines = [x for x in caplog.records if x.getMessage() == "terminal relay connected"]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO    # the rollout's health check greps for it
    first_ok = next(i for i, x in enumerate(caplog.records) if x.getMessage() == "terminal relay connected")
    assert all("unreachable" in x.getMessage() for x in caplog.records[:first_ok])   # only after a poll got an answer


def test_a_mirror_blocked_in_a_10s_push_does_not_delay_the_poll_loop(env):
    env.cp.gate, env.cp.gate_tids = threading.Event(), {TID}
    r = env.make()
    env.start(r)
    try:
        open_cmd(env.cp)
        assert wait_until(lambda: r.active_terminals() == 1)
        time.sleep(0.3)                              # the mirror is now inside its blocked first push
        close = env.cp.push("close", terminal=TID)
        assert wait_until(lambda: any(w[2] == close["id"] for w in env.cp.waits), 2)   # the next wait went out at once
        assert r.active_terminals() == 0             # the slot is freed by registration, not by the join
        env.cp.push("open", terminal=TID2, pane_id="p2", rows=24, cols=80)
        assert wait_until(lambda: env.cp.typed_frames(TID2), 2)
    finally:
        env.cp.gate.set()


def test_a_refusal_whose_push_blocks_does_not_stop_the_next_command(env):
    env.cp.gate, env.cp.gate_tids = threading.Event(), {TID2}
    r = env.make()
    env.start(r)
    try:
        env.cp.push("open", terminal=TID2, pane_id="nope", rows=1, cols=1)      # refused: its push blocks
        assert wait_until(lambda: env.cp.blocked == 1)
        open_cmd(env.cp)
        assert wait_until(lambda: env.cp.typed_frames(TID), 2)
    finally:
        env.cp.gate.set()


def test_probing_at_the_cap_is_bounded_when_the_broker_is_slow(env):
    r = env.make()
    env.client.screens.update({f"q{i}": "x" for i in range(5)})
    env.start(r)
    tids = [f"term_{i:024d}" for i in range(4)]
    for i, t in enumerate(tids):
        open_cmd(env.cp, t, f"q{i}")
    assert wait_until(lambda: r.active_terminals() == 4 and all(env.cp.typed_frames(t) for t in tids))
    env.cp.gate = threading.Event()                  # the broker stops answering pushes
    try:
        began = time.monotonic()
        fifth = open_cmd(env.cp, TID2, "q4")
        assert wait_until(lambda: any(w[2] == fifth["id"] for w in env.cp.waits), 5)
        assert time.monotonic() - began < 4          # not 4 pushes x 10 s
    finally:
        env.cp.gate.set()


def test_the_relay_command_logs_at_info_so_the_health_check_can_see_the_connected_line(monkeypatch):
    import argparse

    from herdr_engineering import cli, host_config
    calls = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))

    def unavailable():
        raise host_config.HostConfigUnavailable("no host.json", permanent=True)
    monkeypatch.setattr(host_config, "host_config", unavailable)
    assert cli._cmd_host_terminal_relay(argparse.Namespace(once_for_test=True)) == 2
    assert calls and calls[0]["level"] == logging.INFO


class Front:
    """A FakeCP behind a real HTTP listener that speaks the broker's host auth: ``X-Host-Token`` must be this host's token
    (anything else, an API key included, is 403) and a ``host_id`` in the query must be the token's host."""

    def __init__(self, cp, keepalive=True):
        front = self
        self.cp, self.seen, self.connections = cp, [], 0
        self.drop_after_reply = 0     # close the socket after this many replies WITHOUT announcing it (an idle-timeout gateway)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if keepalive else "HTTP/1.0"

            def setup(self):
                super().setup()
                front.connections += 1

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                front.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                given = parse_qs(urlsplit(self.path).query).get("host_id")
                if self.headers.get_all("X-Host-Token") != [TOKEN] or (given and given != [HOST]):
                    return self._reply(403, {"error": "the terminal relay needs a host token"})
                try:
                    status, payload = cp.request(self.command, self.path, body)
                except OSError:
                    self.close_connection = True      # the answer is lost: the relay sees a dropped connection
                    return
                self._reply(status, payload)

            def _reply(self, status, payload):
                raw = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                if front.drop_after_reply:
                    front.drop_after_reply -= 1
                    self.close_connection = True

            do_GET = do_POST = _serve

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def front(env):
    fronts = []

    def make(**kw):
        f = Front(env.cp, **kw)
        fronts.append(f)
        return f
    yield make
    for f in fronts:
        f.close()


def test_the_relay_talks_to_the_broker_with_the_host_token_and_nothing_else(env, front):
    f = front()
    cfg = SimpleNamespace(host_id=HOST, control_plane_url=f.url)
    r = env.make(http=ControlPlaneHttp(cfg, token=TOKEN))
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.typed_frames(TID))
    assert f.seen and all(h.get("x-host-token") == TOKEN for _, _, h in f.seen)
    assert not any("x-api-key" in h or "authorization" in h for _, _, h in f.seen)
    assert not any("host_id" in path for _, path, _ in f.seen)     # the host id comes from the token


def _http_to(front_, **kw):
    return ControlPlaneHttp(SimpleNamespace(host_id=HOST, control_plane_url=front_.url), token=TOKEN, **kw)


def test_frame_pushes_reuse_one_keep_alive_connection(env, front):
    f = front()
    http_ = _http_to(f)
    for _ in range(3):
        assert http_.request("POST", f"/api/v1/host/terminal/{TID}/output", {"frames": []})[0] == 200
    assert http_.request("GET", "/api/v1/host/terminal/wait?timeout=0")[0] == 200
    assert f.connections == 1


def test_a_connection_the_gateway_closed_is_replaced_transparently(env, front):
    f = front()
    f.drop_after_reply = 1                          # the first reply is followed by a silent close
    http_ = _http_to(f)
    path = f"/api/v1/host/terminal/{TID}/output"
    assert http_.request("POST", path, {"frames": []})[0] == 200
    assert http_.request("POST", path, {"frames": []})[0] == 200    # the stale socket fails, a new one carries it
    assert f.connections == 2
    assert http_.request("POST", path, {"frames": []})[0] == 200
    assert f.connections == 2


def test_a_server_that_never_keeps_alive_still_works(env, front):
    f = front(keepalive=False)
    http_ = _http_to(f)
    for _ in range(3):
        assert http_.request("GET", "/api/v1/host/terminal/wait?timeout=0")[0] == 200
    assert f.connections == 3


def test_an_unreachable_server_raises_oserror(env):
    http_ = ControlPlaneHttp(SimpleNamespace(host_id=HOST, control_plane_url="http://127.0.0.1:1"), token=TOKEN)
    with pytest.raises(OSError):
        http_.request("GET", "/api/v1/host/terminal/wait?timeout=0", timeout=2)


@pytest.mark.parametrize("token", ["not-this-hosts-token", ""])
def test_a_refused_host_token_backs_off_30s(env, front, token):
    f = front()
    sleeps = []
    cfg = SimpleNamespace(host_id=HOST, control_plane_url=f.url)
    r = env.make(http=ControlPlaneHttp(cfg, token=token), sleep=lambda s: sleeps.append(s))
    env.start(r)
    assert wait_until(lambda: len(sleeps) >= 3)
    assert all(30 <= s <= 36 for s in sleeps[:3])
    assert env.cp.waits == []                       # the broker never saw a wait


# ---------------------------------------------------------------------------------------------- fix round 1
def test_a_plain_shell_pane_is_refused(env):
    env.client.screens["sh"] = "$ "
    env.client.agents = {"sh": None}
    r = env.make()
    env.start(r)
    env.cp.push("open", terminal=TID, pane_id="sh", rows=24, cols=80)
    assert wait_until(lambda: env.cp.ended(TID))
    assert env.cp.ended(TID)[0]["reason"] == "invalid_pane"
    assert r.active_terminals() == 0 and env.client.reads == 0


def test_a_new_control_plane_epoch_reaps_the_terminals_of_the_old_one(env):
    r = env.make()
    env.start(r)
    tids = [f"term_{i:024d}" for i in range(4)]
    env.client.screens.update({f"q{i}": "x" for i in range(5)})
    for i, t in enumerate(tids):
        open_cmd(env.cp, t, f"q{i}")
    assert wait_until(lambda: r.active_terminals() == 4 and all(env.cp.typed_frames(t) for t in tids))
    env.cp.restart()                       # the control plane restarted; the panes are idle
    open_cmd(env.cp, TID2, "q4")           # the app opens again: a 5th open must succeed
    assert wait_until(lambda: env.cp.typed_frames(TID2))
    assert env.cp.ended(TID2) == []
    assert wait_until(lambda: r.active_terminals() == 1)
    assert len([t for t in threading.enumerate() if t.name.startswith("terminal-mirror")]) == 1


def test_the_epoch_is_persisted_so_a_restarted_relay_reaps_old_terminals(env):
    r1 = env.make()
    t1 = env.start(r1)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.typed_frames(TID))
    r1.stop()
    t1.join(5)
    env.cp.restart()                       # the control plane restarted while the relay was down
    env.cp.dead.clear()                    # (and the old terminals answer no 404: only the epoch tells)
    r2 = env.make()
    env.start(r2)
    open_cmd(env.cp, TID2, "p2")
    assert wait_until(lambda: env.cp.typed_frames(TID2))
    assert wait_until(lambda: r2.active_terminals() == 1)


def test_a_silent_pane_is_probed_and_a_404_retires_the_terminal(env, monkeypatch):
    monkeypatch.setattr(relay_mod, "_PROBE_EVERY", 0.3, raising=False)
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: len(env.cp.typed_frames(TID)) >= 3, 3)     # the unchanged screen is re-sent as a probe
    env.cp.retire(TID)
    assert wait_until(lambda: r.active_terminals() == 0)
    assert wait_until(lambda: not any(t.name.startswith("terminal-mirror") for t in threading.enumerate()))


def test_at_the_cap_an_open_first_probes_and_reaps_dead_terminals(env):
    r = env.make()
    env.start(r)
    tids = [f"term_{i:024d}" for i in range(4)]
    env.client.screens.update({f"q{i}": "x" for i in range(5)})
    for i, t in enumerate(tids):
        open_cmd(env.cp, t, f"q{i}")
    assert wait_until(lambda: r.active_terminals() == 4 and all(env.cp.typed_frames(t) for t in tids))
    env.cp.retire(*tids, host_offline=True)    # ended on the broker, silently for an idle relay
    open_cmd(env.cp, TID2, "q4")
    assert wait_until(lambda: env.cp.typed_frames(TID2))
    assert env.cp.ended(TID2) == [] and r.active_terminals() == 1


def test_a_takeover_close_then_open_at_the_cap_succeeds(env):
    r = env.make()
    env.start(r)
    tids = [f"term_{i:024d}" for i in range(4)]
    env.client.screens.update({f"q{i}": "x" for i in range(5)})
    for i, t in enumerate(tids):
        open_cmd(env.cp, t, f"q{i}")
    assert wait_until(lambda: r.active_terminals() == 4)
    env.cp.push("close", terminal=tids[0])
    open_cmd(env.cp, TID2, "q0")
    assert wait_until(lambda: env.cp.typed_frames(TID2))
    assert env.cp.ended(TID2) == [] and r.active_terminals() == 4


def test_a_known_close_is_redispatched_after_a_restart(env):
    import json as _json
    close_id, open_id = f"{EPOCH}-2-r0002", f"{EPOCH}-1-r0001"
    (env.tmp / "terminal-relay-state.json").write_text(_json.dumps(
        {"ids": [open_id, close_id], "epoch": EPOCH,
         "terminals": {TID: {"pane": "p1", "n": 0, "epoch": EPOCH}}}))
    env.cp.queue = [{"id": open_id, "kind": "open", "terminal": TID, "pane_id": "p1", "rows": 1, "cols": 1},
                    {"id": close_id, "kind": "close", "terminal": TID}]
    env.cp.counter = 2
    r = env.make()
    env.start(r)
    assert wait_until(lambda: any(w[2] == close_id for w in env.cp.waits))
    assert wait_until(lambda: r.active_terminals() == 0)


def test_a_crash_after_the_id_was_saved_never_retypes(env):
    class Crash(BaseException):
        pass

    r1 = env.make()

    def hook(method, pane, value):
        if value == "b":
            raise Crash                       # dies inside apply, after the id and n were saved
    env.client.hook = hook
    open_cmd(env.cp)
    inp(env.cp, 1, "a"), inp(env.cp, 2, "b"), inp(env.cp, 3, "c")
    t1 = threading.Thread(target=lambda: _swallow(r1.run_forever), daemon=True)
    t1.start()
    t1.join(5)
    assert env.client.texts() == ["a", "b"]
    env.client.hook = None
    r2 = env.make()
    env.start(r2)
    assert wait_until(lambda: env.client.texts() == ["a", "b", "c"])
    time.sleep(0.2)
    assert env.client.texts() == ["a", "b", "c"]


def _swallow(fn):
    try:
        fn()
    except BaseException:  # noqa: BLE001
        pass


def test_a_dying_mirror_ends_its_terminal_with_relay_error(env):
    def boom():
        raise RuntimeError("bug")
    env.client.screens["p1"] = boom
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: env.cp.ended(TID))
    assert env.cp.ended(TID)[0]["reason"] == "relay_error"
    assert wait_until(lambda: r.active_terminals() == 0)


def test_the_flusher_survives_an_error(env):
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: r.active_terminals() == 1)
    term = r._terms[TID]
    real = term.translator
    calls = []

    class Bad:
        def feed(self, data):
            return []

        def flush(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("bug")
            return [("keys", ["esc"])]
    term.translator = Bad()
    inp(env.cp, 1, "x")
    assert wait_until(lambda: calls)
    inp(env.cp, 2, "y")
    assert wait_until(lambda: ("keys", "p1", ["esc"]) in env.client.sent)
    term.translator = real


def test_sigterm_stops_a_blocked_long_poll_promptly(tmp_path):
    import os
    import signal

    class Blocking:
        def request(self, method, path, body=None, timeout=10.0):
            time.sleep(8)
            return 200, {"commands": []}
    cfg = SimpleNamespace(host_id=HOST, control_plane_url="http://cp.invalid")
    r = TerminalRelay(cfg, FakeClient(), Blocking(), state_dir=tmp_path)
    restore = relay_mod.install_signal_handlers(r)
    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        began = time.monotonic()
        r.run_forever()
        assert time.monotonic() - began < 2
    finally:
        timer.cancel()
        restore()


def test_the_state_file_is_fsynced_before_the_replace(env, monkeypatch):
    import os
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    r = env.make()
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: "replace" in order)
    assert order[:2] == ["fsync", "replace"]


def test_one_state_save_per_input_command_and_it_precedes_the_keystroke(env, monkeypatch):
    import os
    events = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("save"), real_fsync(fd))[1])
    r = env.make()
    env.client.hook = lambda method, pane, value: events.append("type")
    env.start(r)
    open_cmd(env.cp)
    assert wait_until(lambda: r.active_terminals() == 1 and env.cp.typed_frames(TID))
    events.clear()
    inp(env.cp, 1, "a"), inp(env.cp, 2, "b")
    assert wait_until(lambda: env.client.texts() == ["a", "b"])
    assert events == ["save", "type", "save", "type"]        # at-most-once typing: persisted first, one fsync each


def test_retarget_follows_the_url_but_the_host_token_never_changes():
    cfg = SimpleNamespace(host_id=HOST, control_plane_url="http://a.invalid")
    h = ControlPlaneHttp(cfg, token=TOKEN)
    h.retarget("http://b.invalid/", "an-ingest-key-the-relay-must-not-adopt")
    assert h.url == "http://b.invalid" and h.token == TOKEN


# ------------------------------------------------------------------------- against the real broker, over HTTP
class Flaky:
    """The relay's real ControlPlaneHttp, with a switch that makes the network unreachable."""

    def __init__(self, inner):
        self.inner = inner
        self.offline = False

    def request(self, *a, **kw):
        if self.offline:
            raise OSError("offline")
        return self.inner.request(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def real(env, tmp_path, make_server, monkeypatch):
    """A live ControlPlaneServer in open mode (like production) whose fleet file holds this host's token; the relay
    talks to it through ControlPlaneHttp, so the real host-token check, routes and JSON are all in the path."""
    import hashlib

    import yaml

    from herdr_engineering.fleet_config import FleetConfigStore
    from tests.control_plane.test_rakazo_bridge import bridge_section
    servers, make = [], make_server
    monkeypatch.setattr(relay_mod, "_WAIT", 0.1)    # a wait the relay left behind must not hold the broker for 25 s
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({"version": 1, "sections": {}, "hosts": {
        HOST: {"roles": [], "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest()}}}))
    fleet.chmod(0o600)
    clock = Clock()

    def new_server():
        svr, _ = make(bridge_section(), fleet_config=FleetConfigStore(fleet))
        svr.rakazo.terminal._clock = clock
        servers.append(svr)
        return svr
    svr = new_server()
    for i in range(6):
        env.client.screens[f"w1:p{i}"] = f"pane {i}"
        svr.repo.upsert_session(f"s{i}", status="working", host_id=HOST, herdr_session="default", pane_id=f"w1:p{i}",
                                terminal_id=f"term_{i}")

    def owner(server):
        bridge = server.rakazo
        account = bridge.accounts[0]
        return SimpleNamespace(account=account, token=bridge.tokens.mint(account.account_id, "127.0.0.1"),
                               accepts_stream=True)
    http = Flaky(ControlPlaneHttp(SimpleNamespace(host_id=HOST, control_plane_url=f"http://127.0.0.1:{svr.port}"), TOKEN))
    r = env.make(http=http, sleep=lambda s: time.sleep(0.01))
    env.start(r)
    broker = svr.rakazo.terminal
    assert wait_until(lambda: broker.host_connected(HOST))
    ns = SimpleNamespace(env=env, svr=svr, broker=broker, http=http, ctx=owner(svr), clock=clock, relay=r,
                         new_server=new_server, owner=owner, procs=lambda: broker.procs())
    yield ns
    for server in servers:
        server.rakazo.terminal.close_all()


def frames_of(broker, sid):
    return broker._lives[sid].chunks_after(0)


def test_real_broker_open_input_close_round_trip(real):
    out = real.broker.procs()["terminal/open"](real.ctx, {"id": "s0", "rows": 24, "cols": 80})
    tid = out["terminalId"]
    assert wait_until(lambda: frames_of(real.broker, "s0"))
    assert base64.b64decode(frames_of(real.broker, "s0")[0]["data"]).decode().endswith("pane 0")
    for n, text in ((1, "ls"), (2, "\r")):
        real.broker.procs()["terminal/input"](real.ctx, {"terminalId": tid, "n": n, "data": b64(text)})
    assert wait_until(lambda: real.env.client.texts() == ["ls"] and ("keys", "w1:p0", ["enter"]) in real.env.client.sent)
    real.broker.procs()["terminal/close"](real.ctx, {"terminalId": tid})
    assert wait_until(lambda: real.relay.active_terminals() == 0)


def test_real_broker_takeover_and_cap(real):
    proc = real.broker.procs()["terminal/open"]
    tids = [proc(real.ctx, {"id": f"s{i}", "rows": 24, "cols": 80})["terminalId"] for i in range(4)]
    assert wait_until(lambda: real.relay.active_terminals() == 4)
    again = proc(real.ctx, {"id": "s0", "rows": 24, "cols": 80})["terminalId"]    # takeover: close, then open
    assert again != tids[0]
    assert wait_until(lambda: again in real.relay._terms)        # the old live's frames do not prove the takeover
    assert real.relay.active_terminals() == 4
    # the takeover's close really reached the relay: the old terminal is gone from it, the new one took its slot
    assert set(real.relay._terms) == {*tids[1:], again}
    assert tids[0] not in real.relay._terms


def test_real_broker_restart_with_idle_panes_lets_a_fifth_open_succeed(real):
    proc = real.broker.procs()["terminal/open"]
    for i in range(4):
        proc(real.ctx, {"id": f"s{i}", "rows": 24, "cols": 80})
    assert wait_until(lambda: real.relay.active_terminals() == 4)
    second = real.new_server()                     # the control plane restarted: no terminal known any more
    fresh = second.rakazo.terminal
    second.repo.upsert_session("s4", status="working", host_id=HOST, herdr_session="default", pane_id="w1:p4",
                               terminal_id="term_4")
    real.http.retarget(f"http://127.0.0.1:{second.port}")
    assert wait_until(lambda: fresh.host_connected(HOST))
    out = fresh.procs()["terminal/open"](real.owner(second), {"id": "s4", "rows": 24, "cols": 80})
    assert wait_until(lambda: frames_of(fresh, "s4"))
    assert wait_until(lambda: real.relay.active_terminals() == 1)
    assert set(real.relay._terms) == {out["terminalId"]}      # only the new control plane's terminal is served
    assert fresh._lives["s4"].alive


def test_real_broker_host_offline_then_the_host_returns(real, monkeypatch):
    monkeypatch.setattr(relay_mod, "_PROBE_EVERY", 0.3, raising=False)
    monkeypatch.setattr(relay_mod, "_PUSH_RETRY", 0.05)
    tid = real.broker.procs()["terminal/open"](real.ctx, {"id": "s0", "rows": 24, "cols": 80})["terminalId"]
    assert wait_until(lambda: frames_of(real.broker, "s0"))
    assert tid in real.relay._terms
    real.http.offline = True
    time.sleep(0.3)                                # no wait is blocked any more
    real.clock.now += 11
    real.broker._reap()
    assert real.broker._lives["s0"].alive is False
    assert real.broker._lives["s0"].chunks_after(0)[-1]["reason"] == "host_offline"
    real.http.offline = False
    assert wait_until(lambda: real.relay.active_terminals() == 0, 5)   # the probe got a 404
    assert tid not in real.relay._terms
