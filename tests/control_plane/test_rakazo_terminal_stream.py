"""terminal/subscribe: numbered output frames the host pushed, ended, reset and taken_over."""
import base64
import json
import time

import pytest

from tests.control_plane.test_rakazo_terminal_rpc import (  # noqa: F401
    BridgeError, FakeHost, audit, b64, bridge, call, clock, ctx, ctx_for, host, live, repo, status_of)

pytestmark = pytest.mark.filterwarnings('ignore:This process .* is multi-threaded:DeprecationWarning')


def frames(resp):
    out = []
    try:
        for raw in resp.frames:
            event = json.loads(raw[6:])["json"]
            if event.get("type") != "heartbeat":
                out.append(event)
    finally:
        resp.frames.close()
        resp.on_close()
    return out


def opened(bridge, ctx, repo):
    live(repo)
    return call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]


def test_subscribe_from_the_open_seq_replays_the_buffer(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    host.say(tid, "one")
    host.say(tid, "two")
    host.end(tid, 0)
    out = frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0))
    assert [e["seq"] for e in out] == [2, 3]                     # a reattach replays only the newest frame
    assert base64.b64decode(out[0]["data"]) == b"two" and out[0]["reset"] is True
    assert out[1] == {"seq": 3, "ended": True, "exit": 0}


def test_subscribe_from_a_cursor_skips_what_was_seen(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    host.say(tid, "one")
    host.say(tid, "two")
    out = frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=1))
    assert [(e["seq"], e.get("reset")) for e in out] == [(2, True)]


def test_new_output_arrives_while_streaming(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    got = []
    for raw in resp.frames:
        ev = json.loads(raw[6:])["json"]
        if ev.get("type") == "heartbeat":
            if not got:
                host.say(tid, "late")
            continue
        got.append(ev)
        break
    resp.frames.close()
    resp.on_close()
    assert base64.b64decode(got[0]["data"]) == b"late"


def test_a_second_open_ends_the_first_stream_with_taken_over(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    host.say(tid, "a")
    bridge.stream_options["max_seconds"] = 5
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    out = frames(resp)                                           # ends by itself, not by max_seconds
    assert out[-1] == {"seq": out[-1]["seq"], "taken_over": True} and out[-1]["seq"] > 1
    assert not any(e.get("ended") for e in out) and not bridge.terminal._lives["a1"].closed


def test_a_closed_terminal_ends_its_stream(bridge, ctx, repo):
    tid = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    call(bridge, ctx, "close", terminalId=tid)
    assert [e.get("ended") for e in frames(resp)] == [True]


def test_the_stream_stops_after_the_ended_frame(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    host.end(tid, 255)
    assert frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)) == [{"seq": 1, "ended": True, "exit": 255}]


def test_a_host_ended_frame_carries_its_reason(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    host.end(tid, None, reason="pane_closed")
    assert frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)) == [
        {"seq": 1, "ended": True, "exit": None, "reason": "pane_closed"}]


def test_a_silent_host_ends_the_stream_with_host_offline(bridge, ctx, repo, clock):
    tid = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    clock.now += 11                                              # nobody polls for beast in between
    resp = bridge.terminal.subscribe(ctx, {"terminalId": tid, "cursor": 0})
    assert frames(resp) == [{"seq": 1, "ended": True, "exit": None, "reason": "host_offline"}]


def test_streamed_chunks_carry_the_pane_size_and_a_replay_the_newest_frames(bridge, ctx, repo, host):
    tid = opened(bridge, ctx, repo)
    host.say(tid, "one", rows=24, cols=80)
    host.say(tid, "two", rows=40, cols=120)
    out = frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0))
    assert [(e["seq"], e["rows"], e["cols"]) for e in out] == [(2, 40, 120)]
    again = call(bridge, ctx, "open", id="a1", rows=30, cols=100)               # reopen: the buffer is replayed
    out = frames(call(bridge, ctx, "subscribe", terminalId=again["terminalId"], cursor=again["seq"]))
    assert (out[-1]["rows"], out[-1]["cols"]) == (40, 120)


def test_a_newer_subscribe_for_the_same_terminal_ends_the_older_stream_at_once(bridge, ctx, repo):
    tid = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    older = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    newer = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    started = time.monotonic()
    out = frames(older)                                          # ends by itself, not after max_seconds
    assert time.monotonic() - started < 2 and not any(e.get("ended") for e in out)
    newer.frames.close()
    newer.on_close()


def test_subscribe_validation_and_polls_fast(bridge, ctx, repo, monkeypatch):
    tid = opened(bridge, ctx, repo)
    assert status_of(bridge, ctx, "subscribe", terminalId="nope", cursor=0) == 404
    assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor="0") == 400
    assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor=-2) == 400
    ctx.accepts_stream = False
    assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor=0) == 400
    ctx.accepts_stream = True
    seen = {}
    from herdr_engineering.control_plane import rakazo_events as ev
    real = ev.event_stream
    monkeypatch.setattr(ev, "event_stream", lambda *a, **k: seen.update(k) or real(*a, **k))
    frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0))
    assert seen["poll_seconds"] == 0.03 and seen["heartbeat_seconds"] == 0.05
    assert bridge.stream_options["poll_seconds"] == 0.01      # only this stream is overridden


def test_subscribe_respects_the_stream_cap(bridge, ctx, repo):
    tid = opened(bridge, ctx, repo)
    held = 0
    while bridge._streams.acquire(blocking=False):
        held += 1
    try:
        assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor=0) == 429
    finally:
        for _ in range(held):
            bridge._streams.release()
