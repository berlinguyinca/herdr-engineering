"""The host side of the terminal broker: ``GET /api/v1/host/terminal/wait`` and ``POST /api/v1/host/terminal/<id>/output``.

A live ControlPlaneServer is driven over HTTP with host keys; the phone side is the bridge's own ``terminal/open``."""
import base64
import json
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

    def wait(self, host="beast", timeout=0, key="host"):
        return self.call(f"{WAIT}?host_id={host}&timeout={timeout}", key=key)

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


def test_an_unbound_ingest_key_is_refused_when_binding_is_required_else_trusted(rig):
    svr, http = rig(architecture_options={"agent_hosts": BINDINGS, "require_host_binding": True})
    assert http.wait("beast", key="free")[0] == 403
    assert http.wait("beast", key="write")[0] == 200          # operators stay trusted, as for the summary reports
    svr2, http2 = rig(architecture_options={"agent_hosts": BINDINGS})
    assert http2.wait("beast", key="free")[0] == 200


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
    assert http.wait(timeout=0.4)[1] == {"commands": []} and 0.3 < time.monotonic() - started < 3


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
    assert http.wait()[1] == {"commands": []}                                   # fetched once


def test_a_waiter_ends_when_the_bridge_closes(env):
    svr, http = env
    got = []
    poller = threading.Thread(target=lambda: got.append(http.wait(timeout=30)), daemon=True)
    poller.start()
    time.sleep(0.2)
    svr.rakazo._closed.set()
    poller.join(3)
    assert got and got[0][1] == {"commands": []}


def test_at_most_16_waiters_and_the_17th_is_429(env):
    svr, http = env
    results = []
    threads = [threading.Thread(target=lambda: results.append(http.wait(timeout=2)[0]), daemon=True) for _ in range(16)]
    for th in threads:
        th.start()
    deadline = time.monotonic() + 3
    while svr.rakazo.terminal._waiters < 16 and time.monotonic() < deadline:
        time.sleep(0.02)
    status, _, headers = http.wait(timeout=2)
    assert status == 429 and headers.get("Retry-After")
    for th in threads:
        th.join(5)
    assert results == [200] * 16
    assert http.wait(timeout=0)[0] == 200                                       # the slots came back


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
    assert http.output(tid, [frame()])[0] == 200 and len(chunks(svr)) == 1     # accepted, but nothing follows an end


def test_the_command_queue_per_host_is_capped_at_256_oldest_input_first(env):
    svr, http = env
    tid = open_terminal(svr, http)
    term = svr.rakazo.terminal
    ctx = owner(svr)
    for n in range(1, 301):
        term.input(ctx, {"terminalId": tid, "data": b64(f"{n}"), "n": n})
    cmds = http.wait()[1]["commands"]
    assert len(cmds) == 256 and cmds[0]["kind"] == "open"                      # the open is never the victim while inputs exist
    assert [c["n"] for c in cmds[1:]] == list(range(46, 301))                  # 300 inputs + open = 301: the 45 oldest inputs were dropped
    note = base64.b64decode(chunks(svr)[-1]["data"])
    assert b"input dropped" in note and "reset" not in chunks(svr)[-1]         # the terminal says so, without clearing the screen
