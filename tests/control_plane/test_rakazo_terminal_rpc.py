"""terminal/open, input, resize and close through the broker, against a fake host that fetches commands and pushes frames."""
import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane import rakazo_terminal as rt
from herdr_engineering.control_plane.repo import ControlPlaneRepo

BridgeError = rb.BridgeError
pytestmark = pytest.mark.filterwarnings('ignore:This process .* is multi-threaded:DeprecationWarning')


class FakeHost:
    """Plays the relay for the broker: fetches what was queued for its hosts (acking as it goes, deduping by command id)
    and pushes frames back. It learns which host a terminal lives on from the open commands it receives."""

    def __init__(self, terminal, hosts=("beast", "elsewhere")):
        self.terminal, self.hosts, self.log = terminal, hosts, []
        self.acked, self.where = {}, {}

    def poll(self):
        for host in self.hosts:
            for c in self.terminal.wait_commands(host, 0, after=self.acked.get(host)):
                self.acked[host] = c["id"]
                if c["kind"] == "open":
                    self.where[c["terminal"]] = host
                self.log.append(dict(c, host=host))

    def sent(self, kind=None):
        self.poll()
        return [c for c in self.log if kind is None or c["kind"] == kind]

    def say(self, tid, text, rows=24, cols=80):
        self.poll()
        self.terminal.accept_frames(self.where[tid], tid, [{"data": b64(text), "rows": rows, "cols": cols}])

    def end(self, tid, code=0, reason=None):
        self.poll()
        self.terminal.accept_frames(self.where[tid], tid, [{"ended": True, "exit": code, **({"reason": reason} if reason else {})}])


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
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": [
        {"email": "a@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "operator"},
        {"email": "k@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "family"}]})
    b = rb.RakazoBridge(cfg, assistant=None, version="x")
    b.svc = SimpleNamespace(tenant_for=lambda principal: "t", store=SimpleNamespace(audit_record=lambda **kw: audit.append(kw)))
    b.terminal = rt.Terminal(b, BridgeError, clock=clock)
    b.terminal.repo = repo
    b._procs.update(b.terminal.procs())
    b.stream_options = {"poll_seconds": 0.01, "heartbeat_seconds": 0.05, "max_seconds": 0.5}
    b.fake_host = FakeHost(b.terminal)
    b.fake_host.poll()                       # both hosts are connected from the start
    yield b
    b.terminal.close_all()


@pytest.fixture
def host(bridge):
    return bridge.fake_host


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
    bridge.fake_host.poll()                  # the relays are still polling
    return bridge.terminal.procs()[f"terminal/{proc}"](ctx, inp)


def status_of(bridge, ctx, proc, **inp):
    with pytest.raises(BridgeError) as e:
        call(bridge, ctx, proc, **inp)
    return e.value.status


def typed(host):
    return [base64.b64decode(c["data"]) for c in host.sent("input")]


def test_procs_are_named_terminal_slash(bridge):
    assert set(bridge.terminal.procs()) == {"terminal/open", "terminal/input", "terminal/resize",
                                            "terminal/close", "terminal/subscribe"}
    assert set(bridge.terminal.procs()) <= set(bridge._procs)


def test_open_queues_an_open_command_for_the_host_and_returns_the_cursor(bridge, ctx, repo, host):
    live(repo)
    out = call(bridge, ctx, "open", id="a1", rows=30, cols=100)
    assert out["seq"] == 0 and (out["rows"], out["cols"]) == (30, 100) and out["terminalId"]
    (cmd,) = host.sent("open")
    assert cmd["host"] == "beast" and cmd["id"] and cmd["terminal"] == out["terminalId"]
    assert (cmd["pane_id"], cmd["rows"], cmd["cols"]) == ("w1:p1", 30, 100)


def test_open_on_another_host_queues_for_that_host_only(bridge, ctx, repo, host):
    live(repo, host="elsewhere")
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert [c["host"] for c in host.sent("open")] == ["elsewhere"]


def test_open_for_a_host_that_is_not_connected_is_a_plain_503_at_once(bridge, ctx, repo, host):
    live(repo, host="gamma")                                    # nobody ever polled for gamma
    with pytest.raises(BridgeError) as e:
        call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert (e.value.status, e.value.code) == (503, "HOST_NOT_CONNECTED")
    assert not bridge.terminal._lives and not bridge.terminal._queues.get("gamma")


def test_a_host_is_connected_while_a_wait_blocks_and_for_10_seconds_after_it_ends(bridge, clock):
    terminal = bridge.terminal
    assert terminal.host_connected("beast") and not terminal.host_connected("gamma")
    clock.now += 9
    assert terminal.host_connected("beast")
    clock.now += 1.1
    assert not terminal.host_connected("beast")                                # last wait ended more than 10 s ago
    blocked = threading.Thread(target=lambda: terminal.wait_commands("gamma", 1.5), daemon=True)
    blocked.start()
    time.sleep(0.2)
    clock.now += 500
    assert terminal.host_connected("gamma")                                    # a blocked waiter is a connected host
    blocked.join(5)
    assert terminal.host_connected("gamma")                                    # its end time is recorded when it returns
    clock.now += 10.1
    assert not terminal.host_connected("gamma")


def test_state_for_hosts_not_seen_for_ten_minutes_is_pruned(bridge, clock):
    terminal = bridge.terminal
    terminal.wait_commands("gone", 0)
    clock.now += 599
    terminal._reap()
    assert "gone" in terminal._polled
    clock.now += 2
    terminal._reap()
    assert "gone" not in terminal._polled and "gone" not in terminal._queues


def test_open_errors(bridge, ctx, repo, host):
    assert status_of(bridge, ctx, "open", id="nope", rows=24, cols=80) == 404
    repo.upsert_session("dead", status="closed", host_id="beast", herdr_session="default", pane_id="w1:p1",
                        terminal_id="term_1")
    assert status_of(bridge, ctx, "open", id="dead", rows=24, cols=80) == 409
    live(repo)
    assert status_of(bridge, ctx, "open", id="a1", rows=0, cols=80) == 400
    assert status_of(bridge, ctx, "open", id="a1", rows="24", cols=80) == 400
    assert status_of(bridge, ctx, "open", id=7, rows=24, cols=80) == 404
    assert not host.sent("open")


def test_open_before_the_repo_is_wired_is_503(bridge, ctx):
    bridge.terminal.repo = None
    assert status_of(bridge, ctx, "open", id="a1", rows=24, cols=80) == 503


def test_the_viewer_role_is_refused_everywhere(bridge, repo, ctx, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    kid = ctx_for(bridge, "family")
    for proc, inp in (("open", {"id": "a1", "rows": 24, "cols": 80}), ("input", {"terminalId": tid, "data": "", "n": 1}),
                      ("resize", {"terminalId": tid, "rows": 24, "cols": 80}), ("close", {"terminalId": tid}),
                      ("subscribe", {"terminalId": tid, "cursor": 0})):
        assert status_of(bridge, kid, proc, **inp) == 403
    assert [c["kind"] for c in host.sent()] == ["open"]


def test_input_is_queued_once_per_n_in_order(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    for n, text in ((1, "ls"), (1, "ls"), (2, "\r"), (2, "\r"), (4, "skipped"), (1, "old"), (3, "x")):
        assert call(bridge, ctx, "input", terminalId=tid, data=b64(text), n=n) == {"ok": True}
    assert typed(host) == [b"ls", b"\r", b"x"]
    assert [(c["n"], c["terminal"]) for c in host.sent("input")] == [(1, tid), (2, tid), (3, tid)]


def test_input_limits_and_validation(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "input", terminalId=tid, data=b64(b"a" * 65536), n=1)
    assert len(typed(host)[0]) == 65536
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64(b"a" * 65537), n=2) == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data="!!!", n=2) == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data=7, n=2) == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64("x"), n="2") == 400
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64("x"), n=True) == 400
    assert status_of(bridge, ctx, "input", terminalId="nope", data=b64("x"), n=1) == 404
    call(bridge, ctx, "input", terminalId=tid, data=b64("ok"), n=2)       # a refused call did not burn n
    assert typed(host)[-1] == b"ok"


def test_resize(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    assert call(bridge, ctx, "resize", terminalId=tid, rows=40, cols=120) == {"ok": True}
    assert [(c["terminal"], c["rows"], c["cols"]) for c in host.sent("resize")] == [(tid, 40, 120)]
    assert not host.sent("zoom")
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=0, cols=120) == 400
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows="40", cols=120) == 400
    assert status_of(bridge, ctx, "resize", terminalId="nope", rows=40, cols=120) == 404


def test_resize_forwards_an_optional_zoom_flag(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80, zoom=True)
    call(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80, zoom=False)
    assert [(c["terminal"], c["zoom"]) for c in host.sent("zoom")] == [(tid, True), (tid, False)]
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80, zoom="yes") == 400
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80, zoom=1) == 400


def test_close_ends_the_terminal_and_is_idempotent(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    assert call(bridge, ctx, "close", terminalId=tid) == {"ok": True}
    assert [c["terminal"] for c in host.sent("close")] == [tid]
    assert call(bridge, ctx, "close", terminalId=tid) == {"ok": True}
    assert len(host.sent("close")) == 1
    assert status_of(bridge, ctx, "input", terminalId=tid, data=b64("x"), n=1) == 404


def test_the_fifth_terminal_is_429_and_a_close_frees_a_slot(bridge, ctx, repo):
    ids = [call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)["terminalId"] for i in range(4)]
    assert status_of(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80) == 429
    call(bridge, ctx, "open", id="a0", rows=24, cols=80)                    # taking over is not a fifth terminal
    call(bridge, ctx, "close", terminalId=ids[1])
    call(bridge, ctx, "open", id="a4", rows=24, cols=80)


def test_a_second_open_reuses_the_terminal_and_retires_the_old_id(bridge, ctx, repo, host):
    live(repo)
    first = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "input", terminalId=first, data=b64("a"), n=1)
    second = call(bridge, ctx, "open", id="a1", rows=30, cols=100)
    new = second["terminalId"]
    assert first != new and len(bridge.terminal._lives) == 1 and not bridge.terminal._lives["a1"].closed
    # the relay is told to drop the old id and open the new one, in that order
    assert [(c["kind"], c["terminal"]) for c in host.sent()] == [("open", first), ("input", first),
                                                                ("close", first), ("open", new)]
    assert status_of(bridge, ctx, "input", terminalId=first, data=b64("x"), n=2) == 404
    assert status_of(bridge, ctx, "resize", terminalId=first, rows=24, cols=80) == 404
    assert call(bridge, ctx, "close", terminalId=first) == {"ok": True}      # a retired id cannot end the terminal
    assert not bridge.terminal._lives["a1"].closed and len(host.sent("close")) == 1
    call(bridge, ctx, "input", terminalId=new, data=b64("b"), n=1)           # n restarts at 1
    assert typed(host) == [b"a", b"b"]


def test_reopen_replays_the_buffer_after_the_stream_dropped(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    host.say(tid, "screen")
    again = call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    chunks = bridge.terminal._lives["a1"].chunks_after(again["seq"])
    assert [(c["seq"], c["reset"]) for c in chunks] == [(1, True)] and len(bridge.terminal._lives) == 1
    assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor=0) == 404


def test_reuse_does_not_count_against_the_cap(bridge, ctx, repo):
    for i in range(4):
        call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)
    call(bridge, ctx, "open", id="a2", rows=24, cols=80)
    assert len(bridge.terminal._lives) == 4


def test_an_ended_pane_is_opened_afresh_on_the_next_open(bridge, ctx, repo, host, audit):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    old = bridge.terminal._lives["a1"]
    host.end(tid, 255)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert bridge.terminal._lives["a1"] is not old and old.closed and not bridge.terminal._lives["a1"].closed
    assert [e["action"].rsplit(".", 1)[1] for e in audit] == ["attach", "detach", "attach"]
    assert len(host.sent("close")) == 0                                       # the pane was already gone


def test_another_account_cannot_use_the_terminal_id(bridge, ctx, repo, host, monkeypatch):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    other = ctx_for(bridge)
    other.account = SimpleNamespace(**{**vars(ctx.account), "account_id": "someone-else"})
    monkeypatch.setattr(bridge, "is_owner", lambda account: True)     # past the gate: the id binding still holds
    assert status_of(bridge, other, "input", terminalId=tid, data=b64("x"), n=1) == 404
    assert status_of(bridge, other, "subscribe", terminalId=tid, cursor=0) == 404
    assert call(bridge, other, "close", terminalId=tid) == {"ok": True} and not host.sent("close")


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


def test_audit_records_attach_and_detach_without_any_data(bridge, ctx, repo, host, audit):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    call(bridge, ctx, "input", terminalId=tid, data=b64("secret-input"), n=1)
    host.say(tid, "secret-output")
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


def test_an_idle_terminal_is_dropped_five_minutes_after_the_last_stream(bridge, ctx, repo, clock, audit, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    clock.now += 299
    call(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80)           # still there
    clock.now += 2
    assert status_of(bridge, ctx, "resize", terminalId=tid, rows=24, cols=80) == 404
    assert [c["terminal"] for c in host.sent("close")] == [tid] and audit[-1]["action"].endswith("detach")


def test_a_live_stream_keeps_the_terminal_and_its_close_starts_the_clock(bridge, ctx, repo, clock):
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


def short_lived(bridge, repo, **kw):
    live(repo)
    t = rt.Terminal(bridge, BridgeError, ttl=0.2, **kw)
    t.repo = repo
    t.wait_commands("beast", 0)
    return t


def test_the_reaper_closes_an_idle_terminal_with_no_further_calls(bridge, repo, audit):
    t = short_lived(bridge, repo)
    try:
        t.open(ctx_for(bridge), {"id": "a1", "rows": 24, "cols": 80})
        deadline = time.monotonic() + 3
        while t._lives and time.monotonic() < deadline:   # no call, no close_all
            time.sleep(0.02)
        assert not t._lives, "the reaper never closed the idle terminal"
        assert [e["action"].rsplit(".", 1)[1] for e in audit] == ["attach", "detach"]
        assert [c["kind"] for c in t.wait_commands("beast", 0)] == ["close"]
    finally:
        t.close_all()


def test_close_all_stops_the_reaper_and_a_later_open_is_refused(bridge, repo, ctx):
    t = short_lived(bridge, repo)
    t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})
    reaper = t._reaper
    assert reaper.is_alive()
    t.close_all()
    reaper.join(2)
    assert not reaper.is_alive() and not t._lives
    assert [c["kind"] for c in t.wait_commands("beast", 0)] == ["close"]
    bridge._closed.set()
    with pytest.raises(BridgeError) as e:
        t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})
    assert e.value.status == 503


def test_the_reaper_survives_a_failing_pass_and_restarts_after_close_all(bridge, repo, ctx):
    t = short_lived(bridge, repo)
    calls = []
    real = t._reap

    def flaky():
        if threading.current_thread() is threading.main_thread():
            return real()                      # the open's own sweep
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        real()
    t._reap = flaky
    try:
        t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})
        deadline = time.monotonic() + 3
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 3 and t._reaper.is_alive()
        first = t._reaper
        t.close_all()
        first.join(2)
        t.wait_commands("beast", 0)
        t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})      # the manager is reusable until the bridge closes
        assert t._reaper is not first and t._reaper.is_alive()
    finally:
        t.close_all()


def test_a_reopen_refreshes_the_idle_clock(bridge, repo, ctx, clock):
    live(repo)
    t = rt.Terminal(bridge, BridgeError, ttl=100, clock=clock)
    t.repo = repo
    try:
        t.wait_commands("beast", 0)
        t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})
        clock.now += 9
        t.wait_commands("beast", 0)
        clock.now += 81
        t.wait_commands("beast", 0)
        t.open(ctx, {"id": "a1", "rows": 24, "cols": 80})
        clock.now += 9
        t.wait_commands("beast", 0)
        clock.now += 41                      # 140 s after the first open, 50 after the reopen
        t.wait_commands("beast", 0)
        t._reap()
        assert "a1" in t._lives
        clock.now += 9
        t.wait_commands("beast", 0)
        clock.now += 42
        t.wait_commands("beast", 0)
        t._reap()
        assert not t._lives
    finally:
        t.close_all()


def test_replacing_an_ended_pane_works_with_every_slot_full(bridge, ctx, repo, host):
    ids = [call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)["terminalId"] for i in range(4)]
    host.end(ids[0], 255)
    call(bridge, ctx, "open", id="a0", rows=24, cols=80)
    assert len(bridge.terminal._lives) == 4 and bridge.terminal._lives["a0"].alive
    assert status_of(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80) == 429


def test_bridge_close_still_closes_desk_and_push_when_the_terminals_fail(bridge, monkeypatch):
    shut = []

    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(bridge.terminal, "close_all", boom)
    bridge.desk = SimpleNamespace(close=lambda: shut.append("desk"))
    bridge.push = SimpleNamespace(close=lambda: shut.append("push"))
    bridge.close()
    monkeypatch.undo()                      # the fixture's own teardown closes the terminals for real
    assert shut == ["desk", "push"]


def test_an_input_that_lost_the_race_with_a_takeover_queues_nothing(bridge, ctx, repo, host, monkeypatch):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    stale = bridge.terminal._clients[tid]
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    monkeypatch.setattr(bridge.terminal, "_find", lambda c, i: stale)      # it passed the lookup before the takeover
    assert call(bridge, ctx, "input", terminalId=tid, data=b64("late"), n=1) == {"ok": True}
    assert host.sent("input") == []


def test_an_input_or_resize_in_flight_during_a_takeover_is_not_queued_after_the_close(bridge, ctx, repo, host, monkeypatch):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    stale = bridge.terminal._clients[tid]
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    stale.taken = False                                                     # it passed the taken check before the takeover
    monkeypatch.setattr(bridge.terminal, "_find", lambda c, i: stale)
    bridge.terminal.input(ctx, {"terminalId": tid, "data": b64("late"), "n": 1})
    bridge.terminal.resize(ctx, {"terminalId": tid, "rows": 30, "cols": 90})
    assert host.sent("input") == [] and host.sent("resize") == []


def test_a_takeover_waits_for_an_input_in_flight(bridge, ctx, repo):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    client = bridge.terminal._clients[tid]
    done = threading.Event()

    def reopen():
        call(bridge, ctx, "open", id="a1", rows=24, cols=80)
        done.set()
    with client.lock:                                  # an input is mid-queue
        threading.Thread(target=reopen, daemon=True).start()
        assert not done.wait(0.2) and not client.taken
    assert done.wait(2) and client.taken


def test_concurrent_opens_for_one_agent_make_one_terminal(bridge, ctx, repo, host):
    live(repo)
    out = []
    threads = [threading.Thread(target=lambda: out.append(call(bridge, ctx, "open", id="a1", rows=24, cols=80)))
               for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert len(out) == 3 and len({o["terminalId"] for o in out}) == 3
    assert len(bridge.terminal._lives) == 1 and len(bridge.terminal._clients) == 1   # only the newest id is current
    assert len(host.sent("open")) == 3 and len(host.sent("close")) == 2


def test_detach_is_audited_once_however_often_it_runs(bridge, ctx, repo, audit, host):
    live(repo)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    terminal = bridge.terminal._lives["a1"]
    bridge.terminal._detach(terminal)
    bridge.terminal._detach(terminal)
    assert [e["action"].rsplit(".", 1)[1] for e in audit] == ["attach", "detach"]
    assert len(host.sent("close")) == 1


def test_an_ended_terminal_without_a_stream_does_not_hold_a_slot(bridge, ctx, repo, host):
    ids = [call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)["terminalId"] for i in range(4)]
    host.end(ids[0], 255)
    resp = call(bridge, ctx, "subscribe", terminalId=ids[0], cursor=0)
    list(resp.frames)                                            # the ended frame is delivered, the stream closes
    resp.frames.close()
    resp.on_close()
    call(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80)
    assert len(bridge.terminal._lives) == 4 and "a0" not in bridge.terminal._lives


def test_an_ended_terminal_keeps_its_slot_until_the_ended_frame_was_seen(bridge, ctx, repo, host):
    ids = [call(bridge, ctx, "open", id=live(repo, f"a{i}"), rows=24, cols=80)["terminalId"] for i in range(4)]
    host.end(ids[0], 255)
    assert status_of(bridge, ctx, "open", id=live(repo, "a4"), rows=24, cols=80) == 429


def test_a_host_that_stops_polling_ends_its_terminals_with_host_offline(bridge, ctx, repo, clock, host):
    live(repo)
    live(repo, "b1", host="elsewhere")
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    call(bridge, ctx, "open", id="b1", rows=24, cols=80)
    clock.now += 9
    bridge.terminal.wait_commands("elsewhere", 0)               # only elsewhere keeps polling
    clock.now += 2                                               # beast: 11 s of silence
    bridge.terminal._reap()
    assert bridge.terminal._lives["a1"].chunks_after(0) == [
        {"seq": 1, "ended": True, "exit": None, "reason": "host_offline"}]
    assert bridge.terminal._lives["a1"].alive is False


def test_a_new_terminal_gets_ten_seconds_from_its_open_even_if_the_host_polled_long_ago(bridge, ctx, repo, clock):
    live(repo)
    clock.now += 9                                               # beast last polled 9 s ago: still connected
    tid = bridge.terminal.open(ctx, {"id": "a1", "rows": 24, "cols": 80})["terminalId"]
    bridge.terminal._reap()
    assert bridge.terminal._lives["a1"].alive
    clock.now += 10
    bridge.terminal._reap()
    assert not bridge.terminal._lives["a1"].alive and tid


def test_a_stuck_host_cannot_block_the_phone_calls(bridge, ctx, repo, host):
    live(repo)
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    for n in range(1, 301):                                      # the host never fetches: no call waits for it
        bridge.terminal.input(ctx, {"terminalId": tid, "data": b64("x"), "n": n})
    assert len(bridge.terminal._queues["beast"]) <= 256
    assert bridge.terminal._lives["a1"].chunks_after(0)[-1]["reason"] == "input_overflow"


def test_agents_list_is_unaffected(bridge, repo, ctx):
    bridge.agents.repo = repo
    live(repo)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    assert [a["id"] for a in bridge.agents.procs()["agents/list"](ctx, {})["agents"]] == ["a1"]
