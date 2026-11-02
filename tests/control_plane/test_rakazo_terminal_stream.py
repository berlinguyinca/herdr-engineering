"""terminal/subscribe: numbered output frames, ended, reset and taken_over."""
import base64
import json

import pytest

from tests.control_plane.test_rakazo_terminal_rpc import (  # noqa: F401
    BridgeError, FakeSession, audit, b64, bridge, call, clock, ctx, ctx_for, live, repo, status_of)

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
    tid = call(bridge, ctx, "open", id="a1", rows=24, cols=80)["terminalId"]
    return tid, FakeSession.made[-1]


def test_subscribe_from_the_open_seq_replays_the_buffer(bridge, ctx, repo):
    tid, s = opened(bridge, ctx, repo)
    s.say("one")
    s.say("two")
    s.chunks.append({"seq": 3, "ended": True, "exit": 0})
    out = frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0))
    assert [e["seq"] for e in out] == [1, 2, 3]
    assert base64.b64decode(out[0]["data"]) == b"one" and out[2] == {"seq": 3, "ended": True, "exit": 0}


def test_subscribe_from_a_cursor_skips_what_was_seen_and_passes_reset(bridge, ctx, repo):
    tid, s = opened(bridge, ctx, repo)
    s.say("one")
    s.say("two")
    s.chunks[1]["reset"] = True
    out = frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=1))
    assert [(e["seq"], e.get("reset")) for e in out] == [(2, True)]


def test_new_output_arrives_while_streaming(bridge, ctx, repo):
    tid, s = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    got = []
    for raw in resp.frames:
        ev = json.loads(raw[6:])["json"]
        if ev.get("type") == "heartbeat":
            if not got:
                s.say("late")
            continue
        got.append(ev)
        break
    resp.frames.close()
    resp.on_close()
    assert base64.b64decode(got[0]["data"]) == b"late"


def test_a_second_open_ends_the_first_stream_with_taken_over(bridge, ctx, repo):
    tid, s = opened(bridge, ctx, repo)
    s.say("a")
    bridge.stream_options["max_seconds"] = 5
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    call(bridge, ctx, "open", id="a1", rows=24, cols=80)
    out = frames(resp)                                           # ends by itself, not by max_seconds
    assert out[-1] == {"seq": out[-1]["seq"], "taken_over": True} and out[-1]["seq"] > 1
    assert not any(e.get("ended") for e in out) and not s.closed


def test_a_closed_terminal_ends_its_stream(bridge, ctx, repo):
    tid, s = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    resp = call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)
    call(bridge, ctx, "close", terminalId=tid)
    assert [e.get("ended") for e in frames(resp)] == [True]


def test_the_stream_stops_after_the_ended_frame(bridge, ctx, repo):
    tid, s = opened(bridge, ctx, repo)
    bridge.stream_options["max_seconds"] = 5
    s.chunks.append({"seq": 1, "ended": True, "exit": 255})
    assert frames(call(bridge, ctx, "subscribe", terminalId=tid, cursor=0)) == [{"seq": 1, "ended": True, "exit": 255}]


def test_subscribe_validation_and_polls_fast(bridge, ctx, repo, monkeypatch):
    tid, _ = opened(bridge, ctx, repo)
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
    tid, _ = opened(bridge, ctx, repo)
    held = 0
    while bridge._streams.acquire(blocking=False):
        held += 1
    try:
        assert status_of(bridge, ctx, "subscribe", terminalId=tid, cursor=0) == 429
    finally:
        for _ in range(held):
            bridge._streams.release()
