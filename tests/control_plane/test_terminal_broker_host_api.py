"""The host side of the terminal broker: ``GET /api/v1/host/terminal/wait`` and ``POST /api/v1/host/terminal/<id>/output``.

A live ControlPlaneServer is driven over HTTP with host keys; the phone side is the bridge's own ``terminal/open``."""
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tests.control_plane.test_rakazo_bridge import bridge_section, make_server  # noqa: F401

KEYS = {"read": "viewer", "write": "operator", "host": "ingest", "host2": "ingest", "free": "ingest",
        "agent": "agent", "agent-b": "agent"}
# 'host' acts for beast, 'host2' for other; 'free' is an ingest key nobody bound; agent keys are never terminal hosts.
BINDINGS = {"host": ["beast"], "host2": ["other"], "agent-b": ["beast"]}
WAIT, OUT = "/api/v1/host/terminal/wait", "/api/v1/host/terminal/{}/output"


def b64(text):
    return base64.b64encode(text if isinstance(text, bytes) else text.encode()).decode()


def frame(text="x", rows=24, cols=80):
    return {"data": b64(text), "rows": rows, "cols": cols}


class Http:
    def __init__(self, svr):
        self.svr = svr
        self.base = f"http://127.0.0.1:{svr.port}"

    def call(self, path, body=None, key="host", method=None, headers=None, timeout=10):
        hdrs = {"Content-Type": "application/json", **({"X-API-Key": key} if key else {}), **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs,
                                     method=method or ("POST" if body is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.load(resp), dict(resp.headers)
        except urllib.error.HTTPError as err:
            raw = err.read()
            return err.code, (json.loads(raw) if raw else None), dict(err.headers)

    def wait(self, host="beast", timeout=0, key="host", after=None):
        extra = f"&after={after}" if after else ""
        return self.call(f"{WAIT}?host_id={host}&timeout={timeout}{extra}", key=key)

    def output(self, tid, frames, host="beast", key="host", **kw):
        return self.call(OUT.format(tid) + f"?host_id={host}", {"frames": frames}, key=key, **kw)


@pytest.fixture
def rig(make_server):
    def build(**kw):
        svr, _ = make_server(bridge_section(), api_keys=KEYS, **kw)
        return svr, Http(svr)
    return build


@pytest.fixture
def env(rig):
    svr, http = rig(architecture_options={"agent_hosts": BINDINGS})
    return svr, http


def owner(svr):
    bridge = svr.rakazo
    account = bridge.accounts[0]
    return SimpleNamespace(account=account, token=bridge.tokens.mint(account.account_id, "127.0.0.1"), accepts_stream=True)


def open_terminal(svr, http, host="beast", sid="a1"):
    svr.repo.upsert_session(sid, status="working", host_id=host, herdr_session="default", pane_id="w1:p1",
                            terminal_id="term_1")
    assert http.wait(host, key={"beast": "host", "other": "host2"}[host])[0] == 200      # the relay is polling
    out = svr.rakazo.terminal.open(owner(svr), {"id": sid, "rows": 30, "cols": 100})
    return out["terminalId"]


def chunks(svr, sid="a1", after=0):
    return svr.rakazo.terminal._lives[sid].chunks_after(after)


# ---------------------------------------------------------------------------------------------------- auth
def test_wait_needs_a_host_key(env):
    _, http = env
    assert http.wait(key=None)[0] == 401
    assert http.wait(key="nonsense")[0] == 401
    assert http.wait(key="read")[0] == 403                    # viewer
    assert http.wait(key="agent")[0] == 403                   # agent keys run inside sessions and may not read keystrokes
    assert http.wait(key="agent-b")[0] == 403                 # not even one bound to the host
    assert http.wait(key="host")[0] == 200


def test_output_needs_a_host_key(env):
    svr, http = env
    tid = open_terminal(svr, http)
    for key, want in ((None, 401), ("read", 403), ("agent", 403), ("agent-b", 403)):
        assert http.output(tid, [frame()], key=key)[0] == want
    assert chunks(svr) == []
    assert http.output(tid, [frame()])[0] == 200


def test_the_host_id_is_required_and_charset_checked(env):
    _, http = env
    assert http.call(f"{WAIT}?timeout=0")[0] == 400
    for bad in ("", "a%20b", "..%2Fx", "-x", "a" * 64, "be;ast"):
        assert http.call(f"{WAIT}?host_id={bad}&timeout=0")[0] == 400, bad
    assert http.call(f"{WAIT}?host_id=beast&timeout=soon")[0] == 400


def test_a_key_bound_to_other_hosts_gets_nothing_for_this_one(env):
    svr, http = env
    assert http.wait("beast", key="host2")[0] == 403          # bound to other
    assert http.wait("other", key="host")[0] == 403
    assert http.wait("other", key="host2")[0] == 200
    assert not svr.rakazo.terminal.host_connected("beast")    # the refused polls did not mark it connected


def test_an_unbound_key_is_refused_on_both_endpoints_under_the_default_config(env):
    svr, http = env                                           # agent_hosts is set, require_host_binding is NOT
    tid = open_terminal(svr, http)
    assert http.wait("beast", key="free")[0] == 403           # an ingest key nobody bound to a host
    assert http.output(tid, [frame()], key="free")[0] == 403
    assert http.wait("beast", key="write")[0] == 403          # operator keys cannot be bound, so they never qualify
    assert http.output(tid, [frame()], key="write")[0] == 403
    assert chunks(svr) == []


def test_without_any_agent_hosts_no_key_may_be_a_terminal_host(rig):
    svr, http = rig()
    assert http.wait("beast", key="host")[0] == 403
    assert http.wait("beast", key="write")[0] == 403
    svr2, http2 = rig(architecture_options={"agent_hosts": BINDINGS, "require_host_binding": True})
    assert http2.wait("beast", key="free")[0] == 403
    assert http2.wait("beast", key="host")[0] == 200


def test_a_key_bound_to_one_host_cannot_poll_or_post_for_another(env):
    svr, http = env
    tid = open_terminal(svr, http, "beast")
    open_terminal(svr, http, "other", sid="b1")
    assert http.wait("other", key="host")[0] == 403
    assert http.wait("beast", key="host2")[0] == 403
    assert http.output(tid, [frame()], host="other", key="host")[0] == 403
    assert http.output(tid, [frame()], host="beast", key="host2")[0] == 403
    assert chunks(svr) == []


def test_output_for_a_terminal_on_another_host_is_refused(env):
    svr, http = env
    tid = open_terminal(svr, http, "beast")
    assert http.output(tid, [frame()], host="other", key="host2")[0] == 404    # bound to other, but not this terminal's host
    assert http.output(tid, [frame()], host="beast", key="host2")[0] == 403    # bound to other: cannot claim beast
    assert http.output(tid, [frame()], host="other", key="host")[0] == 403
    assert chunks(svr) == []
    assert http.output(tid, [frame()], host="beast", key="host")[0] == 200


def test_output_for_an_unknown_or_retired_terminal_is_404(env):
    svr, http = env
    first = open_terminal(svr, http)
    assert http.output("nope", [frame()])[0] == 404
    second = svr.rakazo.terminal.open(owner(svr), {"id": "a1", "rows": 24, "cols": 80})["terminalId"]   # takeover
    assert http.output(first, [frame()])[0] == 404
    assert http.output(second, [frame()])[0] == 200


def test_posts_must_be_json_and_same_origin(env):
    svr, http = env
    tid = open_terminal(svr, http)
    assert len(tid) >= 22                                                      # token_urlsafe(16): unguessable
    assert http.output(tid, [frame()], headers={"Content-Type": "text/plain"})[0] == 415
    assert http.output(tid, [frame()], headers={"Origin": "https://evil.example"})[0] == 403


# ---------------------------------------------------------------------------------------------- long-poll
def test_wait_returns_at_once_with_an_empty_list_when_nothing_is_queued(env):
    _, http = env
    started = time.monotonic()
    status, body, _ = http.wait(timeout=0)
    assert (status, body) == (200, {"commands": []}) and time.monotonic() - started < 1


def test_wait_wakes_when_a_command_is_queued_and_times_out_empty_otherwise(env):
    svr, http = env
    svr.repo.upsert_session("a1", status="working", host_id="beast", herdr_session="default", pane_id="w1:p1",
                            terminal_id="term_1")
    http.wait()                                               # connected
    got = []
    poller = threading.Thread(target=lambda: got.append(http.wait(timeout=20)), daemon=True)
    started = time.monotonic()
    poller.start()
    time.sleep(0.3)
    assert not got                                            # blocked, not answered
    tid = svr.rakazo.terminal.open(owner(svr), {"id": "a1", "rows": 30, "cols": 100})["terminalId"]
    poller.join(5)
    assert got and time.monotonic() - started < 5
    (cmd,) = got[0][1]["commands"]
    assert cmd["kind"] == "open" and cmd["terminal"] == tid and cmd["id"]
    assert (cmd["pane_id"], cmd["rows"], cmd["cols"]) == ("w1:p1", 30, 100)
    started = time.monotonic()
    assert http.wait(timeout=0.4, after=cmd["id"])[1] == {"commands": []} and 0.3 < time.monotonic() - started < 3


def test_wait_delivers_input_in_order_and_only_to_its_own_host(env):
    svr, http = env
    tid = open_terminal(svr, http)
    http.wait("other", key="host2")
    term = svr.rakazo.terminal
    for n, text in ((1, "ls"), (2, "\r")):
        term.input(owner(svr), {"terminalId": tid, "data": b64(text), "n": n})
    assert http.wait("other", key="host2")[1] == {"commands": []}
    cmds = http.wait()[1]["commands"]
    assert [(c["kind"], c.get("n"), c.get("data")) for c in cmds] == [
        ("open", None, None), ("input", 1, b64("ls")), ("input", 2, b64("\r"))]
    assert len({c["id"] for c in cmds}) == 3 and len(cmds[0]["id"]) >= 16        # unguessable
    ids = [c["id"] for c in cmds]
    assert all(re.fullmatch(r"[0-9a-f]{8}-\d+-[A-Za-z0-9_-]{16,}", i) for i in ids)   # <epoch>-<counter>-<random>
    assert len({i.split("-")[0] for i in ids}) == 1                             # one epoch per process
    assert [int(i.split("-")[1]) for i in ids] == sorted(int(i.split("-")[1]) for i in ids)


def test_a_waiter_ends_when_the_bridge_closes(env):
    svr, http = env
    got = []
    poller = threading.Thread(target=lambda: got.append(http.wait(timeout=30)), daemon=True)
    poller.start()
    time.sleep(0.2)
    svr.rakazo._closed.set()
    poller.join(3)
    assert got and got[0][1] == {"commands": []}


# ------------------------------------------------------------------------------------------------ output
def test_output_frames_are_stored_with_a_seq_as_reset_chunks(env):
    svr, http = env
    tid = open_terminal(svr, http)
    status, body, _ = http.output(tid, [frame("one"), frame("two", rows=40, cols=120)])
    assert status == 200 and body == {"ok": True}
    got = chunks(svr)
    assert [(c["seq"], c["reset"], base64.b64decode(c["data"])) for c in got] == [(1, True, b"one"), (2, True, b"two")]
    http.output(tid, [{"ended": True, "exit": 3, "reason": "pane_closed"}])
    assert chunks(svr, after=2) == [{"seq": 3, "ended": True, "exit": 3, "reason": "pane_closed"}]
    http.output(tid, [frame("after the end")])                                 # nothing follows an end
    assert len(chunks(svr)) == 3


def test_a_frame_over_160_kb_or_more_than_20_frames_store_nothing(env):
    svr, http = env
    tid = open_terminal(svr, http)
    assert http.output(tid, [frame(), {"data": "A" * (160 * 1024 + 4), "rows": 24, "cols": 80}])[0] == 413
    assert http.output(tid, [frame()] * 21)[0] == 413
    assert chunks(svr) == []                                                   # a refused POST is all or nothing
    assert http.output(tid, [{"data": "A" * (160 * 1024), "rows": 24, "cols": 80}])[0] == 200
    assert http.output(tid, [frame()] * 20)[0] == 200 and len(chunks(svr)) == 21


def test_malformed_frames_are_400_and_store_nothing(env):
    svr, http = env
    tid = open_terminal(svr, http)
    bad = [[{"data": "!!!", "rows": 24, "cols": 80}], [{"data": b64("x"), "rows": 0, "cols": 80}],
           [{"data": b64("x"), "rows": "24", "cols": 80}], [{"data": 7, "rows": 24, "cols": 80}], ["text"],
           [{"ended": True, "exit": "x"}], [{"ended": True, "reason": "r" * 300}]]
    for frames in bad:
        assert http.output(tid, [frame()] + frames)[0] == 400, frames
    assert http.call(OUT.format(tid) + "?host_id=beast", {"frames": "no"})[0] == 400
    assert chunks(svr) == []


def test_the_buffer_keeps_the_last_200_kb_and_at_least_the_newest_frame(env):
    svr, http = env
    tid = open_terminal(svr, http)
    big = {"data": "A" * 150_000, "rows": 24, "cols": 80}
    for _ in range(5):
        http.output(tid, [big])
    assert [c["seq"] for c in chunks(svr)] == [5]                              # 150 KB each: only the newest fits twice over
    small = {"data": "A" * 40_000, "rows": 24, "cols": 80}
    for _ in range(8):
        http.output(tid, [small])
    kept = chunks(svr)
    assert [c["seq"] for c in kept] == [9, 10, 11, 12, 13] and all(c["reset"] for c in kept)
    assert chunks(svr, after=11) == kept[3:]                                  # a cursor still reads what is newer


def test_a_silent_host_ends_its_terminal_with_host_offline(env):
    svr, http = env
    tid = open_terminal(svr, http)
    now = [time.monotonic()]
    svr.rakazo.terminal._clock = lambda: now[0]
    http.wait()                                                                # polled "now"
    now[0] += 11
    svr.rakazo.terminal._reap()
    assert chunks(svr) == [{"seq": 1, "ended": True, "exit": None, "reason": "host_offline"}]
    assert http.output(tid, [frame()])[0] == 404 and len(chunks(svr)) == 1     # a relay's probe learns of the end; nothing follows it


def test_a_full_queue_ends_the_terminal_whose_input_overflowed_and_never_leaves_a_gap(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term, ctx = svr.rakazo.terminal, owner(svr)
    for n in range(1, 301):                                                    # the host never fetches
        term.input(ctx, {"terminalId": tid, "data": b64(f"{n}"), "n": n})
    last = chunks(svr)[-1]
    assert last["ended"] is True and last["reason"] == "input_overflow"
    assert http.wait()[1] == {"commands": []}                                  # none of its inputs is delivered with a hole in n


def test_input_overflow_drops_only_the_overflowing_terminals_inputs(env):
    svr, http = env
    t1 = open_terminal(svr, http, sid="a1")
    t2 = open_terminal(svr, http, sid="a2")
    term, ctx = svr.rakazo.terminal, owner(svr)
    http.wait(after=http.wait()[1]["commands"][-1]["id"])                       # both opens delivered and acked
    for n in range(1, 128):                                                    # 254 queued inputs, alternating, t1's first
        term.input(ctx, {"terminalId": t1, "data": b64("a"), "n": n})
        term.input(ctx, {"terminalId": t2, "data": b64("b"), "n": n})
    for n in range(128, 131):                                                  # t2 keeps typing into a full queue
        term.input(ctx, {"terminalId": t2, "data": b64("b"), "n": n})
    cmds = http.wait()[1]["commands"]
    assert chunks(svr, "a1")[-1]["reason"] == "input_overflow" and chunks(svr, "a2") == []
    assert [c["n"] for c in cmds if c["terminal"] == t2] == list(range(1, 131))   # the other terminal has no gap
    assert not [c for c in cmds if c["terminal"] == t1 and c["kind"] == "input"]


def test_a_full_queue_with_no_input_to_drop_refuses_and_keeps_the_open(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term, ctx = svr.rakazo.terminal, owner(svr)
    for _ in range(255):
        term.resize(ctx, {"terminalId": tid, "rows": 24, "cols": 80})
    with pytest.raises(term.Error) as e:
        term.resize(ctx, {"terminalId": tid, "rows": 24, "cols": 80})
    assert e.value.status == 503
    cmds = http.wait()[1]["commands"]
    assert len(cmds) == 256 and cmds[0]["kind"] == "open"                      # an open (or close) is never dropped for room


# ------------------------------------------------------------------------------- at-least-once delivery
def test_commands_stay_queued_until_acked_with_after(env):
    svr, http = env
    tid = open_terminal(svr, http)
    for n in (1, 2):
        svr.rakazo.terminal.input(owner(svr), {"terminalId": tid, "data": b64("x"), "n": n})
    first = http.wait()[1]["commands"]
    assert [c["kind"] for c in first] == ["open", "input", "input"]
    assert http.wait()[1]["commands"] == first                                  # not acked: delivered again, same ids
    assert http.wait(after=first[0]["id"])[1]["commands"] == first[1:]          # the ack covers everything up to that id
    assert http.wait(after=first[-1]["id"])[1] == {"commands": []}
    assert http.wait()[1] == {"commands": []}                                   # and an ack is permanent


def test_an_orphan_waiter_does_not_swallow_commands(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term = svr.rakazo.terminal
    got = term.wait_commands("beast", 0)                                        # a relay whose connection then died
    term.input(owner(svr), {"terminalId": tid, "data": b64("x"), "n": 1})
    again = http.wait()[1]["commands"]
    assert [c["id"] for c in again][:len(got)] == [c["id"] for c in got] and again[-1]["kind"] == "input"


def test_unacked_commands_expire_after_60_seconds(env):
    svr, http = env
    tid = open_terminal(svr, http)
    now = [time.monotonic()]
    svr.rakazo.terminal._clock = lambda: now[0]
    http.wait()
    now[0] += 59
    assert [c["kind"] for c in http.wait()[1]["commands"]] == ["open"]
    now[0] += 2
    assert http.wait()[1] == {"commands": []} and tid


def test_a_malformed_after_cursor_is_400(env):
    _, http = env
    assert http.call(f"{WAIT}?host_id=beast&timeout=0&after=nope")[0] == 400


# --------------------------------------------------------------------------------------- stale commands
def test_a_terminal_that_ended_leaves_no_stale_commands_for_a_host_that_returns(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term, ctx = svr.rakazo.terminal, owner(svr)
    now = [time.monotonic()]
    term._clock = lambda: now[0]
    http.wait()                                                                # connected; the open is queued, never fetched
    term.input(ctx, {"terminalId": tid, "data": b64("typed just before it went down"), "n": 1})
    term.resize(ctx, {"terminalId": tid, "rows": 30, "cols": 90, "zoom": True})
    now[0] += 11                                                               # the host went down
    term._reap()
    assert chunks(svr)[-1]["reason"] == "host_offline"
    now[0] += 1                                                                # back, well inside the 60 s expiry
    assert http.wait()[1] == {"commands": []}                                  # no open, input, resize or zoom is replayed
    now[0] += 3600
    assert http.wait()[1] == {"commands": []}


def test_a_closed_or_retired_terminal_keeps_only_its_close(env):
    svr, http = env
    first = open_terminal(svr, http)
    term, ctx = svr.rakazo.terminal, owner(svr)
    term.input(ctx, {"terminalId": first, "data": b64("x"), "n": 1})
    second = term.open(ctx, {"id": "a1", "rows": 24, "cols": 80})["terminalId"]   # takeover before anything was fetched
    sent = http.wait()[1]["commands"]
    assert [(c["kind"], c["terminal"]) for c in sent] == [("close", first), ("open", second)]
    http.wait(after=sent[-1]["id"])
    term.input(ctx, {"terminalId": second, "data": b64("y"), "n": 1})
    term.close(ctx, {"terminalId": second})
    assert [(c["kind"], c["terminal"]) for c in http.wait()[1]["commands"]] == [("close", second)]


# ------------------------------------------------------------------------------------------ waiter caps
def test_a_newer_wait_for_the_same_host_ends_the_older_one(env):
    svr, http = env
    got = []
    older = threading.Thread(target=lambda: got.append(http.wait(timeout=20)), daemon=True)
    older.start()
    time.sleep(0.3)
    newer = threading.Thread(target=lambda: got.append(http.wait(timeout=1.5)), daemon=True)
    newer.start()
    older.join(1.0)
    assert not older.is_alive() and got == [(200, {"commands": []}, got[0][2])]   # the older poll was ended, empty
    newer.join(5)


def test_one_host_cannot_lock_out_the_others_and_many_hosts_can_poll(env):
    svr, http = env
    spam = [threading.Thread(target=lambda: http.wait("beast", timeout=1.5), daemon=True) for _ in range(30)]
    for th in spam:
        th.start()
    time.sleep(0.3)
    assert http.wait("other", key="host2")[0] == 200                            # not locked out by beast's 30 polls
    assert svr.rakazo.terminal._waiters <= 30
    for th in spam:
        th.join(5)


def test_more_than_16_hosts_can_wait_at_once_and_the_65th_waiter_is_429(env):
    svr, http = env
    term = svr.rakazo.terminal
    results = []
    threads = [threading.Thread(target=lambda h=f"h{i}": results.append(term.wait_commands(h, 1.5)), daemon=True)
               for i in range(64)]
    for th in threads:
        th.start()
    deadline = time.monotonic() + 3
    while term._waiters < 64 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert term._waiters == 64
    status, _, headers = http.wait(timeout=2)
    assert status == 429 and headers.get("Retry-After")
    for th in threads:
        th.join(5)
    assert len(results) == 64


# ---------------------------------------------------------------------------------------------- redaction
@pytest.mark.parametrize("size", [500, 2000, 4000])
def test_the_wait_response_is_never_redacted(rig, size):
    token = "cifix-test-credential-98c2a1"
    svr, http = rig(architecture_options={"agent_hosts": BINDINGS},
                    ci_fix={"enabled": True, "agent_api_key": token, "hosts": ["beast"]})
    tid = open_terminal(svr, http)
    raw = bytes([0x41]) * (size * 3 // 4 - 1) + bytes([0x1c])                  # base64 text of `size` chars ending in 'c'
    data = b64(raw)
    assert len(data) == size and data.endswith(token[0])
    svr.rakazo.terminal.input(owner(svr), {"terminalId": tid, "data": data, "n": 1})
    cmds = http.wait()[1]["commands"]
    assert cmds[-1]["data"] == data


# ----------------------------------------------------------------------------------------- the ack cursor
def test_an_after_cursor_from_another_epoch_or_beyond_anything_issued_acks_nothing(env):
    svr, http = env
    open_terminal(svr, http)
    (cmd,) = http.wait()[1]["commands"]
    epoch, counter, rand = cmd["id"].split("-", 2)
    other = "0" * 8 if epoch != "0" * 8 else "1" * 8
    for forged in (f"{other}-{counter}-{rand}", f"{other}-1-{rand}", f"{epoch}-{int(counter) + 1000}-{rand}"):
        assert http.wait(after=forged)[1]["commands"] == [cmd], forged           # ignored: the whole queue comes back
    assert http.wait(after=cmd["id"])[1] == {"commands": []}


def test_a_retried_after_cursor_is_harmless(env):
    svr, http = env
    tid = open_terminal(svr, http)
    svr.rakazo.terminal.input(owner(svr), {"terminalId": tid, "data": b64("x"), "n": 1})
    first = http.wait()[1]["commands"]
    assert http.wait(after=first[0]["id"])[1]["commands"] == first[1:]
    assert http.wait(after=first[0]["id"])[1]["commands"] == first[1:]           # the same ack again acks nothing more


def test_a_command_that_expired_ends_its_terminal_so_n_has_no_gap(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term = svr.rakazo.terminal
    now = [time.monotonic()]
    term._clock = lambda: now[0]
    http.wait(after=http.wait()[1]["commands"][0]["id"])
    term.input(owner(svr), {"terminalId": tid, "data": b64("lost"), "n": 1})
    now[0] += 61
    assert http.wait()[1] == {"commands": []}
    assert chunks(svr)[-1]["reason"] == "stale"


def test_a_replaced_waiter_stops_counting_and_a_takeover_needs_no_free_slot(env):
    svr, http = env
    term = svr.rakazo.terminal
    threads = [threading.Thread(target=lambda h=f"h{i}": term.wait_commands(h, 1.5), daemon=True) for i in range(64)]
    for th in threads:
        th.start()
    deadline = time.monotonic() + 3
    while term._waiters < 64 and time.monotonic() < deadline:
        time.sleep(0.02)
    outcome = []

    def retake():
        try:
            outcome.append(term.wait_commands("h0", 1))
        except Exception as exc:  # noqa: BLE001
            outcome.append(type(exc).__name__)
    again = threading.Thread(target=retake, daemon=True)
    again.start()                                                              # h0 already waits: it takes over its slot
    time.sleep(0.3)
    assert term._waiters == 64
    assert http.wait("beast", timeout=0)[0] == 429
    for th in threads + [again]:
        th.join(5)
    assert outcome == [[]] and term._waiters == 0 and not term._blocked


# ---------------------------------------------------------------------------------------------- room for closes
def test_a_close_is_queued_even_when_the_queue_is_full_of_other_terminals_commands(env):
    svr, http = env
    t1 = open_terminal(svr, http, sid="a1")
    http.wait(after=http.wait()[1]["commands"][-1]["id"])
    t2 = open_terminal(svr, http, sid="a2")
    term, ctx = svr.rakazo.terminal, owner(svr)
    for _ in range(255):
        term.resize(ctx, {"terminalId": t2, "rows": 24, "cols": 80})
    assert len(term._queues["beast"]) == 256
    term.close(ctx, {"terminalId": t1})
    assert [(c["kind"], c["terminal"]) for c in http.wait()[1]["commands"]][-1] == ("close", t1)


def test_a_resize_with_zoom_queues_both_commands_or_neither(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term, ctx = svr.rakazo.terminal, owner(svr)
    for _ in range(254):
        term.resize(ctx, {"terminalId": tid, "rows": 24, "cols": 80})
    assert len(term._queues["beast"]) == 255
    with pytest.raises(term.Error):
        term.resize(ctx, {"terminalId": tid, "rows": 30, "cols": 90, "zoom": True})
    assert len(term._queues["beast"]) == 255


def test_the_terminal_cap_is_checked_before_any_terminal_is_ended_for_room(env):
    svr, http = env
    term, ctx = svr.rakazo.terminal, owner(svr)
    first = open_terminal(svr, http, sid="a0")
    for i in range(1, 4):
        svr.repo.upsert_session(f"a{i}", status="working", host_id="beast", herdr_session="default",
                                pane_id="w1:p1", terminal_id="term_1")
        term.open(ctx, {"id": f"a{i}", "rows": 24, "cols": 80})
    n = 0
    while len(term._queues["beast"]) < 255:
        n += 1
        term.input(ctx, {"terminalId": first, "data": b64("x"), "n": n})
    svr.repo.upsert_session("a4", status="working", host_id="beast", herdr_session="default",
                            pane_id="w1:p1", terminal_id="term_1")
    with pytest.raises(term.Error) as e:
        term.open(ctx, {"id": "a4", "rows": 24, "cols": 80})
    assert e.value.status == 429 and term._lives["a0"].alive                    # a refused open ended nothing


def test_a_push_for_an_ended_terminal_answers_404_so_a_probe_finds_it(env):
    svr, http = env
    tid = open_terminal(svr, http)
    assert http.output(tid, [frame()])[0] == 200
    assert http.output(tid, [{"ended": True, "exit": 0}])[0] == 200
    assert http.output(tid, [frame()])[0] == 404              # ended on the broker, not yet reaped
    live = svr.rakazo.terminal._lives["a1"]
    tid2 = open_terminal(svr, http, sid="a2")
    svr.rakazo.terminal._finish(svr.rakazo.terminal._lives["a2"], None, "host_offline", host_knows=True)
    assert http.output(tid2, [frame()])[0] == 404             # host_offline: the returning host learns at once
    assert live is not None


# ------------------------------------------------------------------------------------------- pane size
def test_data_chunks_carry_the_rows_and_cols_of_their_frame(env):
    svr, http = env
    tid = open_terminal(svr, http)
    http.output(tid, [frame("one", rows=24, cols=80), frame("two", rows=40, cols=120)])
    http.output(tid, [{"ended": True, "exit": 0}])
    got = chunks(svr)
    assert [(c["seq"], c["rows"], c["cols"]) for c in got[:2]] == [(1, 24, 80), (2, 40, 120)]   # a resized pane's next frame
    assert "rows" not in got[2]                                                # an ended chunk needs no size
