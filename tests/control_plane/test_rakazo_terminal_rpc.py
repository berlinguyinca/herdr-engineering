"""terminal/open, input, resize and close: a fake pty session for speed, plus one real ``cat`` smoke test."""
import base64
import json
import socket
import sys
import time
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane import rakazo_terminal as rt
from herdr_engineering.control_plane.repo import ControlPlaneRepo

BridgeError = rb.BridgeError
pytestmark = pytest.mark.filterwarnings('ignore:This process .* is multi-threaded:DeprecationWarning')


class FakeSession:
    """Stands in for TerminalSession: records what the procedures do to it."""
    made = []

    def __init__(self, argv, rows, cols, **kw):
        rt._check_size(rows, cols)
        self.argv, self.rows, self.cols = list(argv), rows, cols
        self.written, self.resizes, self.closed, self.started = [], [], False, False
        self.chunks = []
        self.alive = True
        FakeSession.made.append(self)

    def start(self):
        self.started = True

    def write(self, data):
        self.written.append(data)

    def resize(self, rows, cols):
        rt._check_size(rows, cols)
        self.rows, self.cols = rows, cols
        self.resizes.append((rows, cols))

    def snapshot_seq(self):
        return self.chunks[-1]["seq"] if self.chunks else 0

    def chunks_after(self, seq):
        return [dict(c) for c in self.chunks if c["seq"] > seq]

    def say(self, text):
        self.chunks.append({"seq": len(self.chunks) + 1, "data": base64.b64encode(text.encode()).decode()})

    def end(self, code=0):
        self.chunks.append({"seq": len(self.chunks) + 1, "ended": True, "exit": code})
        self.alive = False

    def close(self):
        self.closed = True
        self.alive = False


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def b64(text):
    return base64.b64encode(text if isinstance(text, bytes) else text.encode()).decode()


@pytest.fixture
def repo():
    return ControlPlaneRepo()


@pytest.fixture
def audit():
    return []


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bridge(repo, audit, clock):
    FakeSession.made = []
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": [
        {"email": "a@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "operator"},
        {"email": "k@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "family"}]})
    b = rb.RakazoBridge(cfg, assistant=None, version="x")
    b.svc = SimpleNamespace(tenant_for=lambda principal: "t", store=SimpleNamespace(audit_record=lambda **kw: audit.append(kw)))
    b.terminal = rt.Terminal(b, BridgeError, spawn=FakeSession, local_host="Beast", clock=clock)
    b.terminal.repo = repo
    b._procs.update(b.terminal.procs())
    b.stream_options = {"poll_seconds": 0.01, "heartbeat_seconds": 0.05, "max_seconds": 0.5}
    yield b
    b.terminal.close_all()


def ctx_for(bridge, role="operator"):
    account = next(a for a in bridge.accounts if a.role == role)
    return SimpleNamespace(account=account, token=bridge.tokens.mint(account.account_id, "127.0.0.1"), accepts_stream=True)


@pytest.fixture
def ctx(bridge):
    return ctx_for(bridge)


def live(repo, sid="a1", host="beast", **kw):
    repo.upsert_session(sid, status="working", host_id=host, herdr_session="default", pane_id="w1:p1",
                        terminal_id="term_1", **kw)
    return sid


def call(bridge, ctx, proc, **inp):
    return bridge.terminal.procs()[f"terminal/{proc}"](ctx, inp)


def status_of(bridge, ctx, proc, **inp):
    with pytest.raises(BridgeError) as e:
        call(bridge, ctx, proc, **inp)
    return e.value.status


def test_procs_are_named_terminal_slash(bridge):
    assert set(bridge.terminal.procs()) == {"terminal/open", "terminal/input", "terminal/resize",
                                            "terminal/close", "terminal/subscribe"}
    assert set(bridge.terminal.procs()) <= set(bridge._procs)


def test_open_starts_a_local_attach_and_returns_the_cursor(bridge, ctx, repo):
    live(repo)
    out = call(bridge, ctx, "open", id="a1", rows=30, cols=100)
    assert out["seq"] == 0 and (out["rows"], out["cols"]) == (30, 100) and out["terminalId"]
    s = FakeSession.made[0]
    assert s.started and s.argv == ["herdr", "--session", "default", "agent", "attach", "w1:p1"]   # Beast == beast
    assert (s.rows, s.cols) == (30, 100)


def test_open_a_remote_host_goes_over_ssh_and_never_errors(bridge, ctx, repo):
    live(repo, host="elsewhere")
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert FakeSession.made[0].argv[:2] == ["ssh", "-tt"]


def test_open_errors(bridge, ctx, repo):
    assert status_of(bridge, ctx, "open", id="nope", rows=24, cols=80) == 404
    repo.upsert_session("dead", status="closed", host_id="beast", herdr_session="default", pane_id="w1:p1",
                        terminal_id="term_1")
    assert status_of(bridge, ctx, "open", id="dead", rows=24, cols=80) == 409
    live(repo)
    assert status_of(bridge, ctx, "open", id="a1", rows=0, cols=80) == 400
    assert status_of(bridge, ctx, "open", id="a1", rows="24", cols=80) == 400
    assert status_of(bridge, ctx, "open", id=7, rows=24, cols=80) == 404
    assert not FakeSession.made


def test_open_before_the_repo_is_wired_is_503(bridge, ctx):
    bridge.terminal.repo = None
    assert status_of(bridge, ctx, "open", id="a1", rows=24, cols=80) == 503


def test_the_viewer_role_is_refused_everywhere(bridge, repo, ctx):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    kid = ctx_for(bridge, "family")
    for proc, inp in (("open", {"id": "a1", "rows": 24, "cols": 80}), ("input", {"terminalId": tid, "data": "", "n": 1}),
                      ("resize", {"terminalId": tid, "rows": 24, "cols": 80}), ("close", {"terminalId": tid}),
                      ("subscribe", {"terminalId": tid, "cursor": 0})):
        assert status_of(bridge, kid, proc, **inp) == 403
    assert len(FakeSession.made) == 1 and not FakeSession.made[0].closed


def test_input_is_typed_once_per_n(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    s = FakeSession.made[0]
    for n, text in ((1, "ls"), (1, "ls"), (2, "\r"), (2, "\r"), (4, "skipped"), (1, "old"), (3, "x")):
        assert call(bridge, ctx, "input", terminalId=tid, data=b64(text), n=n) == {"ok": True}
    assert s.written == [b"ls", b"\r", b"x"]


def test_input_limits_and_validation(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    s = FakeSession.made[0]
    call(bridge, ctx, "input", terminalId=tid, data=b64(b"a" * 65536), n=1)
    assert len(s.written[0]) == 65536
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64(b"a" * 65537), n=2) == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data="!!!", n=2) == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data=7, n=2) == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64("x"), n="2") == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64("x"), n=True) == 400
    assert status_of(bridge, ctx, "input", terminalId="nope", data=b64("x"), n=1) == 404
    call(bridge, ctx, "input", terminalId=tid, data=b64("ok"), n=2)       # a refused call did not burn n
    assert s.written[-1] == b"ok"


def test_resize(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    assert call(bridge, ctx, "resize", terminalId=tid, rows=40, cols=120) == {"ok": True}
    assert FakeSession.made[0].resizes == [(40, 120)]
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=0, cols=120) == 400
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows="40", cols=120) == 400
    assert status_of(bridge, ctx, "resize", terminalId="nope", rows=40, cols=120) == 404


def test_close_ends_the_session_and_is_idempotent(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    assert call(bridge, ctx, "close", terminalId=tid) == {"ok": True}
    assert FakeSession.made[0].closed
    assert call(bridge, ctx, "close", terminalId=tid) == {"ok": True}
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64("x"), n=1) == 404


def test_the_fifth_session_is_429_and_a_close_frees_a_slot(bridge, ctx, repo):
    ids = [call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)["terminalId"] for i in range(4)]
    assert status_of(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80) == 429
    call(bridge, ctx, "open", id="a0", rows=24, cols=80)                    # taking over is not a fifth session
    call(bridge, ctx, "close", terminalId=ids[1])
    call(bridge, ctx, "open", id="a4", rows=24, cols=80)


def test_a_second_open_reuses_the_pty_and_retires_the_old_id(bridge, ctx, repo):
    live(repo)
    first = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "input", terminalId=first, data=b64("a"), n=1)
    second = call(bridge, ctx, "open", id="a1", rows=30, cols=100)
    assert first != second["terminalId"] and len(FakeSession.made) == 1 and not FakeSession.made[0].closed
    assert FakeSession.made[0].resizes == [(30, 100)]
    assert status_of(bridge, ctx, "input", terminalId=first, data=b64("x"), n=2) == 404
    assert status_of(bridge, ctx, "resize", terminalId=first, rows=24, cols=80) == 404
    assert call(bridge, ctx, "close", terminalId=first) == {"ok": True}      # a retired id cannot end the session
    assert not FakeSession.made[0].closed
    call(bridge, ctx, "input", terminalId=second["terminalId"], data=b64("b"), n=1)   # n restarts at 1
    assert FakeSession.made[0].written == [b"a", b"b"]


def test_reopen_replays_the_buffer_after_the_stream_dropped(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    s = FakeSession.made[0]
    s.say("screen")
    again = call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert [c["seq"] for c in s.chunks_after(again["seq"])] == [1] and len(FakeSession.made) == 1
    assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor=0) == 404


def test_reuse_does_not_count_against_the_cap(bridge, ctx, repo):
    for i in range(4):
        call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)
    call(bridge, ctx, "open", id="a2", rows=24, cols=80)
    assert len(FakeSession.made) == 4


def test_an_ended_pty_is_respawned_on_the_next_open(bridge, ctx, repo, audit):
    live(repo)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    FakeSession.made[0].end(255)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert len(FakeSession.made) == 2 and FakeSession.made[0].closed and not FakeSession.made[1].closed
    assert [e["action"].rsplit(".", 1)[1] for e in audit] == ["attach", "detach", "attach"]


def test_another_operator_cannot_use_the_terminal_id(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    other = ctx_for(bridge)
    other.account = SimpleNamespace(**{**vars(ctx.account), "account_id": "someone-else"})
    assert status_of(bridge, other, "input", terminalId=tid, data=b64("x"), n=1) == 404
    assert status_of(bridge, other, "subscribe", terminalId=tid, cursor=0) == 404
    assert call(bridge, other, "close", terminalId=tid) == {"ok": True} and not FakeSession.made[0].closed


def test_open_errors_carry_the_reason_as_the_code(bridge, ctx, repo):
    repo.upsert_session("dead", status="closed", host_id="beast", herdr_session="default", pane_id="w1:p1",
                        terminal_id="term_1")
    with pytest.raises(BridgeError) as e:
        call(bridge, ctx, "open", id="dead", rows=24, cols=80)
    assert (e.value.status, e.value.code) == (409, "NOT_RUNNING")
    repo.upsert_session("bad", status="working", host_id="beast", herdr_session="default", pane_id="w1 p1",
                        terminal_id="term_1")
    with pytest.raises(BridgeError) as e:
        call(bridge, ctx, "open", id="bad", rows=24, cols=80)
    assert (e.value.status, e.value.code) == (409, "UNSAFE_IDENTITY")


def test_audit_records_attach_and_detach_without_any_data(bridge, ctx, repo, audit):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "input", terminalId=tid, data=b64("secret-input"), n=1)
    FakeSession.made[0].say("secret-output")
    call(bridge, ctx, "close", terminalId=tid)
    call(bridge, ctx, "close", terminalId=tid)
    assert [e["action"] for e in audit] == ["rakazo.bridge.terminal.attach", "rakazo.bridge.terminal.detach"]
    assert all(e["subject_id"] == "a1" and "beast" in e["detail"] for e in audit)
    text = json.dumps(audit, default=str)
    assert "secret" not in text and b64("secret-input") not in text and tid not in text


def test_a_client_takeover_is_not_a_detach(bridge, ctx, repo, audit):
    live(repo)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert [e["action"].rsplit(".", 1)[1] for e in audit] == ["attach", "attach"]


def test_an_idle_session_is_dropped_five_minutes_after_the_last_stream(bridge, ctx, repo, clock, audit):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    clock.now += 299
    call(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80)           # still there
    clock.now += 2
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80) == 404
    assert FakeSession.made[0].closed and audit[-1]["action"].endswith("detach")


def test_a_live_stream_keeps_the_session_and_its_close_starts_the_clock(bridge, ctx, repo, clock):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    clock.now += 1000
    call(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80)           # a stream is open: not idle
    resp.frames.close()
    resp.on_close()
    clock.now += 299
    call(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80)
    clock.now += 2
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80) == 404


def test_the_reaper_closes_an_idle_session_with_no_further_calls(bridge, repo, audit):
    live(repo)
    t = rt.Terminal(bridge, BridgeError, spawn=FakeSession, local_host="beast", ttl=0.2)
    t.repo = repo
    ctx = ctx_for(bridge)
    t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})
    deadline = time.monotonic() + 3
    while not FakeSession.made[0].closed and time.monotonic() < deadline:
        time.sleep(0.02)
    t.close_all()
    assert FakeSession.made[0].closed and [e["action"].rsplit(".", 1)[1] for e in audit] == ["attach", "detach"]


def test_an_ended_session_without_a_stream_does_not_hold_a_slot(bridge, ctx, repo, clock):
    for i in range(4):
        call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)
    FakeSession.made[0].end(255)
    resp = call(bridge, ctx, "subscribe", terminalId=next(iter(bridge.terminal._clients)), cursor=0)
    list(resp.frames)                                            # the ended frame is delivered, the stream closes
    resp.frames.close()
    resp.on_close()
    call(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80)
    assert FakeSession.made[0].closed and len(FakeSession.made) == 5


def test_an_ended_session_keeps_its_slot_until_the_ended_frame_was_seen(bridge, ctx, repo):
    for i in range(4):
        call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)
    FakeSession.made[0].end(255)
    assert status_of(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80) == 429


def test_agents_list_is_unaffected(bridge, repo, ctx):
    bridge.agents.repo = repo
    live(repo)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert [a["id"] for a in bridge.agents.procs()["agents/list"](ctx, {})["agents"]] == ["a1"]


def test_the_default_local_host_is_this_machine_lowercased(bridge):
    t = rt.Terminal(bridge, BridgeError)
    assert t.local_host == socket.gethostname().lower()


def test_real_pty_smoke_with_cat(bridge, ctx, repo, monkeypatch):
    live(repo)
    monkeypatch.setattr(rt, "attach_argv", lambda *a, **k: ["cat"])
    bridge.terminal.spawn = rt.TerminalSession
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "input", terminalId=tid, data=b64("hello\n"), n=1)
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    seen = b""
    try:
        for raw in resp.frames:
            ev = json.loads(raw[6:])["json"]
            seen += base64.b64decode(ev.get("data", ""))
            if b"hello" in seen:
                break
    finally:
        resp.frames.close()
        resp.on_close()
    call(bridge, ctx, "close", terminalId=tid)
    assert b"hello" in seen
