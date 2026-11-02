"""agents/subscribe and agents/subscribeList: SSE streams mirroring threads/subscribe."""
import json
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.repo import ControlPlaneRepo

BridgeError = rb.BridgeError


@pytest.fixture
def repo():
    return ControlPlaneRepo()


@pytest.fixture
def bridge(repo):
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": [
        {"email": "a@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "operator"}]})
    b = rb.RakazoBridge(cfg, assistant=None, version="x")
    b.agents.repo = repo
    b.stream_options = {"poll_seconds": 0.01, "heartbeat_seconds": 0.05, "max_seconds": 0.5}
    return b


@pytest.fixture
def ctx(bridge):
    return SimpleNamespace(token=bridge.tokens.mint("acct", "127.0.0.1"), accepts_stream=True)


def sub(bridge, proc, ctx, **inp):
    return bridge.agents.procs()[f"agents/{proc}"](ctx, inp)


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


def feed(repo, sid, *events):
    return repo.inspector.ingest(sid, {"events": list(events)})


def test_subscribe_streams_new_events_after_cursor(bridge, repo, ctx):
    repo.upsert_session("s1", status="working")
    feed(repo, "s1", {"kind": "message", "role": "user", "text": "one"},
         {"kind": "message", "role": "assistant", "text": "two"})
    first = frames(sub(bridge, "subscribe", ctx, id="s1", cursor=-1))
    assert len(first) >= 2
    seqs = [e["seq"] for e in first]
    assert seqs == sorted(seqs)
    after = frames(sub(bridge, "subscribe", ctx, id="s1", cursor=seqs[0]))
    assert [e["seq"] for e in after] == seqs[1:]
    assert frames(sub(bridge, "subscribe", ctx, id="s1", cursor=seqs[-1])) == []


def test_subscribe_unknown_agent_is_404_and_no_repo_is_503(bridge, ctx):
    with pytest.raises(BridgeError) as e:
        sub(bridge, "subscribe", ctx, id="nope", cursor=-1)
    assert e.value.status == 404
    bridge.agents.repo = None
    with pytest.raises(BridgeError) as e:
        sub(bridge, "subscribeList", ctx, cursor=-1)
    assert e.value.status == 503


def test_subscribe_list_emits_only_changed_rows_with_increasing_seq(bridge, repo, ctx):
    repo.upsert_session("a", status="working")
    repo.upsert_session("b", status="idle")
    repo.upsert_session("old", status="closed")
    first = frames(sub(bridge, "subscribeList", ctx, cursor=-1))
    assert sorted(f["row"]["id"] for f in first) == ["a", "b"]
    seqs = [f["seq"] for f in first]
    assert seqs == sorted(set(seqs))
    last = seqs[-1]
    assert frames(sub(bridge, "subscribeList", ctx, cursor=last)) == []
    repo.upsert_session("a", status="blocked")
    repo.upsert_session("old", status="closed")
    changed = frames(sub(bridge, "subscribeList", ctx, cursor=last))
    assert [(f["row"]["id"], f["row"]["status"]) for f in changed] == [("a", "blocked")]
    assert changed[0]["seq"] > last
    # a shown row that closes is emitted once so the app can drop it; hidden rows stay quiet
    repo.upsert_session("b", status="closed")
    closing = frames(sub(bridge, "subscribeList", ctx, cursor=changed[0]["seq"]))
    assert [(f["row"]["id"], f["row"]["status"]) for f in closing] == [("b", "closed")]
    # a second subscriber sees the same seq numbers
    again = frames(sub(bridge, "subscribeList", ctx, cursor=-1))
    assert max(f["seq"] for f in again) == closing[0]["seq"]


def test_subscribe_rejects_bad_cursor_and_missing_accept_header(bridge, repo, ctx):
    repo.upsert_session("s1", status="working")
    for proc, extra in (("subscribe", {"id": "s1"}), ("subscribeList", {})):
        for bad in ("x", -2, True, None, 1.5):
            with pytest.raises(BridgeError) as e:
                sub(bridge, proc, ctx, cursor=bad, **extra)
            assert e.value.status == 400
        with pytest.raises(BridgeError) as e:
            sub(bridge, proc, SimpleNamespace(token=ctx.token, accepts_stream=False), cursor=0, **extra)
        assert e.value.status == 400


def test_subscribe_429_when_stream_cap_exhausted(bridge, repo, ctx):
    repo.upsert_session("s1", status="working")
    held = 0
    while bridge._streams.acquire(blocking=False):
        held += 1
    try:
        for proc, extra in (("subscribe", {"id": "s1"}), ("subscribeList", {})):
            with pytest.raises(BridgeError) as e:
                sub(bridge, proc, ctx, cursor=-1, **extra)
            assert e.value.status == 429
            assert e.value.headers == {"Retry-After": "10"}
    finally:
        for _ in range(held):
            bridge._streams.release()


def test_subscribe_releases_slot_and_stops_when_token_revoked(bridge, repo, ctx):
    repo.upsert_session("s1", status="working")
    bridge.stream_options = {"poll_seconds": 0.01, "heartbeat_seconds": 0.05, "max_seconds": 30}
    for proc, extra in (("subscribe", {"id": "s1"}), ("subscribeList", {})):
        resp = sub(bridge, proc, ctx, cursor=-1, **extra)
        it = iter(resp.frames)
        next(it)
        bridge.tokens.revoke(ctx.token)
        assert list(it) is not None   # the generator ends instead of running 30 s
        resp.on_close()
        ctx.token = bridge.tokens.mint("acct", "127.0.0.1")
    held = 0
    while bridge._streams.acquire(blocking=False):
        held += 1
    assert held == rb.MAX_STREAMS
    for _ in range(held):
        bridge._streams.release()
