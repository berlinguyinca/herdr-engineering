"""Share housekeeping: expiry sweep, events ingest, orphan sweep, and the rate-limited worker wrapper."""
import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane import share_bundle, worker
from herdr_engineering.control_plane.artifact_shares import new_token
from herdr_engineering.control_plane.worker import ShareHousekeeper, share_housekeeping

from .test_share_store import index, kw, run, seed, share_postgres  # noqa: F401


def make_state(root: Path, token_hash: str):
    share_bundle.write_state(root, token_hash, {"share_id": "x"})


def make_bundle(root: Path, share_id: str, age: float = 0.0):
    d = root / share_id
    d.mkdir(parents=True)
    (d / "index.html").write_text("hi")
    if age:
        old = time.time() - age
        os.utime(d, (old, old))
    return d


async def live_share(store, conn, root, **over):
    aid, vid = await seed(conn)
    s = await store.create(**kw(aid, vid, **over))
    _, th = new_token()
    await store.mark_live(s["share_id"], token_hash=th, index=index(), approved_by="op")
    make_state(root, th)
    make_bundle(root, s["share_id"])
    return s["share_id"], th


async def expire(conn, sid):
    await conn.execute("UPDATE artifact_shares SET expires_at=now() - interval '1 minute' WHERE share_id=$1", sid)


def test_expired_share_is_swept_and_other_untouched(share_postgres, tmp_path):
    async def body(store, conn):
        a, ah = await live_share(store, conn, tmp_path)
        b, bh = await live_share(store, conn, tmp_path)
        await expire(conn, a)
        out = await share_housekeeping(store, tmp_path, tmp_path / "none.jsonl", time.time())
        assert out == {"expired": 1, "ingested": 0, "orphans_removed": 0}
        assert not (tmp_path / "by-token" / ah).exists() and not (tmp_path / a).exists()
        assert (await store.get(a))["state"] == "revoked"
        kinds = [e["kind"] for e in await store.events(a)]
        assert "expired" in kinds and "revoked" in kinds
        assert (tmp_path / "by-token" / bh / "state.json").exists() and (tmp_path / b).exists()
        assert (await store.get(b))["state"] == "live"
    run(share_postgres, body)


def test_one_failure_does_not_stop_others(share_postgres, tmp_path, monkeypatch):
    async def body(store, conn):
        a, _ = await live_share(store, conn, tmp_path)
        b, bh = await live_share(store, conn, tmp_path)
        await expire(conn, a)
        await expire(conn, b)
        real = share_bundle.remove_bundle

        def flaky(root, sid):
            if sid == a:
                raise OSError("boom")
            real(root, sid)
        monkeypatch.setattr(worker, "remove_bundle", flaky)
        out = await share_housekeeping(store, tmp_path, tmp_path / "none.jsonl", time.time())
        assert out["expired"] == 1
        assert not (tmp_path / "by-token" / bh).exists() and not (tmp_path / b).exists()
        assert (await store.get(a))["state"] == "revoked"  # revoked even though the bundle removal failed
    run(share_postgres, body)


def test_events_ingested_then_zero(share_postgres, tmp_path):
    async def body(store, conn):
        sid, _ = await live_share(store, conn, tmp_path)
        events = tmp_path / "events.jsonl"
        marker = tmp_path / "markers" / "m.json"
        marker.parent.mkdir()
        line = json.dumps({"share_id": sid, "kind": "opened", "actor": "a@x.org",
                           "at": datetime.now(UTC).isoformat(), "detail": {}})
        events.write_text(line + "\n")
        first = await share_housekeeping(store, tmp_path, events, time.time(), marker_path=marker)
        assert first["ingested"] == 1
        second = await share_housekeeping(store, tmp_path, events, time.time(), marker_path=marker)
        assert second["ingested"] == 0
        assert [e["kind"] for e in await store.events(sid)].count("opened") == 1
    run(share_postgres, body)


def test_housekeeping_rotates_the_events_file(share_postgres, tmp_path, monkeypatch):
    """share-public stops appending at its cap; the worker must rotate the file so a busy link cannot silence audit."""
    from herdr_engineering.control_plane import artifact_shares
    monkeypatch.setattr(artifact_shares, "EVENTS_ROTATE_BYTES", 300)

    async def body(store, conn):
        sid, _ = await live_share(store, conn, tmp_path / "shares")
        d = tmp_path / "share-events"
        d.mkdir()
        events, rotated = d / "events.jsonl", d / "events.jsonl.1"
        marker = tmp_path / "markers" / "m.json"
        marker.parent.mkdir()

        def lines(start, count):
            return "".join(json.dumps({"share_id": sid, "kind": "opened", "actor": "a@x.org",
                                       "at": datetime.now(UTC).isoformat(), "detail": {"n": start + i}}) + "\n"
                           for i in range(count))
        events.write_text(lines(0, 5))
        t = time.time()
        out = await share_housekeeping(store, tmp_path / "shares", events, t, marker_path=marker)
        assert out["ingested"] == 5 and rotated.exists() and not events.exists()
        events.write_text(lines(5, 1))
        os.utime(rotated, (t - 3600, t - 3600))
        out = await share_housekeeping(store, tmp_path / "shares", events, t + 60, marker_path=marker)
        assert out["ingested"] == 1 and not rotated.exists()
        ns = [e["detail"]["n"] for e in await store.events(sid, limit=100) if e["kind"] == "opened"]
        assert sorted(ns) == list(range(6))
    run(share_postgres, body)


def test_missing_dirs_are_not_errors(share_postgres, tmp_path):
    async def body(store, conn):
        out = await share_housekeeping(store, tmp_path / "nope", tmp_path / "nope" / "e.jsonl", time.time())
        assert out == {"expired": 0, "ingested": 0, "orphans_removed": 0}
    run(share_postgres, body)


def test_orphan_sweep(share_postgres, tmp_path):
    async def body(store, conn):
        sid, th = await live_share(store, conn, tmp_path)
        aid, vid = await seed(conn)
        pending = (await store.create(**kw(aid, vid)))["share_id"]
        make_bundle(tmp_path, pending, age=7200)
        orphan_hash = "ab" * 32
        make_state(tmp_path, orphan_hash)
        old = time.time() - 7200
        os.utime(tmp_path / "by-token" / orphan_hash, (old, old))
        recent_hash = "cd" * 32
        make_state(tmp_path, recent_hash)  # in-flight approve: recent, not live yet
        unknown_old = make_bundle(tmp_path, "share_" + "1" * 24, age=7200)
        unknown_new = make_bundle(tmp_path, "share_" + "2" * 24)
        revoked_id, _ = await live_share(store, conn, tmp_path)
        await store.mark_revoked(revoked_id, actor="op")
        old_revoked = time.time() - 7200
        os.utime(tmp_path / revoked_id, (old_revoked, old_revoked))
        # odd entries
        (tmp_path / "notes.txt").write_text("x")
        (tmp_path / "oddly named").mkdir()
        (tmp_path / "by-token" / "not-a-hash").mkdir()
        (tmp_path / "by-token" / "stray.txt").write_text("x")
        target = tmp_path / "outside"
        target.mkdir()
        (target / "keep").write_text("k")
        os.symlink(target, tmp_path / ("share_" + "3" * 24))
        os.symlink(target, tmp_path / "by-token" / ("ef" * 32))
        out = await share_housekeeping(store, tmp_path, tmp_path / "none.jsonl", time.time())
        assert out["orphans_removed"] == 3  # orphan token dir, unknown old bundle, old revoked bundle
        assert not (tmp_path / "by-token" / orphan_hash).exists()
        assert not unknown_old.exists() and not (tmp_path / revoked_id).exists()
        assert unknown_new.exists() and (tmp_path / "by-token" / recent_hash).exists()
        assert (tmp_path / "by-token" / th / "state.json").exists() and (tmp_path / sid).exists()
        assert (tmp_path / pending).exists()
        assert (target / "keep").exists()
        for odd in ("notes.txt", "oddly named", "share_" + "3" * 24):
            assert os.path.lexists(tmp_path / odd)
        assert (tmp_path / "by-token" / "not-a-hash").exists() and (tmp_path / "by-token" / "stray.txt").exists()
        assert os.path.islink(tmp_path / "by-token" / ("ef" * 32))
    run(share_postgres, body)


def test_housekeeper_rate_limits_and_swallows(monkeypatch, tmp_path):
    calls = []
    clock = [1000.0]

    async def fake(store, root, events, now, *, marker_path=None):
        calls.append(now)
        return {"expired": 0, "ingested": 0, "orphans_removed": 0}
    monkeypatch.setattr(worker, "share_housekeeping", fake)
    hk = ShareHousekeeper(tmp_path, tmp_path / "e", tmp_path / "m", clock=lambda: clock[0])

    async def go():
        assert (await hk.tick(object()))["expired"] == 0
        assert await hk.tick(object()) == "skipped"
        clock[0] += 61
        assert isinstance(await hk.tick(object()), dict)

        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(worker, "share_housekeeping", boom)
        clock[0] += 61
        assert await hk.tick(object()) == "error"
    asyncio.run(go())
    assert len(calls) == 2


def test_run_one_pass_wires_housekeeper():
    class HK:
        async def tick(self, store):
            return {"expired": 0, "ingested": 0, "orphans_removed": 0}

    class Conn:
        pass
    out = asyncio.run(worker.run_one_pass(Conn(), share_housekeeper=HK()))
    assert out["share_housekeeping"]["expired"] == 0
    assert "share_housekeeping" not in asyncio.run(worker.run_one_pass(Conn()))


def test_share_settings_and_housekeeper_disabled():
    from herdr_engineering.cli import _share_worker_settings
    off = _share_worker_settings({})
    assert off["enabled"] is False and off["shares_dir"] == "/shares"
    assert off["events_dir"] == "/share-events" and off["marker_dir"] == "/var/lib/herdr/share-markers"
    assert _share_worker_settings({"share": {"enabled": "yes"}})["enabled"] is False
    on = _share_worker_settings({"share": {"enabled": True, "shares_dir": "/a", "events_dir": "/b", "marker_dir": "/c"}})
    assert on == {"enabled": True, "shares_dir": "/a", "events_dir": "/b", "marker_dir": "/c"}
    with pytest.raises(SystemExit):
        _share_worker_settings({"share": "x"})
    with pytest.raises(SystemExit):
        _share_worker_settings({"share": {"enabled": True, "shares_dir": 5}})


def test_store_listing_methods(share_postgres):
    async def body(store, conn):
        aid, vid = await seed(conn)
        pending = await store.create(**kw(aid, vid))
        _, th = new_token()
        live = await store.create(**kw(aid, vid))
        await store.mark_live(live["share_id"], token_hash=th, index=index(), approved_by="op")
        assert th in await store.live_token_hashes()
        states = await store.known_share_states()
        assert states[pending["share_id"]] == "pending" and states[live["share_id"]] == "live"
        await store.mark_revoked(live["share_id"], actor="op")
        assert th not in await store.live_token_hashes()
        assert (await store.known_share_states())[live["share_id"]] == "revoked"
    run(share_postgres, body)
