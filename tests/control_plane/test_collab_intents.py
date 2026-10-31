# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""CollabIntentApplier core: registry, lanes, ledger replay and the generic steps 1-5 (Task 5a)."""
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane import collab_bridge_files as cbf
from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab_bridge_files import (
    Actor,
    BridgeLimits,
    new_ticket,
    request_digest,
)
from herdr_engineering.control_plane.collab_intents import _LOCK_SALT, CollabIntentApplier
from tests.control_plane.registry_pg import ROOT, FakeBlobs, registry_pg, uid  # noqa: F401 - imported fixture
from tests.control_plane.test_collab_artifacts import World, commit_args
from tests.control_plane.test_collab_tasks import W, svc  # noqa: F401 - imported fixture

SENTINEL = "SECRET-SENTINEL-7c1f0e"


class Clock:
    def __init__(self, t=None):
        self.t = time.time() if t is None else t

    def __call__(self):
        return self.t


class Mirror:
    def __init__(self, root):
        self.root = Path(root)


@pytest.fixture
def registry():
    saved = dict(cbf._INTENT_KINDS)
    yield
    cbf._INTENT_KINDS.clear()
    cbf._INTENT_KINDS.update(saved)


def validate_x(payload, limits):
    if not isinstance(payload, dict) or set(payload) - {"n", "text"}:
        raise cbf.IntentError("bad x payload")
    return dict(payload)


def register_x(kind="x.test", actors=("agent", "user")):
    cbf.register_intent_kind(kind, validate_x, actors=frozenset(actors))


class Env:
    def __init__(self, tmp_path, pool, clock, **kw):
        self.outbox = tmp_path / "outbox"
        (self.outbox / "intents").mkdir(parents=True)
        self.mirror_root = tmp_path / "mirror"
        self.clock = clock
        self.pool = pool
        self.mirror = kw.pop("mirror_cls", Mirror)(self.mirror_root)
        self.applier = CollabIntentApplier(self.outbox, self.mirror, rustfs=FakeBlobs(),
                                           limits=kw.pop("limits", BridgeLimits()), spool_dir=tmp_path / "spool",
                                           clock=clock, **kw)

    def feed(self, agents, *, age=0.0, **row):
        doc = {"generated_at": self.clock() - age, "agents": {
            a: {"owner": "owner@example.org", "status": status, "expires_at": self.clock() + 3600,
                "external": False, "join_id": None, "hard_expiry": None, **row} for a, status in agents.items()}}
        (self.outbox / "agents.json").write_text(json.dumps(doc))

    def intent(self, actor, *, kind="x.test", payload=None, key=None, artifact_id=None, age=0.0, ticket=None):
        raw = {"ticket": ticket or new_ticket(), "kind": kind, "actor": actor, "submitted_at": self.clock() - age,
               "payload": {"n": 1} if payload is None else payload}
        if key is not None:
            raw["idempotency_key"] = key
        if artifact_id is not None:
            raw["artifact_id"] = artifact_id
        (self.outbox / "intents" / f"{raw['ticket']}.json").write_text(json.dumps(raw))
        return raw

    def result(self, ticket):
        path = self.mirror_root / "results" / f"{ticket}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def pending(self, ticket):
        return (self.outbox / "intents" / f"{ticket}.json").exists()

    async def tick(self, wait=5.0, **kw):
        return await self.applier.tick(self.pool.acquire, wait=wait, **kw)


def agent(agent_id):
    return {"kind": "agent", "id": agent_id}


def run(pg, tmp_path, body, *, clock=None, pool_size=10, **kw):
    async def go():
        pool = await asyncpg.create_pool(pg["dsn"], min_size=1, max_size=pool_size)
        try:
            env = Env(tmp_path, pool, clock or Clock(), **kw)
            try:
                return await asyncio.wait_for(body(env), 60)
            finally:
                await env.applier.aclose(timeout=10)
        finally:
            try:
                await asyncio.wait_for(pool.close(), 10)
            except TimeoutError:
                pool.terminate()
    return asyncio.run(go())


async def ledger(env, ticket):
    async with env.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM artifact_collab_intents WHERE ticket_id=$1", ticket)
        return dict(row) if row else None


# ----------------------------------------------------------------------------- registry and actors

def test_user_kind_reaches_its_handler_with_no_agent_and_the_extras(registry_pg, tmp_path, registry):
    register_x(actors=("user",))
    seen = []

    async def handler(ctx, intent):
        seen.append((ctx.agent, dict(ctx.extras), ctx.actor))
        return {"result": "done"}

    async def body(env):
        env.applier.register("x.test", handler)
        raw = env.intent({"kind": "user", "id": "user_1"})
        counts = await env.tick()
        assert counts["applied"] == 1
        assert seen == [(None, {"share_store": "S"}, Actor("user", "user_1"))]
        doc = env.result(raw["ticket"])
        assert doc["actor"] == {"kind": "user", "id": "user_1"} and doc["result"] == "done"
        assert not env.pending(raw["ticket"])
    run(registry_pg, tmp_path, body, context_extras={"share_store": "S"})


def test_replay_by_key_uses_the_kinds_own_replay_outcomes(registry_pg, tmp_path, registry):
    register_x("x.test")
    register_x("x.other")
    calls = {"x.test": 0, "x.other": 0}

    async def handler(ctx, intent):
        calls[intent.kind] += 1
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler, replay_outcomes=frozenset({"done"}))
        env.applier.register("x.other", handler, replay_outcomes=frozenset({"other"}))
        for kind in ("x.test", "x.other"):
            env.intent(agent(a), kind=kind, key="k1")
            await env.tick()
            second = env.intent(agent(a), kind=kind, key="k1")
            await env.tick()
            assert env.result(second["ticket"])["result"] == "done"
        assert calls == {"x.test": 1, "x.other": 2}
    run(registry_pg, tmp_path, body)


def test_unregistered_kinds_are_rejected(registry_pg, tmp_path, registry):
    register_x("x.nohandler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        no_handler = env.intent(agent(a), kind="x.nohandler")
        unknown = env.intent(agent(a), kind="x.missing")
        counts = await env.tick()
        assert counts["rejected"] == 2
        assert env.result(no_handler["ticket"])["reason"] == "unknown intent kind"
        assert env.result(unknown["ticket"])["reason"] == "unknown intent kind"
        assert not env.pending(no_handler["ticket"]) and not env.pending(unknown["ticket"])
    run(registry_pg, tmp_path, body)


def test_a_user_actor_sending_commit_is_rejected(registry_pg, tmp_path, registry):
    async def handler(ctx, intent):  # pragma: no cover - must never run
        raise AssertionError("commit from a user reached a handler")

    async def body(env):
        env.applier.register("commit", handler)
        raw = env.intent({"kind": "user", "id": "user_1"}, kind="commit", artifact_id="art_1",
                         payload={"base_version_id": "v1", "changes": [{"name": "a.md", "op": "put", "text": "x"}]})
        await env.tick()
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "unknown intent kind"
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- lanes

def test_one_agents_intents_run_in_order_and_never_concurrently_while_another_agent_runs_alongside(
        registry_pg, tmp_path, registry):
    register_x()
    events, active = [], {}
    other_done = None

    async def handler(ctx, intent):
        lane = intent.actor.id
        active[lane] = active.get(lane, 0) + 1
        assert active[lane] == 1, "two intents of one lane ran concurrently"
        events.append(("start", intent.ticket))
        if intent.payload["n"] == 1:               # finishes only once the other agent's intent ran meanwhile
            await asyncio.wait_for(other_done.wait(), 5)
        elif intent.payload["n"] == 3:
            other_done.set()
        await asyncio.sleep(0.01)
        events.append(("end", intent.ticket))
        active[lane] -= 1
        return {"result": "done"}

    async def body(env):
        nonlocal other_done
        other_done = asyncio.Event()
        a, b = uid("agent"), uid("agent")
        env.feed({a: "active", b: "active"})
        env.applier.register("x.test", handler)
        a1 = env.intent(agent(a), payload={"n": 1}, age=2)
        a2 = env.intent(agent(a), payload={"n": 2}, age=1)
        b1 = env.intent(agent(b), payload={"n": 3})
        counts = await env.tick()
        assert counts["applied"] == 3
        order = [t for kind, t in events if kind == "start" and t in (a1["ticket"], a2["ticket"])]
        assert order == [a1["ticket"], a2["ticket"]]
        assert events.index(("end", a1["ticket"])) < events.index(("start", a2["ticket"]))
        assert events.index(("start", b1["ticket"])) < events.index(("end", a1["ticket"]))
    run(registry_pg, tmp_path, body)


def test_a_custom_lane_key_serialises_two_actors(registry_pg, tmp_path, registry):
    register_x()
    state = {"active": 0, "max": 0}

    async def handler(ctx, intent):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return {"result": "done"}

    async def body(env):
        a, b = uid("agent"), uid("agent")
        env.feed({a: "active", b: "active"})
        env.applier.register("x.test", handler, lane_key=lambda intent, row: "member:one")
        env.intent(agent(a))
        env.intent(agent(b))
        counts = await env.tick()
        assert counts["applied"] == 2 and state["max"] == 1
    run(registry_pg, tmp_path, body)


def test_a_lane_still_busy_from_the_previous_tick_is_skipped_and_others_apply(registry_pg, tmp_path, registry):
    register_x()
    release = None
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a, b = uid("agent"), uid("agent")
        env.feed({a: "active", b: "active"})
        env.applier.register("x.test", handler)
        a1 = env.intent(agent(a), payload={"n": 1})
        first = await env.tick(wait=0.05)
        assert first["applied"] == 0 and calls == [a1["ticket"]]
        a2 = env.intent(agent(a), payload={"n": 2})
        b1 = env.intent(agent(b), payload={"n": 3})
        second = await env.tick(wait=2.0)
        assert second["skipped"] == 1 and second["applied"] == 1
        assert env.result(b1["ticket"])["result"] == "done"
        assert env.pending(a2["ticket"]) and a2["ticket"] not in calls
        assert (await ledger(env, a2["ticket"]))["outcome"] == "received"   # received at scan time, not dispatched
        release.set()
        await env.applier.drain(timeout=5)
        await env.tick()
        assert env.result(a2["ticket"])["result"] == "done" and calls.count(a1["ticket"]) == 1
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- age and feed

def test_an_intent_older_than_60s_with_no_ledger_row_expires_uncharged(registry_pg, tmp_path, registry):
    register_x()

    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("expired intent reached its handler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a), age=61)
        await env.tick()
        assert env.result(raw["ticket"])["result"] == "expired"
        row = await ledger(env, raw["ticket"])
        assert row["outcome"] == "expired" and row["bytes"] == 0
    run(registry_pg, tmp_path, body)


def test_an_intent_queued_behind_a_slow_handler_of_its_lane_is_applied_not_expired(registry_pg, tmp_path, registry):
    register_x()
    release = None
    clock = Clock()

    async def handler(ctx, intent):
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2})
        await env.tick(wait=0.05)
        assert (await ledger(env, queued["ticket"]))["outcome"] == "received"
        clock.t += 120                                     # the slow assembly runs for two minutes
        env.feed({a: "active"})                            # collab-public keeps the feed fresh meanwhile
        await env.tick(wait=0.05)
        release.set()
        await env.applier.drain(timeout=5)
        assert env.result(queued["ticket"])["result"] == "done"
    run(registry_pg, tmp_path, body, clock=clock)


@pytest.mark.parametrize("case", ["missing", "revoked", "stale"])
def test_agent_feed_gates_agent_intents(registry_pg, tmp_path, registry, case):
    register_x()

    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("refused agent reached its handler")

    async def body(env):
        a = uid("agent")
        if case == "missing":
            env.feed({uid("agent"): "active"})
        elif case == "revoked":
            env.feed({a: "revoked"})
        else:
            env.feed({a: "active"}, age=121)
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a))
        await env.tick()
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected"
        assert doc["reason"] == ("agent feed stale" if case == "stale" else "agent not active")
        assert (await ledger(env, raw["ticket"]))["outcome"] == "rejected"
    run(registry_pg, tmp_path, body)


def test_a_missing_feed_fails_closed(registry_pg, tmp_path, registry):
    register_x()

    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("no feed, yet the handler ran")

    async def body(env):
        env.applier.register("x.test", handler)
        raw = env.intent(agent(uid("agent")))
        await env.tick()
        assert env.result(raw["ticket"])["reason"] == "agent feed stale"
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- crash and retry

async def _record(env, raw, *, key=None):
    async with env.pool.acquire() as conn:
        await CollabGrantStore(conn).record_received(
            raw["ticket"], raw["kind"], Actor(raw["actor"]["kind"], raw["actor"]["id"]), None, None,
            key or raw["ticket"], request_digest(raw), 0)


def test_a_received_intent_older_than_60s_is_still_applied(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a), age=300)
        await _record(env, raw)                           # received before the crash
        await env.tick()
        assert calls == [raw["ticket"]] and env.result(raw["ticket"])["result"] == "done"
    run(registry_pg, tmp_path, body)


def test_a_received_row_does_not_exempt_a_rewritten_intent(registry_pg, tmp_path, registry):
    register_x()

    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("rewritten intent reached its handler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a), age=300)
        await _record(env, raw)
        env.intent(agent(a), age=300, payload={"n": 2}, ticket=raw["ticket"])   # new content, same ticket
        await env.tick()
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "intent changed after it was received"
    run(registry_pg, tmp_path, body)


def test_rerunning_after_a_crash_before_unlink_gives_the_same_result_without_a_second_call(
        registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        return {"result": "done", "version_id": "ver_1", "version_number": 7}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a))
        consume = env.applier._consume
        env.applier._consume = lambda name: None          # the process dies right before the unlink
        await env.tick()
        first = env.result(raw["ticket"])
        assert env.pending(raw["ticket"])
        env.applier._consume = consume
        await env.tick()
        assert calls == [raw["ticket"]]
        assert env.result(raw["ticket"]) == first and first["version_number"] == 7
        assert not env.pending(raw["ticket"])
        (env.mirror_root / "results" / f"{raw['ticket']}.json").unlink()   # result lost too: rebuilt from the row
        env.intent(agent(a), ticket=raw["ticket"])
        await env.tick()
        rebuilt = env.result(raw["ticket"])
        assert rebuilt["result"] == "done" and rebuilt["version_id"] == "ver_1" and calls == [raw["ticket"]]
    run(registry_pg, tmp_path, body)


def test_two_tickets_with_one_key_and_one_request_call_the_handler_once(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        await asyncio.sleep(0.05)
        return {"result": "done", "version_id": "ver_k"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler, replay_outcomes=frozenset({"done"}))
        one = env.intent(agent(a), key="same-key", age=1)
        two = env.intent(agent(a), key="same-key")      # same tick: only the in-lane recheck can catch it
        await env.tick()
        assert len(calls) == 1
        for raw in (one, two):
            doc = env.result(raw["ticket"])
            assert doc["result"] == "done" and doc["version_id"] == "ver_k"
    run(registry_pg, tmp_path, body)


def test_a_reused_key_with_a_different_request_is_rejected_and_the_first_result_is_kept(
        registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        return {"result": "done", "version_id": "ver_1"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler, replay_outcomes=frozenset({"done"}))
        one = env.intent(agent(a), key="k", payload={"n": 1})
        await env.tick()
        first = env.result(one["ticket"])
        two = env.intent(agent(a), key="k", payload={"n": 2})
        await env.tick()
        doc = env.result(two["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "idempotency key was reused for a different request"
        assert env.result(one["ticket"]) == first and calls == [one["ticket"]]
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- forged input and hostile tree

_CHILD = r"""
import asyncio, json, sys
from pathlib import Path
import asyncpg
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits
from herdr_engineering.control_plane.collab_intents import CollabIntentApplier

class Mirror:
    root = Path(sys.argv[3])

async def main():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=4)
    try:
        applier = CollabIntentApplier(Path(sys.argv[2]), Mirror(), rustfs=None, limits=BridgeLimits(),
                                      spool_dir=Path(sys.argv[2]))
        counts = await applier.tick(pool.acquire)
        await applier.drain()
    finally:
        await pool.close()
    print(json.dumps({"counts": counts,
                      "share_api": "herdr_engineering.control_plane.artifact_share_api" in sys.modules}))

asyncio.run(main())
"""


def test_share_create_is_rejected_without_importing_the_share_module(registry_pg, tmp_path):
    async def shares():
        conn = await asyncpg.connect(registry_pg["dsn"])
        try:
            return await conn.fetchval("SELECT count(*) FROM artifact_shares")
        finally:
            await conn.close()

    outbox, mirror = tmp_path / "outbox", tmp_path / "mirror"
    (outbox / "intents").mkdir(parents=True)
    a = uid("agent")
    (outbox / "agents.json").write_text(json.dumps({"generated_at": time.time(), "agents": {
        a: {"status": "active", "expires_at": time.time() + 3600}}}))
    ticket = new_ticket()
    (outbox / "intents" / f"{ticket}.json").write_text(json.dumps({
        "ticket": ticket, "kind": "share_create", "actor": agent(a), "artifact_id": "art_1",
        "submitted_at": time.time(), "payload": {"permission": "view"}}))
    before = asyncio.run(shares())
    proc = subprocess.run([sys.executable, "-c", _CHILD, registry_pg["dsn"], str(outbox), str(mirror)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"counts": {"applied": 0, "conflicts": 0, "rejected": 1, "skipped": 0}, "share_api": False}
    doc = json.loads((mirror / "results" / f"{ticket}.json").read_text())
    assert doc["result"] == "rejected" and doc["reason"] == "unknown intent kind"
    assert asyncio.run(shares()) == before


def test_a_symlinked_intent_is_skipped_and_its_target_is_never_unlinked(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        outside = tmp_path / "outside"
        outside.mkdir()
        ticket = new_ticket()
        target = outside / f"{ticket}.json"
        target.write_text(json.dumps({"ticket": ticket, "kind": "x.test", "actor": agent(a),
                                      "submitted_at": env.clock(), "payload": {"n": 1}}))
        link = env.outbox / "intents" / f"{ticket}.json"
        link.symlink_to(target)
        await env.tick()
        assert calls == [] and env.result(ticket) is None
        assert target.exists() and link.is_symlink()
        assert await ledger(env, ticket) is None
    run(registry_pg, tmp_path, body)


def test_a_symlinked_intents_dir_is_never_read_or_unlinked(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        outside = tmp_path / "outside"
        outside.mkdir()
        ticket = new_ticket()
        planted = outside / f"{ticket}.json"
        planted.write_text(json.dumps({"ticket": ticket, "kind": "x.test", "actor": agent(a),
                                       "submitted_at": env.clock(), "payload": {"n": 1}}))
        (env.outbox / "intents").rmdir()
        (env.outbox / "intents").symlink_to(outside, target_is_directory=True)
        counts = await env.tick()
        assert counts == {"applied": 0, "conflicts": 0, "rejected": 0, "skipped": 0}
        assert calls == [] and planted.exists() and env.result(ticket) is None
    run(registry_pg, tmp_path, body)


def test_an_unreadable_intent_is_rejected_without_echo(registry_pg, tmp_path, registry):
    async def body(env):
        ticket = new_ticket()
        (env.outbox / "intents" / f"{ticket}.json").write_text("{not json " + SENTINEL)
        await env.tick()
        doc = env.result(ticket)
        assert doc == {"ticket": ticket, "kind": None, "actor": None, "artifact_id": None, "result": "rejected",
                       "reason": "intent is not readable", "decided_at": doc["decided_at"]}
        assert not env.pending(ticket)
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- results

def test_sweep_results_deletes_old_result_files_and_keeps_new_ones(registry_pg, tmp_path):
    async def body(env):
        results = env.mirror_root / "results"
        results.mkdir(parents=True)
        old, new = results / f"{new_ticket()}.json", results / f"{new_ticket()}.json"
        for path in (old, new):
            path.write_text("{}")
        os.utime(old, (env.clock() - 7200, env.clock() - 7200))
        os.utime(new, (env.clock() - 60, env.clock() - 60))
        assert await env.applier.sweep_results(max_age=3600) == 1
        assert not old.exists() and new.exists()
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- no secrets in logs

def test_an_invalid_intent_with_secret_text_leaves_no_trace(registry_pg, tmp_path, registry, caplog):
    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("invalid commit reached its handler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("commit", handler)
        raw = env.intent(agent(a), kind="commit", artifact_id="art_1", key=None, payload={
            "base_version_id": "v1", "message": "ok",
            "changes": [{"name": "a.md", "op": "put", "text": SENTINEL + "\x01"}]})
        await env.tick()
        path = env.mirror_root / "results" / f"{raw['ticket']}.json"
        doc = json.loads(path.read_text())
        assert doc["result"] == "rejected" and doc["reason"] == "change text contains a control character"
        assert SENTINEL not in path.read_text()

    caplog.set_level(logging.DEBUG)
    run(registry_pg, tmp_path, body)
    assert caplog.records and SENTINEL not in caplog.text


# ----------------------------------------------------------------------------- review round 1

class Dedicated:
    """``scan_acquire`` over one connection outside the lanes' pool."""

    def __init__(self, conn):
        self.conn = conn

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def commit_payload(text="hello"):
    return {"base_version_id": "ver_1", "changes": [{"name": "a.md", "op": "put", "text": text}]}


@pytest.mark.parametrize("budget", [1, 2])
def test_received_intents_of_a_busy_lane_never_starve_another_agents_fresh_intent(registry_pg, tmp_path, registry,
                                                                                  budget):
    register_x()
    release = None

    async def handler(ctx, intent):
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a, b = uid("agent"), uid("agent")
        env.feed({a: "active", b: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), payload={"n": 1})
        await env.tick(wait=0.05)                          # A's lane is now blocked
        queued = [env.intent(agent(a), payload={"n": 2}), env.intent(agent(a), payload={"n": 2})]
        for i, raw in enumerate(queued):
            os.utime(env.outbox / "intents" / f"{raw['ticket']}.json", (env.clock() - 50 + i, env.clock() - 50 + i))
        for _ in range(-(-len(queued) // budget)):       # the queued intents take the budget once, to be received
            await env.tick(wait=0.05)
        for raw in queued:
            assert (await ledger(env, raw["ticket"]))["outcome"] == "received"
        fresh = env.intent(agent(b), payload={"n": 3})   # newest mtime: last in an mtime-ordered budget
        await env.tick(wait=0.05)
        assert await ledger(env, fresh["ticket"]) is not None
        release.set()
    run(registry_pg, tmp_path, body, max_per_tick=budget)


def test_the_scan_has_its_own_connection_when_every_lane_holds_one(registry_pg, tmp_path, registry):
    register_x()
    release = None

    async def handler(ctx, intent):
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a, b = uid("agent"), uid("agent")
        env.feed({a: "active", b: "active"})
        env.applier.register("x.test", handler)
        scan = await asyncpg.connect(registry_pg["dsn"])
        try:
            env.intent(agent(a), payload={"n": 1})
            await env.tick(wait=0.05, scan_acquire=Dedicated(scan))      # the lane takes the pool's only slot
            fresh = env.intent(agent(b), payload={"n": 3})
            await asyncio.wait_for(env.tick(wait=0, scan_acquire=Dedicated(scan)), 1.0)
            row = await scan.fetchrow("SELECT outcome FROM artifact_collab_intents WHERE ticket_id=$1",
                                      fresh["ticket"])
            assert row is not None and row["outcome"] == "received"
            release.set()
            await env.applier.drain(timeout=5)
        finally:
            await scan.close()
    run(registry_pg, tmp_path, body, pool_size=1, limits=BridgeLimits(apply_concurrency=1))


def test_a_ticket_replayed_in_the_lane_is_not_charged_twice(registry_pg, tmp_path, registry):
    async def handler(ctx, intent):
        await asyncio.sleep(0.05)
        return {"result": "created", "version_id": "ver_2"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("commit", handler, replay_outcomes=frozenset({"created"}))
        art = uid("art")
        one = env.intent(agent(a), kind="commit", artifact_id=art, key="ck", payload=commit_payload(), age=1)
        two = env.intent(agent(a), kind="commit", artifact_id=art, key="ck", payload=commit_payload())
        await env.tick()
        assert env.result(two["ticket"])["result"] == "created"
        async with env.pool.acquire() as conn:
            assert await CollabGrantStore(conn).bytes_last_24h(a) == len("hello")
        assert (await ledger(env, two["ticket"]))["bytes"] == 0 and (await ledger(env, one["ticket"]))["bytes"] == 5
    run(registry_pg, tmp_path, body)


def test_an_agent_revoked_while_queued_behind_its_lane_is_rejected(registry_pg, tmp_path, registry):
    register_x()
    release = None
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.payload["n"])
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2})
        await env.tick(wait=0.05)
        env.feed({a: "revoked"})
        await env.tick(wait=0.05)                          # loads the new feed; the lane is still busy
        release.set()
        await env.applier.drain(timeout=5)
        doc = env.result(queued["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "agent not active" and calls == [1]
    run(registry_pg, tmp_path, body)


def test_a_raising_handler_is_an_internal_error_with_no_echo(registry_pg, tmp_path, registry, caplog):
    register_x()

    async def handler(ctx, intent):
        raise RuntimeError(SENTINEL)

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a))
        await env.tick()
        text = (env.mirror_root / "results" / f"{raw['ticket']}.json").read_text()
        assert json.loads(text)["reason"] == "internal error" and SENTINEL not in text

    caplog.set_level(logging.DEBUG)
    run(registry_pg, tmp_path, body)
    assert SENTINEL not in caplog.text


def test_an_invalid_actor_is_never_echoed(registry_pg, tmp_path, registry, caplog):
    register_x()

    async def body(env):
        raw = env.intent({"kind": "agent", "id": "../" + SENTINEL})
        await env.tick()
        text = (env.mirror_root / "results" / f"{raw['ticket']}.json").read_text()
        doc = json.loads(text)
        assert doc["actor"] is None and doc["reason"] == "invalid actor" and SENTINEL not in text

    caplog.set_level(logging.DEBUG)
    run(registry_pg, tmp_path, body)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("field", ["expires_at", "hard_expiry"])
def test_an_expired_agent_is_rejected(registry_pg, tmp_path, registry, field):
    register_x()

    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("expired agent reached its handler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"}, **{field: env.clock() - 1})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a))
        await env.tick()
        assert env.result(raw["ticket"])["reason"] == "agent not active"
    run(registry_pg, tmp_path, body)


def test_lane_order_holds_when_the_first_intents_key_is_locked_elsewhere(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.idempotency_key)
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), key="k1", age=1)
        env.intent(agent(a), key="k2")
        other = await asyncpg.connect(registry_pg["dsn"])
        try:
            lock = _LOCK_SALT + f"key:agent:{a}:x.test:k1"
            assert await other.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock)
            counts = await env.tick()
            assert calls == [] and counts["skipped"] == 2
            await other.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock)
        finally:
            await other.close()
        await env.tick()
        assert calls == ["k1", "k2"]
    run(registry_pg, tmp_path, body)


@pytest.mark.parametrize("out", [
    {"result": "received"}, {"result": "done", "reason": 7}, {"result": "done", "version_id": ["x"]},
    {"result": "done", "conflicts": "nope"}, {"result": "done", "version_number": "1"},
    {"result": "done", "merged_names": [1]}, {"result": "done", "attempt_id": 3}])
def test_an_invalid_handler_return_is_an_internal_error_in_ledger_and_result(registry_pg, tmp_path, registry, out):
    register_x()

    async def handler(ctx, intent):
        return out

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a))
        await env.tick()
        doc, row = env.result(raw["ticket"]), await ledger(env, raw["ticket"])
        assert (doc["result"], doc["reason"]) == ("rejected", "internal error")
        assert (row["outcome"], row["reason"], row["version_id"]) == ("rejected", "internal error", None)
    run(registry_pg, tmp_path, body)


def test_an_expired_commit_records_no_bytes(registry_pg, tmp_path, registry):
    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("expired commit reached its handler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("commit", handler)
        raw = env.intent(agent(a), kind="commit", artifact_id=uid("art"), payload=commit_payload(), age=61)
        await env.tick()
        row = await ledger(env, raw["ticket"])
        assert row["outcome"] == "expired" and row["bytes"] == 0
    run(registry_pg, tmp_path, body)


def test_a_future_submitted_at_is_rejected(registry_pg, tmp_path, registry):
    register_x()

    async def handler(ctx, intent):  # pragma: no cover
        raise AssertionError("future intent reached its handler")

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a), age=-3600)
        await env.tick()
        assert env.result(raw["ticket"])["reason"] == "invalid submitted_at"
    run(registry_pg, tmp_path, body)


def test_sweep_results_deletes_in_bounded_batches_past_any_cap(registry_pg, tmp_path, monkeypatch):
    from herdr_engineering.control_plane import collab_intents as ci
    monkeypatch.setattr(ci, "RESULTS_SWEEP_BATCH", 3)

    async def body(env):
        results = env.mirror_root / "results"
        results.mkdir(parents=True)
        for _ in range(7):
            path = results / f"{new_ticket()}.json"
            path.write_text("{}")
            os.utime(path, (env.clock() - 7200, env.clock() - 7200))
        assert await env.applier.sweep_results(max_age=3600) == 3
        assert await env.applier.sweep_results(max_age=3600) == 3
        assert await env.applier.sweep_results(max_age=3600) == 1
        assert list(results.iterdir()) == []
    run(registry_pg, tmp_path, body)


def test_stale_stray_files_in_intents_are_removed(registry_pg, tmp_path, registry):
    async def body(env):
        intents = env.outbox / "intents"
        stale, young = intents / ".tmp-abc", intents / ".tmp-def"
        stale.write_text("x")
        young.write_text("x")
        os.utime(stale, (env.clock() - 7200, env.clock() - 7200))
        outside = tmp_path / "outside"
        outside.write_text("keep")
        (intents / "junk-link").symlink_to(outside)
        os.utime(intents / "junk-link", (env.clock() - 7200, env.clock() - 7200), follow_symlinks=False)
        await env.tick()
        assert not stale.exists() and young.exists() and outside.read_text() == "keep"
        assert (intents / "junk-link").is_symlink()
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- review round 2

def test_a_fresh_intent_joins_its_lane_behind_older_received_ones(registry_pg, tmp_path, registry):
    register_x()
    release = None
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.payload["n"])
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), payload={"n": 1}, age=40)
        await env.tick(wait=0.05)
        for i in range(2):
            raw = env.intent(agent(a), payload={"n": 10 + i}, age=30 - i)
            os.utime(env.outbox / "intents" / f"{raw['ticket']}.json", (env.clock() - 30 + i,) * 2)
        await env.tick(wait=0.05)                          # 10 and 11 are received behind the busy lane
        release.set()
        await env.applier.drain(timeout=5)
        env.intent(agent(a), payload={"n": 99})            # fresh: must run after 10 and 11
        for _ in range(4):
            await env.tick()
        assert calls == [1, 10, 11, 99]
    run(registry_pg, tmp_path, body, max_per_tick=2)


def test_stale_strays_cannot_wedge_an_over_cap_intents_dir(registry_pg, tmp_path, registry):
    from herdr_engineering.control_plane import collab_intents as ci
    register_x()

    async def handler(ctx, intent):
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        intents = env.outbox / "intents"
        old = env.clock() - 7200
        for i in range(ci.INTENTS_MIN_ENTRIES + 1):
            path = intents / f".tmp-{i:05d}"
            path.write_text("")
            os.utime(path, (old, old))
        raw = env.intent(agent(a))
        for _ in range(3):
            await env.tick()
            if env.result(raw["ticket"]) is not None:
                break
        assert env.result(raw["ticket"])["result"] == "done"
        assert sorted(p.name for p in intents.iterdir()) == []
    run(registry_pg, tmp_path, body)


@pytest.mark.parametrize("ahead,rejected", [(60.0, False), (60.01, True)])
def test_the_future_skew_boundary(registry_pg, tmp_path, registry, ahead, rejected):
    register_x()

    async def handler(ctx, intent):
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a), age=-ahead)
        await env.tick()
        doc = env.result(raw["ticket"])
        assert (doc.get("reason") == "invalid submitted_at") is rejected
        assert doc["result"] == ("rejected" if rejected else "done")
    run(registry_pg, tmp_path, body, clock=Clock(1_000_000_000.0))


def test_a_nan_inside_conflicts_is_an_internal_error_in_ledger_and_result(registry_pg, tmp_path, registry):
    register_x()

    async def handler(ctx, intent):
        return {"result": "conflict", "conflicts": [{"name": "a.md", "kind": "x", "score": float("nan")}]}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a))
        await env.tick()
        doc, row = env.result(raw["ticket"]), await ledger(env, raw["ticket"])
        assert (doc["result"], doc["reason"]) == ("rejected", "internal error")
        assert (row["outcome"], row["reason"]) == ("rejected", "internal error")
    run(registry_pg, tmp_path, body)


def test_lane_and_key_locks_are_released_after_the_lane_finishes(registry_pg, tmp_path, registry):
    register_x()

    async def handler(ctx, intent):
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), key="k1")
        # a pool's release runs pg_advisory_unlock_all(); a plain connection does not, so the applier must unlock
        lane, other = await asyncpg.connect(registry_pg["dsn"]), await asyncpg.connect(registry_pg["dsn"])
        try:
            await env.applier.tick(Dedicated(lane), scan_acquire=Dedicated(other), wait=5)
            await env.applier.drain(timeout=5)
            assert env.result(env_ticket(env))["result"] == "done"
            for lock in (_LOCK_SALT + f"lane:agent:{a}", _LOCK_SALT + f"key:agent:{a}:x.test:k1"):
                assert await other.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock)
                await other.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock)
        finally:
            await lane.close()
            await other.close()
    run(registry_pg, tmp_path, body)


def env_ticket(env):
    return next((env.mirror_root / "results").iterdir()).name[:-5]


def test_a_received_intent_whose_file_vanished_is_forgotten(registry_pg, tmp_path, registry):
    register_x()
    release = None
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.payload["n"])
        if intent.payload["n"] == 1:
            await release.wait()
        return {"result": "done"}

    async def body(env):
        nonlocal release
        release = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.intent(agent(a), payload={"n": 1}, age=1)
        await env.tick(wait=0.05)
        gone = env.intent(agent(a), payload={"n": 2})
        await env.tick(wait=0.05)                          # received behind the busy lane
        (env.outbox / "intents" / f"{gone['ticket']}.json").unlink()
        await env.tick(wait=0.05)
        release.set()
        await env.applier.drain(timeout=5)
        await env.tick()
        assert calls == [1] and gone["ticket"] not in env.applier._received
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- review round 3

def _blocked_lane(release_holder, calls):
    async def handler(ctx, intent):
        calls.append(intent.payload.get("n"))
        if intent.payload.get("n") == 1:
            await release_holder["event"].wait()
        return {"result": "done"}
    return handler


def test_a_received_ticket_is_remembered_without_its_payload_and_the_set_is_capped(
        registry_pg, tmp_path, registry, monkeypatch):
    from herdr_engineering.control_plane import collab_intents as ci
    monkeypatch.setattr(ci, "RECEIVED_MIN_CAP", 3)
    monkeypatch.setattr(ci, "RECEIVED_PER_AGENT", 1)
    register_x()
    hold, calls = {}, []

    async def body(env):
        hold["event"] = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _blocked_lane(hold, calls))
        env.intent(agent(a), payload={"n": 1}, age=10)
        await env.tick(wait=0.05)
        big = "y" * 200_000
        queued = [env.intent(agent(a), payload={"n": 2 + i, "text": big}, age=9 - i) for i in range(4)]
        await env.tick(wait=0.05)
        assert len(env.applier._received) == 3                             # cap: A1 + two queued
        received = [r for r in queued if await ledger(env, r["ticket"]) is not None]
        assert len(received) == 2
        for rec in env.applier._received.values():
            assert not hasattr(rec, "payload") and not hasattr(rec, "intent")
            assert all(len(v) < 300 for v in vars(rec).values() if isinstance(v, str))
        hold["event"].set()
        await env.applier.drain(timeout=5)
        for _ in range(4):
            await env.tick()
        assert sorted(calls) == [1, 2, 3, 4, 5]
    run(registry_pg, tmp_path, body)


def test_an_intent_rewritten_after_it_was_received_is_rejected_in_the_lane(registry_pg, tmp_path, registry):
    register_x()
    hold, calls = {}, []

    async def body(env):
        hold["event"] = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _blocked_lane(hold, calls))
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2})
        await env.tick(wait=0.05)
        env.intent(agent(a), payload={"n": 7}, ticket=queued["ticket"])     # rewritten while queued
        hold["event"].set()
        await env.applier.drain(timeout=5)
        doc = env.result(queued["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "intent changed after it was received"
        assert calls == [1]
    run(registry_pg, tmp_path, body)


def test_a_restart_picks_up_received_rows(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.payload["n"])
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        raw = env.intent(agent(a), age=300)
        await _record(env, raw)                            # received by the previous process
        fresh = CollabIntentApplier(env.outbox, Mirror(env.mirror_root), rustfs=FakeBlobs(), limits=BridgeLimits(),
                                    spool_dir=tmp_path / "spool", clock=env.clock)
        fresh.register("x.test", handler)
        await fresh.tick(env.pool.acquire, wait=5)
        await fresh.drain(timeout=5)
        assert calls == [1] and env.result(raw["ticket"])["result"] == "done"
    run(registry_pg, tmp_path, body)


def test_a_stale_received_ticket_file_is_applied_not_swept(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def handler(ctx, intent):
        calls.append(intent.ticket)
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        raw = env.intent(agent(a), age=300)
        await _record(env, raw)
        old = env.clock() - 7200
        os.utime(env.outbox / "intents" / f"{raw['ticket']}.json", (old, old))
        await env.tick()
        assert calls == [raw["ticket"]] and env.result(raw["ticket"])["result"] == "done"
    run(registry_pg, tmp_path, body)


def test_a_received_row_that_vanished_is_forgotten_and_rescanned_once(registry_pg, tmp_path, registry):
    register_x()
    hold, calls = {}, []

    async def body(env):
        hold["event"] = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _blocked_lane(hold, calls))
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2})
        await env.tick(wait=0.05)
        async with env.pool.acquire() as conn:
            await conn.execute("DELETE FROM artifact_collab_intents WHERE ticket_id=$1", queued["ticket"])
        hold["event"].set()
        await env.applier.drain(timeout=5)
        for _ in range(3):
            await env.tick()
        assert calls == [1, 2] and env.result(queued["ticket"])["result"] == "done"
    run(registry_pg, tmp_path, body)


def test_an_intent_withdrawn_after_its_lane_started_is_forgotten(registry_pg, tmp_path, registry):
    register_x()
    hold, calls = {}, []

    async def body(env):
        hold["event"] = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _blocked_lane(hold, calls))
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2})
        await env.tick(wait=0.05)                          # both in one running lane
        (env.outbox / "intents" / f"{queued['ticket']}.json").unlink()
        hold["event"].set()
        await env.applier.drain(timeout=5)
        assert calls == [1] and env.result(queued["ticket"]) is None
        assert queued["ticket"] not in env.applier._received
        assert (await ledger(env, queued["ticket"]))["outcome"] == "received"
    run(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- review round 4

def _keyed(calls):
    async def handler(ctx, intent):
        calls.append((intent.payload.get("n"), intent.idempotency_key))
        if intent.payload.get("n") == 1:
            await _keyed.hold.wait()
        return {"result": "done"}
    return handler


def test_a_key_only_rewrite_of_a_queued_ticket_is_rejected_in_the_lane_and_never_applied_under_the_new_key(
        registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def body(env):
        _keyed.hold = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _keyed(calls), replay_outcomes=frozenset({"done"}))
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2}, key="k-one")
        await env.tick(wait=0.05)                          # queued is received under k-one
        rewritten = dict(queued, idempotency_key="k-two")  # same body, digest unchanged: only the key moved
        (env.outbox / "intents" / f"{queued['ticket']}.json").write_text(json.dumps(rewritten))
        _keyed.hold.set()
        await env.applier.drain(timeout=5)
        doc = env.result(queued["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "intent changed after it was received"
        assert (await ledger(env, queued["ticket"]))["idempotency_key"] == "k-one"
        later = env.intent(agent(a), payload={"n": 2}, key="k-two")      # k-two was never applied: runs once
        await env.tick()
        assert env.result(later["ticket"])["result"] == "done"
        assert calls == [(1, None), (2, "k-two")]
    run(registry_pg, tmp_path, body)


def test_a_submitted_at_only_rewrite_of_a_queued_ticket_is_rejected_in_the_lane(registry_pg, tmp_path, registry):
    register_x()
    calls = []

    async def body(env):
        _keyed.hold = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _keyed(calls))
        env.intent(agent(a), payload={"n": 1}, age=40)
        queued = env.intent(agent(a), payload={"n": 2}, age=30)
        await env.tick(wait=0.05)
        rewritten = dict(queued, submitted_at=env.clock())   # younger: would change its place in the lane
        (env.outbox / "intents" / f"{queued['ticket']}.json").write_text(json.dumps(rewritten))
        _keyed.hold.set()
        await env.applier.drain(timeout=5)
        doc = env.result(queued["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "intent changed after it was received"
        assert calls == [(1, None)]
    run(registry_pg, tmp_path, body)


@pytest.mark.parametrize("field,value", [("idempotency_key", "k-two"), ("kind", "x.other"),
                                         ("actor", {"kind": "user", "id": "user_9"})])
def test_a_received_row_rediscovered_after_a_restart_rejects_a_file_whose_key_kind_or_actor_moved(
        registry_pg, tmp_path, registry, field, value):
    register_x()
    register_x("x.other")
    calls = []

    async def handler(ctx, intent):  # pragma: no cover - must never run
        calls.append(intent.ticket)
        return {"result": "done"}

    async def body(env):
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", handler)
        env.applier.register("x.other", handler)
        raw = env.intent(agent(a), key="k-one", age=300)
        await _record(env, raw, key="k-one")               # received by the previous process under k-one
        rewritten = dict(raw, **{field: value})
        if field != "idempotency_key":                     # kind/actor are in the digest: keep the row's digest
            async with env.pool.acquire() as conn:
                await conn.execute("UPDATE artifact_collab_intents SET request_sha256=$2 WHERE ticket_id=$1",
                                   raw["ticket"], request_digest(rewritten))
        (env.outbox / "intents" / f"{raw['ticket']}.json").write_text(json.dumps(rewritten))
        await env.tick()
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "intent changed after it was received"
        assert calls == [] and (await ledger(env, raw["ticket"]))["outcome"] == "rejected"
    run(registry_pg, tmp_path, body)


def test_a_file_unlinked_between_the_lanes_exists_check_and_its_read_is_forgotten_not_rejected(
        registry_pg, tmp_path, registry, monkeypatch):
    from herdr_engineering.control_plane import collab_intents as ci
    register_x()
    calls = []
    real_exists = ci.file_exists_beneath
    target = {}

    def racing_exists(root, parts, name):
        present = real_exists(root, parts, name)
        if present and name == target.get("name"):
            (Path(root) / "intents" / name).unlink()       # withdrawn right after the check
        return present

    monkeypatch.setattr(ci, "file_exists_beneath", racing_exists)

    async def body(env):
        _keyed.hold = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _keyed(calls))
        env.intent(agent(a), payload={"n": 1}, age=1)
        queued = env.intent(agent(a), payload={"n": 2})
        target["name"] = f"{queued['ticket']}.json"
        await env.tick(wait=0.05)
        _keyed.hold.set()
        await env.applier.drain(timeout=5)
        assert calls == [(1, None)] and env.result(queued["ticket"]) is None
        assert queued["ticket"] not in env.applier._received
        assert (await ledger(env, queued["ticket"]))["outcome"] == "received"
    run(registry_pg, tmp_path, body)


def test_expired_and_future_tickets_are_decided_while_the_received_set_is_full(registry_pg, tmp_path, registry,
                                                                                monkeypatch):
    from herdr_engineering.control_plane import collab_intents as ci
    monkeypatch.setattr(ci, "RECEIVED_PER_AGENT", 1)
    register_x()
    calls = []

    async def body(env):
        _keyed.hold = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active"})
        env.applier.register("x.test", _keyed(calls))
        env.intent(agent(a), payload={"n": 1}, age=10)
        queued = env.intent(agent(a), payload={"n": 2}, age=9)
        await env.tick(wait=0.05)
        assert len(env.applier._received) == 2             # the cap is full
        expired = env.intent(agent(a), payload={"n": 3}, age=61)
        future = env.intent(agent(a), payload={"n": 4}, age=-3600)
        held = env.intent(agent(a), payload={"n": 5})
        counts = await env.tick(wait=0.05)
        assert counts["rejected"] == 2
        assert env.result(expired["ticket"])["result"] == "expired"
        assert env.result(future["ticket"])["reason"] == "invalid submitted_at"
        assert env.pending(held["ticket"]) and await ledger(env, held["ticket"]) is None   # no room: not received
        assert (await ledger(env, queued["ticket"]))["outcome"] == "received"
        _keyed.hold.set()
        await env.applier.drain(timeout=5)
        for _ in range(3):
            await env.tick()
        assert sorted(calls) == [(1, None), (2, None), (5, None)]
    run(registry_pg, tmp_path, body, received_min_cap=2)


def test_the_received_cap_scales_with_the_number_of_agents_in_the_feed(registry_pg, tmp_path, registry,
                                                                        monkeypatch):
    from herdr_engineering.control_plane import collab_intents as ci
    monkeypatch.setattr(ci, "RECEIVED_PER_AGENT", 2)
    register_x()
    calls = []

    async def body(env):
        _keyed.hold = asyncio.Event()
        a = uid("agent")
        env.feed({a: "active", **{uid("agent"): "active" for _ in range(3)}})   # 4 agents x 2 > base 3
        env.applier.register("x.test", _keyed(calls))
        env.intent(agent(a), payload={"n": 1}, age=10)
        await env.tick(wait=0.05)
        assert env.applier._received_cap == 8
        queued = [env.intent(agent(a), payload={"n": 2 + i}, age=9 - i) for i in range(9)]
        await env.tick(wait=0.05)
        assert len(env.applier._received) == 8             # 1 running + 7 queued
        assert len([r for r in queued if await ledger(env, r["ticket"]) is not None]) == 7
        env.feed({a: "active"})                            # one agent: back to the base
        await env.tick(wait=0.05)
        assert env.applier._received_cap == 3
        _keyed.hold.set()
        await env.applier.drain(timeout=5)
        for _ in range(6):
            await env.tick()
        assert sorted(n for n, _ in calls) == list(range(1, 11))
    run(registry_pg, tmp_path, body, received_min_cap=3)


# ----------------------------------------------------------------------------- the commit handler (Task 5b)

BASE_DOC = "".join(f"line {i}\n" for i in range(1, 11))
NO_GRANT = "no write grant"


class RecMirror(Mirror):
    """The mirror's ``refresh`` surface (``CollabMirror.refresh(store, artifact_id, *, gate)``), recording calls."""

    def __init__(self, root):
        super().__init__(root)
        self.refreshed = []
        self.fail = False

    async def refresh(self, store, artifact_id, *, gate):
        assert gate is not None
        self.refreshed.append(artifact_id)
        if self.fail:
            raise RuntimeError("mirror broke " + SENTINEL)
        return {}


def edit(text, lineno, new):
    lines = text.splitlines(keepends=True)
    lines[lineno - 1] = new + "\n"
    return "".join(lines)


def blob_entry(env, name, text, media_type="text/markdown"):
    key, digest = env.applier.rustfs.put(text.encode())
    return {"name": name, "media_type": media_type, "sha256": digest, "size_bytes": len(text.encode()),
            "storage_key": key}


async def seed(env, files=None, *, source_manifest_blob=None):
    async with env.pool.acquire() as conn:
        store = ArtifactStore(conn, env.applier.rustfs)
        aid = (await store.register_logical(original_filename="doc.md", created_by="owner"))["artifact_id"]
        version = await store.commit_version(
            aid, base_version_id=None, source_manifest_blob=source_manifest_blob,
            output_manifest=files if files is not None else [blob_entry(env, "doc.md", BASE_DOC)], created_by="owner")
    return aid, version["version_id"]


async def owner_edit(env, aid, base, text, name="doc.md"):
    async with env.pool.acquire() as conn:
        version = await ArtifactStore(conn, env.applier.rustfs).commit_version(
            aid, base_version_id=base, source_manifest_blob=None, output_manifest=[blob_entry(env, name, text)],
            created_by="owner")
    return version["version_id"]


async def grant(env, aid, agent_id, permission="write", *, days=1.0):
    async with env.pool.acquire() as conn:
        await CollabGrantStore(conn).grant(aid, agent_id, permission,
                                           expires_at=datetime.now(UTC) + timedelta(days=days), granted_by="owner")


def put(name, text):
    return {"name": name, "op": "put", "text": text}


def commit(env, agent_id, aid, base, changes, *, key=None, message="collab edit"):
    return env.intent(agent(agent_id), kind="commit", artifact_id=aid, key=key,
                      payload={"base_version_id": base, "message": message, "changes": changes})


async def settle(env):
    counts = await env.tick(wait=30)
    await env.applier.drain(timeout=30)
    later = await env.tick(wait=30)
    return {k: counts[k] + later[k] for k in counts}


async def sql(env, query, *args):
    async with env.pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def head_of(env, aid):
    (row,) = await sql(env, "SELECT current_version_id FROM artifacts WHERE artifact_id=$1", aid)
    return row["current_version_id"]


async def version_of(env, vid):
    async with env.pool.acquire() as conn:
        return await ArtifactStore(conn, env.applier.rustfs).get_version(vid)


async def text_of(env, vid, name="doc.md"):
    (entry,) = [e for e in (await version_of(env, vid))["output_manifest"] if e["name"] == name]
    return env.applier.rustfs.get(entry["storage_key"], entry["sha256"]).decode()


def committing(pg, tmp_path, body, **kw):
    kw.setdefault("mirror_cls", RecMirror)

    async def go(env):
        env.applier.register_commit()
        a = uid("agent")
        env.feed({a: "active"})
        return await body(env, a)
    return run(pg, tmp_path, go, **kw)


def test_a_fast_forward_commit_is_created_with_the_head_as_its_parent(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        mine = edit(BASE_DOC, 2, "mine")
        raw = commit(env, a, aid, v1, [put("doc.md", mine)])
        counts = await settle(env)
        doc = env.result(raw["ticket"])
        assert counts["applied"] == 1 and doc["result"] == "created" and not env.pending(raw["ticket"])
        assert doc["version_id"] == await head_of(env, aid) and doc["version_number"] == 2
        assert doc["actor"] == {"kind": "agent", "id": a} and doc["artifact_id"] == aid
        version = await version_of(env, doc["version_id"])
        assert version["parent_version_ids"] == [v1] and version["created_by"] == f"collab:{a}"
        assert version["created_via"] == "collaborator" and version["collaborator_agent_id"] == a
        assert await text_of(env, doc["version_id"]) == mine
        row = await ledger(env, raw["ticket"])
        assert row["outcome"] == "created" and row["version_id"] == doc["version_id"]
        assert row["bytes"] == len(mine.encode())
        assert env.mirror.refreshed == [aid]
    committing(registry_pg, tmp_path, body)


def test_a_stale_base_with_disjoint_edits_is_merged_with_two_parents(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        v2 = await owner_edit(env, aid, v1, edit(BASE_DOC, 2, "owner"))
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 9, "mine"))])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "merged" and doc["merged_names"] == ["doc.md"]
        version = await version_of(env, doc["version_id"])
        assert version["parent_version_ids"] == [v2, v1]
        merged = await text_of(env, doc["version_id"])
        assert "owner\n" in merged and "mine\n" in merged
    committing(registry_pg, tmp_path, body)


def test_overlapping_edits_conflict_and_persist_one_attempt_named_by_the_ledger(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        v2 = await owner_edit(env, aid, v1, edit(BASE_DOC, 5, "owner"))
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 5, "mine"))])
        counts = await settle(env)
        doc = env.result(raw["ticket"])
        assert counts["conflicts"] == 1 and doc["result"] == "conflict" and doc["head_version_id"] == v2
        (conflict,) = doc["conflicts"]
        assert conflict["name"] == "doc.md" and conflict["kind"] == "modify_modify_text"
        assert "<<<<<<<" in conflict["merged_text"] and conflict["truncated"] is False
        assert await head_of(env, aid) == v2
        (attempt,) = await sql(env, "SELECT * FROM artifact_collab_attempts WHERE artifact_id=$1", aid)
        assert attempt["ticket_id"] == raw["ticket"]
        row = await ledger(env, raw["ticket"])
        assert row["outcome"] == "conflict" and row["attempt_id"] == attempt["attempt_id"]
        async with env.pool.acquire() as conn:
            (listed,) = await CollabGrantStore(conn).attempts(aid)
        assert listed["ticket_id"] == raw["ticket"]
        assert listed["conflict_names"] == [{"name": "doc.md", "kind": "modify_modify_text"}]
        assert env.mirror.refreshed == []
    committing(registry_pg, tmp_path, body)


def test_conflict_text_over_the_cap_is_left_out_and_marked_truncated(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await owner_edit(env, aid, v1, edit(BASE_DOC, 5, "owner"))
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 5, "mine"))])
        await settle(env)
        (conflict,) = env.result(raw["ticket"])["conflicts"]
        assert conflict["merged_text"] is None and conflict["truncated"] is True
    committing(registry_pg, tmp_path, body, limits=BridgeLimits(max_conflict_text_bytes=16))


@pytest.mark.parametrize("case", ["none", "read", "expired"])
def test_a_commit_without_a_live_write_grant_is_rejected(registry_pg, tmp_path, case):
    async def body(env, a):
        aid, v1 = await seed(env)
        if case == "read":
            await grant(env, aid, a, "read")
        elif case == "expired":
            await grant(env, aid, a, days=-1)
        raw = commit(env, a, aid, v1, [put("doc.md", SENTINEL)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == NO_GRANT
        assert await head_of(env, aid) == v1
        assert SENTINEL not in (env.mirror_root / "results" / f"{raw['ticket']}.json").read_text()
    committing(registry_pg, tmp_path, body)


def test_a_grant_revoked_after_the_intent_was_written_is_rejected_before_staging_is_looked_at(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [{"name": "big.bin", "op": "put", "upload_id": "upl_" + "0" * 24}])
        async with env.pool.acquire() as conn:
            assert await CollabGrantStore(conn).revoke(aid, a)
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == NO_GRANT
    committing(registry_pg, tmp_path, body)


@pytest.mark.parametrize("case", ["artifact", "head_version", "base_version"])
def test_a_retracted_artifact_or_version_is_rejected(registry_pg, tmp_path, case):
    async def body(env, a):
        aid, v1 = await seed(env)
        v2 = await owner_edit(env, aid, v1, edit(BASE_DOC, 2, "owner"))
        await grant(env, aid, a)
        warm, w1 = await seed(env)                         # load the gate first: the durable state must decide
        await grant(env, warm, a)
        warm_up = commit(env, a, warm, w1, [put("doc.md", "warm\n")])
        await settle(env)
        assert env.result(warm_up["ticket"])["result"] == "created"
        async with env.pool.acquire() as conn:
            store = ArtifactStore(conn, env.applier.rustfs)
            if case == "artifact":
                await store.record_retraction(aid)
            else:
                await store.record_retraction(aid, v2 if case == "head_version" else v1)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 9, "mine"))])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and "retracted" in doc["reason"]
        assert await head_of(env, aid) == v2
    committing(registry_pg, tmp_path, body)


def test_a_retraction_only_the_gate_holds_is_rejected(registry_pg, tmp_path):
    async def body(env, a):
        gone, g1 = await seed(env)
        other, o1 = await seed(env)
        await grant(env, gone, a)
        await grant(env, other, a)
        async with env.pool.acquire() as conn:
            await ArtifactStore(conn, env.applier.rustfs).record_retraction(gone)
        first = commit(env, a, other, o1, [put("doc.md", "first\n")])
        await settle(env)                                  # the gate is loaded with load_retractions here
        assert env.result(first["ticket"])["result"] == "created"
        await sql(env, "UPDATE artifacts SET lifecycle='active' WHERE artifact_id=$1", gone)
        raw = commit(env, a, gone, g1, [put("doc.md", "second\n")])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and "retracted" in doc["reason"]
        assert await head_of(env, gone) == g1
    committing(registry_pg, tmp_path, body)


def test_a_report_artifact_is_not_writable_by_collaboration(registry_pg, tmp_path):
    async def body(env, a):
        typed, t1 = await seed(env)
        await sql(env, "UPDATE artifacts SET artifact_type='report' WHERE artifact_id=$1", typed)
        rendered, r1 = await seed(env, source_manifest_blob=blob_entry(env, "source.json", "{}")["storage_key"])
        tickets = []
        for aid, base in ((typed, t1), (rendered, r1)):
            await grant(env, aid, a)
            tickets.append(commit(env, a, aid, base, [put("doc.md", "x\n")])["ticket"])
        await settle(env)
        for ticket in tickets:
            doc = env.result(ticket)
            assert doc["result"] == "rejected" and doc["reason"] == "not writable by collaboration"
        assert await head_of(env, typed) == t1 and await head_of(env, rendered) == r1
    committing(registry_pg, tmp_path, body)


@pytest.mark.parametrize("name", ["../x", "a/../../b", "a\x00b", "a\rb", "dir", "doc.md/x"])
def test_hostile_names_are_rejected_without_stopping_the_tick(registry_pg, tmp_path, name):
    async def body(env, a):
        aid, v1 = await seed(env, [blob_entry(env, "doc.md", BASE_DOC), blob_entry(env, "dir/a.md", "a\n")])
        await grant(env, aid, a)
        hostile = commit(env, a, aid, v1, [put(name, "x\n")])
        b = uid("agent")
        env.feed({a: "active", b: "active"})
        await grant(env, aid, b)
        fine = commit(env, b, aid, v1, [put("doc.md", edit(BASE_DOC, 3, "fine"))])
        counts = await settle(env)
        doc = env.result(hostile["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "invalid or clashing file names"
        assert counts["rejected"] == 1 and env.result(fine["ticket"])["result"] == "created"
        assert not env.pending(hostile["ticket"])
        manifests = [e["name"] for e in (await version_of(env, await head_of(env, aid)))["output_manifest"]]
        assert sorted(manifests) == ["dir/a.md", "doc.md"]
    committing(registry_pg, tmp_path, body)


@pytest.mark.parametrize("text", ["".join(chr(97 + i % 26) + "\n" for i in range(100_000)), "y" * 60_000])
def test_hostile_text_shapes_commit_or_conflict_normally(registry_pg, tmp_path, text):
    async def body(env, a):
        aid, v1 = await seed(env)
        await owner_edit(env, aid, v1, edit(BASE_DOC, 2, "owner"))
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", text)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] in ("merged", "conflict")
        if doc["result"] == "conflict":
            assert doc["conflicts"][0]["kind"] == "modify_modify_text" and doc["conflicts"][0]["merged_text"]
    committing(registry_pg, tmp_path, body)


def test_inline_bytes_over_the_daily_quota_are_rejected_and_a_retry_is_not_counted_twice(registry_pg, tmp_path):
    first_text = "q" * 40 + "\n"

    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        first = commit(env, a, aid, v1, [put("doc.md", first_text)])
        # crash-retry shape: the ticket's own received row (with its inline bytes) already exists
        async with env.pool.acquire() as conn:
            await CollabGrantStore(conn).record_received(
                first["ticket"], "commit", Actor("agent", a), aid, v1, first["ticket"], request_digest(first),
                len(first_text))
        await settle(env)
        assert env.result(first["ticket"])["result"] == "created"
        over = commit(env, a, aid, await head_of(env, aid), [put("doc.md", "z\n")])
        await settle(env)
        doc = env.result(over["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "daily quota exceeded"
        # nothing of the refused commit is stored: no bytes, no scoped blob row, no version
        key, digest = FakeBlobs.key(hashlib.sha256(b"z\n").hexdigest()), hashlib.sha256(b"z\n").hexdigest()
        assert key not in env.applier.rustfs.objects
        assert await sql(env, "SELECT 1 FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2", aid, digest) == []
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 2
    committing(registry_pg, tmp_path, body, limits=BridgeLimits(daily_bytes_per_agent=len(first_text)))


def test_a_forged_storage_key_is_rejected_by_validation_and_never_referenced(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        other, _ = await seed(env)
        key, digest = env.applier.rustfs.put(b"other's private bytes\n")
        async with env.pool.acquire() as conn:
            await ArtifactStore(conn, env.applier.rustfs).register_report_blob(
                other, storage_key=key, sha256=digest, size_bytes=22, media_type="text/plain")
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [{"name": "doc.md", "op": "put", "storage_key": key}])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "a change has an unknown or forbidden key"
        assert await head_of(env, aid) == v1
        assert await sql(env, "SELECT 1 FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2", aid, digest) == []
        manifests = await sql(env, "SELECT output_manifest::text AS m FROM artifact_versions WHERE artifact_id=$1",
                              aid)
        assert all(key not in r["m"] for r in manifests)
    committing(registry_pg, tmp_path, body)


def test_two_tickets_with_one_key_produce_one_version_and_both_name_it(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        changes = [put("doc.md", edit(BASE_DOC, 4, "once"))]
        one = commit(env, a, aid, v1, changes, key="retry-1")
        two = commit(env, a, aid, v1, changes, key="retry-1")
        await settle(env)
        first, second = env.result(one["ticket"]), env.result(two["ticket"])
        assert first["result"] == second["result"] == "created"
        assert first["version_id"] == second["version_id"] == await head_of(env, aid)
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 2
    committing(registry_pg, tmp_path, body)


def test_hooks_run_on_success_and_their_failures_are_logged_not_fatal(registry_pg, tmp_path, caplog):
    seen = []

    def lineage(version):
        seen.append(dict(version))
        raise RuntimeError("lineage broke " + SENTINEL)

    async def body(env, a):
        env.mirror.fail = True
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", SENTINEL + "\n")], message="m")
        counts = await settle(env)
        doc = env.result(raw["ticket"])
        assert counts["applied"] == 1 and doc["result"] == "created"
        assert (await ledger(env, raw["ticket"]))["outcome"] == "created"
        assert env.mirror.refreshed == [aid]
        assert aid in env.applier.take_mirror_force() and env.applier.take_mirror_force() == set()
        assert [(v["artifact_id"], v["version_id"]) for v in seen] == [(aid, doc["version_id"])]
        refused = commit(env, a, uid("art"), v1, [put("doc.md", SENTINEL + "\n")])
        await settle(env)
        assert SENTINEL not in (env.mirror_root / "results" / f"{refused['ticket']}.json").read_text()

    caplog.set_level(logging.DEBUG)
    committing(registry_pg, tmp_path, body, lineage=lineage)
    assert "lineage" in caplog.text and "mirror" in caplog.text
    assert SENTINEL not in caplog.text


def test_hooks_run_on_the_merged_path(registry_pg, tmp_path):
    seen = []

    async def body(env, a):
        aid, v1 = await seed(env)
        await owner_edit(env, aid, v1, edit(BASE_DOC, 2, "owner"))
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 9, "mine"))])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "merged"
        assert env.mirror.refreshed == [aid]
        assert [(v["artifact_id"], v["version_id"]) for v in seen] == [(aid, doc["version_id"])]
    committing(registry_pg, tmp_path, body, lineage=seen.append)


async def poll_result(env, ticket, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await env.tick(wait=1)
        doc = env.result(ticket)
        if doc is not None:
            return doc
        await asyncio.sleep(0.05)
    return None


def test_a_blocked_lineage_does_not_hold_the_lane(registry_pg, tmp_path):
    release, entered, seen = threading.Event(), threading.Event(), []

    def lineage(version):
        entered.set()
        release.wait(30)
        seen.append(version["version_id"])

    async def body(env, a):
        try:
            aid, v1 = await seed(env)
            await grant(env, aid, a)
            first = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 2, "one"))])
            doc = await poll_result(env, first["ticket"])
            assert doc is not None and doc["result"] == "created"
            assert await asyncio.to_thread(entered.wait, 10)
            second = commit(env, a, aid, doc["version_id"], [put("doc.md", edit(BASE_DOC, 3, "two"))])
            later = await poll_result(env, second["ticket"])
            assert later is not None and later["result"] == "created"   # decided while lineage is still blocked
            assert seen == []
        finally:
            release.set()
        await env.applier.drain(timeout=30)
        assert seen == [doc["version_id"], later["version_id"]]
    committing(registry_pg, tmp_path, body, lineage=lineage)


async def crash_before_outcome(env, raw):
    """The worker died after merge_commit and before record_outcome: the row is ``received`` again, the intent
    file is back and no result was written."""
    await sql(env, "UPDATE artifact_collab_intents SET outcome='received', version_id=NULL, attempt_id=NULL, "
                   "reason=NULL, decided_at=NULL WHERE ticket_id=$1", raw["ticket"])
    (env.mirror_root / "results" / f"{raw['ticket']}.json").unlink()
    (env.outbox / "intents" / f"{raw['ticket']}.json").write_text(json.dumps(raw))


FEED_CASES = ("feed_stale_scan", "feed_stale_lane", "deactivated_scan", "deactivated_lane")


def break_feed(env, a, case):
    if case.startswith("feed_stale"):
        env.feed({a: "active"}, age=cbf.FEED_MAX_AGE + 60)
    else:
        env.feed({a: "suspended"})


async def retry_through_the_lane(env, raw, a, case):
    """A rediscovered ticket is settled by the scan's early replay, so the lane's check is reached by a new ticket
    for the same request and key (the crashed one withdrawn): received while its key lock is held elsewhere, then
    the feed breaks and the lane runs. Returns the new ticket."""
    (env.outbox / "intents" / f"{raw['ticket']}.json").unlink()
    same = commit(env, a, raw["artifact_id"], raw["payload"]["base_version_id"], raw["payload"]["changes"],
                  key=raw["idempotency_key"])
    other = await asyncpg.connect(env.dsn)
    lock = _LOCK_SALT + f"key:agent:{a}:commit:{raw['idempotency_key']}"
    try:
        assert await other.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock)
        counts = await env.tick(wait=5)
        await env.applier.drain(timeout=10)
        assert counts["skipped"] >= 1 and env.result(same["ticket"]) is None
        break_feed(env, a, case)
        await other.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock)
    finally:
        await other.close()
    return same


@pytest.mark.parametrize("case", ["revoked", "names", "quota", "retracted", *FEED_CASES])
def test_a_crash_retry_reports_the_existing_version_whatever_would_now_refuse_it(registry_pg, tmp_path, case):
    text = edit(BASE_DOC, 4, "once")
    limits = BridgeLimits(daily_bytes_per_agent=len(text.encode()) + 10)

    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", text)], key="k-" + case if case.endswith("_lane") else None)
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "created"
        env.mirror.refreshed.clear()
        retry = raw
        await crash_before_outcome(env, raw)
        if case == "revoked":
            async with env.pool.acquire() as conn:
                assert await CollabGrantStore(conn).revoke(aid, a)
        elif case == "names":                              # head now has doc.md/x: a put of doc.md clashes
            await owner_edit(env, aid, doc["version_id"], "x\n", name="doc.md/x")
        elif case == "quota":                              # another commit used up the day's bytes
            async with env.pool.acquire() as conn:
                await CollabGrantStore(conn).record_received(
                    new_ticket(), "commit", Actor("agent", a), aid, v1, None, "0" * 64, 1000)
        elif case == "retracted":
            async with env.pool.acquire() as conn:
                await ArtifactStore(conn, env.applier.rustfs).record_retraction(aid, doc["version_id"])
        elif case.endswith("_scan"):
            break_feed(env, a, case)
        else:
            env.dsn = registry_pg["dsn"]
            retry = await retry_through_the_lane(env, raw, a, case)
        await settle(env)
        again = env.result(retry["ticket"])
        assert again["result"] == "created" and again["version_id"] == doc["version_id"]
        row = await ledger(env, retry["ticket"])
        assert row["version_id"] == doc["version_id"]
        assert row["bytes"] == (0 if retry is not raw else len(text.encode()))
        versions = await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)
        assert len(versions) == (3 if case == "names" else 2)
        assert env.mirror.refreshed == [aid]            # the after-hook the crash skipped runs now
    committing(registry_pg, tmp_path, body, limits=limits)


def test_a_new_ticket_reusing_a_crashed_tickets_key_for_another_request_is_rejected(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 4, "once"))], key="k-crash")
        await settle(env)
        assert env.result(raw["ticket"])["result"] == "created"
        await crash_before_outcome(env, raw)
        (env.outbox / "intents" / f"{raw['ticket']}.json").unlink()      # withdrawn: only the new ticket runs
        other = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 5, "other"))], key="k-crash")
        await settle(env)
        refused = env.result(other["ticket"])
        assert refused["result"] == "rejected"
        assert refused["reason"] == "idempotency key was reused for a different request"
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 2
    committing(registry_pg, tmp_path, body)


def test_a_clean_stale_base_commit_merges_when_head_replaced_a_file_with_a_directory(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env, [blob_entry(env, "doc.md", BASE_DOC), blob_entry(env, "a", "file\n")])
        async with env.pool.acquire() as conn:
            v2 = (await ArtifactStore(conn, env.applier.rustfs).commit_version(
                aid, base_version_id=v1, source_manifest_blob=None, created_by="owner",
                output_manifest=[blob_entry(env, "doc.md", BASE_DOC), blob_entry(env, "a/b", "nested\n")]))["version_id"]
        await grant(env, aid, a)
        raw = commit(env, a, aid, v1, [put("doc.md", edit(BASE_DOC, 9, "mine"))])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "merged", doc
        version = await version_of(env, doc["version_id"])
        assert version["parent_version_ids"] == [v2, v1]
        assert sorted(e["name"] for e in version["output_manifest"]) == ["a/b", "doc.md"]
    committing(registry_pg, tmp_path, body)


def test_a_new_ticket_for_a_crashed_tickets_request_replays_its_version_uncharged(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        changes = [put("doc.md", edit(BASE_DOC, 4, "once"))]
        raw = commit(env, a, aid, v1, changes, key="k-crash")
        await settle(env)
        first = env.result(raw["ticket"])
        await crash_before_outcome(env, raw)
        (env.outbox / "intents" / f"{raw['ticket']}.json").unlink()
        same = commit(env, a, aid, v1, changes, key="k-crash")
        await settle(env)
        doc = env.result(same["ticket"])
        assert doc["result"] == "created" and doc["version_id"] == first["version_id"]
        assert (await ledger(env, same["ticket"]))["bytes"] == 0
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 2
    committing(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- staged uploads (Task 5c)

CHUNK = cbf.CHUNK_BYTES
BLOB = (bytes(range(128, 256)) * (3 * CHUNK // 128 + 1))[:2 * CHUNK + 1000]    # 3 parts, never UTF-8
NOT_FOUND, INCOMPLETE, FAILED = "upload not found", "upload incomplete", "upload failed verification"


def stage(env, owner, aid, name, data, *, skip=(), created_at=None):
    """Stage ``data`` as collab-public would: ``staging/<owner>/<upload_id>/{meta.json, 0000000..}``."""
    sha = hashlib.sha256(data).hexdigest()
    upl = cbf.upload_id_for(owner, aid, name, sha, len(data))
    total = -(-len(data) // CHUNK)
    d = env.outbox / "staging" / owner / upl
    d.mkdir(parents=True, exist_ok=True)
    meta = cbf.StagedUpload(upload_id=upl, owner_key=owner, artifact_id=aid, name=name, size_bytes=len(data),
                            file_sha256=sha, total=total, created_at=env.clock() if created_at is None else created_at)
    (d / "meta.json").write_text(json.dumps(meta.to_dict()))
    for i in range(total):
        if i not in skip:
            (d / f"{i:07d}").write_bytes(data[i * CHUNK:(i + 1) * CHUNK])
    return upl, d


def staged(name, upl):
    return {"name": name, "op": "put", "upload_id": upl}


def entry_of(env, name, data, media_type):
    key, digest = env.applier.rustfs.put(data)
    return {"name": name, "media_type": media_type, "sha256": digest, "size_bytes": len(data), "storage_key": key}


async def owner_commit(env, aid, base, entries):
    async with env.pool.acquire() as conn:
        return (await ArtifactStore(conn, env.applier.rustfs).commit_version(
            aid, base_version_id=base, source_manifest_blob=None, output_manifest=entries,
            created_by="owner"))["version_id"]


def reset_calls(env):
    env.applier.rustfs.calls.update(put=0, put_stream=0)


def tree(d):
    return {p.relative_to(d).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(d.rglob("*"))}


async def upload_row(env, owner, upl):
    rows = await sql(env, "SELECT * FROM artifact_collab_uploads WHERE owner_key=$1 AND upload_id=$2", owner, upl)
    return dict(rows[0]) if rows else None


async def used_bytes(env, agent_id):
    async with env.pool.acquire() as conn:
        return await CollabGrantStore(conn).bytes_last_24h(agent_id)


def test_a_three_chunk_staged_binary_file_is_assembled_verified_and_only_meta_and_markers_remain(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB)
        (d / "audited").write_bytes(b"")                       # collab-public's audit marker stays
        (d / ".tmp-0123456789abcdef").write_bytes(b"stale")    # a stale fallback tmp goes with the parts
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "created", doc
        (entry,) = [e for e in (await version_of(env, doc["version_id"]))["output_manifest"] if e["name"] == "blob.bin"]
        assert entry["sha256"] == hashlib.sha256(BLOB).hexdigest() and entry["size_bytes"] == len(BLOB)
        assert entry["media_type"] == "application/octet-stream"
        assert env.applier.rustfs.get(entry["storage_key"], entry["sha256"]) == BLOB
        assert sorted(p.name for p in d.iterdir()) == ["audited", "meta.json", "verified"]
        assert env.applier.rustfs.calls["put_stream"] == 1
        assert env.applier.rustfs.spool_dirs == [env.applier.spool_dir]
        row = await upload_row(env, a, upl)
        assert row["charged"] and row["size_bytes"] == len(BLOB) and row["artifact_id"] == aid
        assert (await ledger(env, raw["ticket"]))["bytes"] == 0
        assert await used_bytes(env, a) == len(BLOB)
    committing(registry_pg, tmp_path, body)


def test_an_upload_staged_by_collab_public_is_consumable_by_the_assembly(registry_pg, tmp_path):
    """Task 7b's ``stage_chunk_for`` (the real collab-public writer, out of order) produces exactly the layout the
    5c assembly consumes; afterwards a re-sent chunk is a complete replay that writes nothing."""
    import base64

    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    from herdr_engineering.control_plane.collab_artifacts import CollabArtifactBridge

    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        service = CollabService(StaticTokenAuthenticator({}), now=env.clock)
        bridge = CollabArtifactBridge(service, tmp_path / "mirror", env.outbox, clock=env.clock, feed_interval=3600.0)
        sha = hashlib.sha256(BLOB).hexdigest()

        def chunk(i):
            piece = BLOB[i * CHUNK:(i + 1) * CHUNK]
            return cbf.validate_chunk({"artifact_id": aid, "name": "blob.bin", "size_bytes": len(BLOB),
                                       "file_sha256": sha, "total": 3, "index": i,
                                       "data": base64.b64encode(piece).decode(),
                                       "sha256": hashlib.sha256(piece).hexdigest()}, env.applier.limits)
        outs = [await asyncio.to_thread(bridge.stage_chunk_for, a, chunk(i), limits=env.applier.limits,
                                        staging_cap=env.applier.limits.staging_bytes_per_agent) for i in (2, 0, 1)]
        upl = outs[0]["upload_id"]
        assert outs[0]["complete"] is False and outs[0]["missing"] == [0, 1]
        replay = await asyncio.to_thread(bridge.stage_chunk_for, a, chunk(2), limits=env.applier.limits,
                                         staging_cap=env.applier.limits.staging_bytes_per_agent)
        assert replay["complete"] is True
        d = env.outbox / "staging" / a / upl
        assert sorted(p.name for p in d.iterdir()) == ["0000000", "0000001", "0000002", "audited", "meta.json"]
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "created", doc
        (entry,) = [e for e in (await version_of(env, doc["version_id"]))["output_manifest"] if e["name"] == "blob.bin"]
        assert entry["sha256"] == sha and env.applier.rustfs.get(entry["storage_key"], entry["sha256"]) == BLOB
        assert sorted(p.name for p in d.iterdir()) == ["audited", "meta.json", "verified"]
        after = await asyncio.to_thread(bridge.stage_chunk_for, a, chunk(0), limits=env.applier.limits,
                                        staging_cap=env.applier.limits.staging_bytes_per_agent)
        assert after["stored"] is False and after["complete"] is True
        assert sorted(p.name for p in d.iterdir()) == ["audited", "meta.json", "verified"]
        assert [e["event_type"] for e in service._log].count("artifact_upload_staged") == 1
    committing(registry_pg, tmp_path, body)


BIG = "".join(f"big line {i}\n" for i in range(40))


def test_a_staged_text_file_over_the_merge_cap_merges_on_one_side_and_conflicts_oversize_on_both(registry_pg, tmp_path):
    async def body(env, a):
        doc0 = blob_entry(env, "doc.md", BASE_DOC)
        aid, v1 = await seed(env, [doc0, blob_entry(env, "big.txt", BIG, "text/plain")])
        await grant(env, aid, a)
        v2 = await owner_commit(env, aid, v1, [blob_entry(env, "doc.md", edit(BASE_DOC, 1, "owner")),
                                               blob_entry(env, "big.txt", BIG, "text/plain")])
        mine = edit(BIG, 30, "mine").encode()
        upl, _ = stage(env, a, aid, "big.txt", mine)
        one = commit(env, a, aid, v1, [staged("big.txt", upl)])
        await settle(env)
        merged = env.result(one["ticket"])
        assert merged["result"] == "merged", merged
        assert await text_of(env, merged["version_id"], "big.txt") == mine.decode()
        assert await text_of(env, merged["version_id"], "doc.md") == edit(BASE_DOC, 1, "owner")
        v4 = await owner_commit(env, aid, merged["version_id"], [
            blob_entry(env, "doc.md", edit(BASE_DOC, 1, "owner")),
            blob_entry(env, "big.txt", edit(mine.decode(), 2, "owner"), "text/plain")])
        again = edit(mine.decode(), 2, "contributor").encode()
        upl2, _ = stage(env, a, aid, "big.txt", again)
        two = commit(env, a, aid, merged["version_id"], [staged("big.txt", upl2)])
        await settle(env)
        doc = env.result(two["ticket"])
        assert doc["result"] == "conflict" and doc["head_version_id"] == v4
        assert doc["conflicts"] == [{"name": "big.txt", "kind": "oversize", "merged_text": None, "truncated": False}]
        assert v2
    committing(registry_pg, tmp_path, body, limits=BridgeLimits(max_text_merge_bytes=64))


def test_a_binary_file_changed_on_both_sides_conflicts_and_a_recommit_reuses_the_verified_upload(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env, [blob_entry(env, "doc.md", BASE_DOC),
                                   entry_of(env, "blob.bin", b"\x80base", "application/octet-stream")])
        await grant(env, aid, a)
        v2 = await owner_commit(env, aid, v1, [blob_entry(env, "doc.md", BASE_DOC),
                                               entry_of(env, "blob.bin", b"\x80owner", "application/octet-stream")])
        upl, d = stage(env, a, aid, "blob.bin", BLOB)
        reset_calls(env)
        first = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(first["ticket"])
        assert doc["result"] == "conflict" and doc["head_version_id"] == v2
        assert doc["conflicts"] == [{"name": "blob.bin", "kind": "binary", "merged_text": None, "truncated": False}]
        assert await used_bytes(env, a) == len(BLOB)
        assert sorted(p.name for p in d.iterdir()) == ["meta.json", "verified"]
        second = commit(env, a, aid, doc["head_version_id"], [staged("blob.bin", upl)])
        await settle(env)
        done = env.result(second["ticket"])
        assert done["result"] == "created", done
        assert env.applier.rustfs.calls["put_stream"] == 1          # never re-assembled
        assert await used_bytes(env, a) == len(BLOB)                # never charged twice (the quota is exactly one)
        (entry,) = [e for e in (await version_of(env, done["version_id"]))["output_manifest"] if e["name"] == "blob.bin"]
        assert entry["sha256"] == hashlib.sha256(BLOB).hexdigest()
    committing(registry_pg, tmp_path, body, limits=BridgeLimits(daily_bytes_per_agent=len(BLOB)))


@pytest.mark.parametrize("case", ["other_agent", "other_artifact", "other_name"])
def test_an_upload_that_is_not_the_callers_own_for_this_artifact_and_name_is_not_found(registry_pg, tmp_path, case):
    async def body(env, a):
        aid, v1 = await seed(env)
        other_aid, _ = await seed(env)
        await grant(env, aid, a)
        if case == "other_agent":
            upl, d = stage(env, uid("agent"), aid, "blob.bin", BLOB)
        elif case == "other_artifact":
            upl, d = stage(env, a, other_aid, "blob.bin", BLOB)
        else:
            upl, d = stage(env, a, aid, "other.bin", BLOB)
        before = tree(d)
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == NOT_FOUND
        assert env.applier.rustfs.calls == {"put": 0, "put_stream": 0}
        assert tree(d) == before and await head_of(env, aid) == v1
    committing(registry_pg, tmp_path, body)


def test_a_missing_part_is_incomplete_and_nothing_is_stored(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB, skip={1})
        before = tree(d)
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl), put("doc.md", "inline\n")])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == INCOMPLETE
        assert env.applier.rustfs.calls == {"put": 0, "put_stream": 0}
        assert tree(d) == before and await upload_row(env, a, upl) is None
    committing(registry_pg, tmp_path, body)


def _growing_part(monkeypatch):
    """Part 1 raises BridgeError mid-read, as a part that grew past its cap after the open does."""
    from herdr_engineering.control_plane import collab_intents
    real = collab_intents.open_beneath

    class Grows:
        def __init__(self, inner):
            self.inner = inner

        def read(self, n=-1):
            raise cbf.BridgeError("file grew past its size cap")

        def close(self):
            self.inner.close()

    def opener(root, parts, *, max_bytes=None):
        reader = real(root, parts, max_bytes=max_bytes)
        return Grows(reader) if reader is not None and parts[-1] == "0000001" else reader
    monkeypatch.setattr(collab_intents, "open_beneath", opener)


@pytest.mark.parametrize("case", ["overwritten", "oversized_part", "grew_mid_stream"])
def test_a_part_changed_after_staging_fails_verification_and_the_upload_is_deleted(registry_pg, tmp_path, case,
                                                                                    monkeypatch):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB)
        if case == "overwritten":
            (d / "0000001").write_bytes(b"\x00" * CHUNK)
        elif case == "oversized_part":
            (d / "0000001").write_bytes(b"\x00" * (CHUNK + 1))
        else:
            _growing_part(monkeypatch)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == FAILED
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 1
        assert await sql(env, "SELECT 1 FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2", aid,
                         hashlib.sha256(BLOB).hexdigest()) == []
        assert await upload_row(env, a, upl) is None
        assert not d.exists() and (env.outbox / "staging" / a).is_dir()
    committing(registry_pg, tmp_path, body)


@pytest.mark.parametrize("complete", [True, False])
def test_a_forged_verified_marker_without_a_row_is_ignored(registry_pg, tmp_path, complete):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB, skip=() if complete else {2})
        (d / "verified").write_bytes(b"")
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        if complete:
            assert doc["result"] == "created" and env.applier.rustfs.calls["put_stream"] == 1
            assert (await upload_row(env, a, upl))["sha256"] == hashlib.sha256(BLOB).hexdigest()
        else:
            assert doc["result"] == "rejected" and doc["reason"] == INCOMPLETE
            assert env.applier.rustfs.calls["put_stream"] == 0 and await upload_row(env, a, upl) is None
    committing(registry_pg, tmp_path, body)


def test_a_symlinked_part_is_rejected_and_its_target_is_untouched(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB, skip={1})
        outside = tmp_path / "outside-part"
        outside.write_bytes(BLOB[CHUNK:2 * CHUNK])
        (d / "0000001").symlink_to(outside)
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == INCOMPLETE
        assert env.applier.rustfs.calls["put_stream"] == 0
        assert outside.read_bytes() == BLOB[CHUNK:2 * CHUNK] and (d / "0000001").is_symlink()
    committing(registry_pg, tmp_path, body)


@pytest.mark.parametrize("level", ["agent_dir", "upload_dir"])
def test_a_staging_dir_swapped_for_a_symlink_is_rejected_and_nothing_outside_is_touched(registry_pg, tmp_path, level):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB)       # complete and valid: following the link would assemble it
        outside = tmp_path / "outside"
        outside.mkdir()
        moved = d.parent if level == "agent_dir" else d
        target = outside / moved.name
        moved.rename(target)
        moved.symlink_to(target, target_is_directory=True)
        before = tree(outside)
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == NOT_FOUND
        assert env.applier.rustfs.calls == {"put": 0, "put_stream": 0}
        assert tree(outside) == before and not any(p.name == "verified" for p in outside.rglob("*"))
        assert await upload_row(env, a, upl) is None
    committing(registry_pg, tmp_path, body)


def test_a_declared_size_over_the_daily_quota_is_rejected_before_any_rustfs_call(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB)
        before = tree(d)
        reset_calls(env)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl), put("doc.md", "x\n")])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "daily quota exceeded"
        assert env.applier.rustfs.calls == {"put": 0, "put_stream": 0}
        assert tree(d) == before and await upload_row(env, a, upl) is None
    # the upload alone fits; with the commit's 2 inline bytes it does not
    committing(registry_pg, tmp_path, body, limits=BridgeLimits(daily_bytes_per_agent=len(BLOB) + 1))


def test_a_crash_retry_of_a_consumed_staged_put_reports_the_same_version(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        upl, d = stage(env, a, aid, "blob.bin", BLOB)
        raw = commit(env, a, aid, v1, [staged("blob.bin", upl)], key="k-staged")
        await settle(env)
        first = env.result(raw["ticket"])
        assert first["result"] == "created"
        assert not (d / "0000000").exists()
        await crash_before_outcome(env, raw)
        await settle(env)
        again = env.result(raw["ticket"])
        assert again["result"] == "created" and again["version_id"] == first["version_id"]
        same = commit(env, a, aid, v1, [staged("blob.bin", upl)], key="k-staged")
        await settle(env)
        replay = env.result(same["ticket"])
        assert replay["result"] == "created" and replay["version_id"] == first["version_id"]
        assert env.applier.rustfs.calls["put_stream"] == 1
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 2
        assert await used_bytes(env, a) == len(BLOB)
    committing(registry_pg, tmp_path, body)


def test_an_owner_key_assembles_uncharged_through_ctx_assemble(registry_pg, tmp_path, registry):
    register_x()
    seen = {}

    async def handler(ctx, intent):
        seen["entry"] = await ctx.assemble("m_test", seen["upl"], artifact_id=seen["aid"], name="blob.bin",
                                           charge=False)
        return {"result": "done"}

    async def body(env):
        env.applier.register("x.test", handler)
        a = uid("agent")
        aid, _ = await seed(env)
        seen["aid"] = aid
        seen["upl"], d = stage(env, "m_test", aid, "blob.bin", BLOB)
        used = await used_bytes(env, a)
        raw = env.intent({"kind": "user", "id": "u_owner"})
        await settle(env)
        assert env.result(raw["ticket"])["result"] == "done"
        assert seen["entry"]["sha256"] == hashlib.sha256(BLOB).hexdigest()
        assert seen["entry"]["size_bytes"] == len(BLOB) and seen["entry"]["name"] == "blob.bin"
        row = await upload_row(env, "m_test", seen["upl"])
        assert row is not None and row["charged"] is False
        assert await used_bytes(env, "m_test") == 0 and await used_bytes(env, a) == used
        assert sorted(p.name for p in d.iterdir()) == ["meta.json", "verified"]
    run(registry_pg, tmp_path, body)


def test_staged_bytes_never_reach_the_logs(registry_pg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    secret = (SENTINEL.encode() * (CHUNK // len(SENTINEL) + 2))[:CHUNK + 10]

    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        good, _ = stage(env, a, aid, "secret.txt", secret)
        bad, d = stage(env, a, aid, "bad.txt", secret[::-1])
        (d / "0000001").write_bytes(SENTINEL.encode())
        one = commit(env, a, aid, v1, [staged("secret.txt", good)])
        await settle(env)
        two = commit(env, a, aid, env.result(one["ticket"])["version_id"], [staged("bad.txt", bad)])
        await settle(env)
        assert env.result(one["ticket"])["result"] == "created"
        assert env.result(two["ticket"])["reason"] == FAILED
    committing(registry_pg, tmp_path, body)
    assert SENTINEL not in caplog.text and SENTINEL[::-1] not in caplog.text


# ----------------------------------------------------------------------------- staging sweep (Task 5d)

TTL = BridgeLimits().staging_ttl_seconds


def age_dir(env, d, seconds):
    """Make every file in ``d`` (and ``d``) ``seconds`` old by the fake clock."""
    when = env.clock() - seconds
    for p in [*d.rglob("*"), d]:
        os.utime(p, (when, when), follow_symlinks=False)


def live_grant(aid, agent_id, permission="write", *, expires=None):
    return {"artifact_id": aid, "agent_id": agent_id, "permission": permission,
            "expires_at": expires or datetime.now(UTC) + timedelta(days=1)}


def active(*ids):
    return {i: {"status": "active"} for i in ids}


async def swept(env, agents, grants, *, feed_age=0.0):
    """One sweep over the pool's connection; ``agents.json`` is written fresh (``feed_age`` seconds old) unless
    ``feed_age`` is None (no feed at all)."""
    if feed_age is not None:
        (env.outbox / "agents.json").write_text(json.dumps(
            {"generated_at": env.clock() - feed_age, "agents": agents if isinstance(agents, dict) else {}}))
    async with env.pool.acquire() as conn:
        return await env.applier.sweep_staging(conn, agents=agents, grants=grants)


def sweeping(pg, tmp_path, body, **kw):
    async def go(env):
        return await body(env, uid("agent"))
    return run(pg, tmp_path, go, **kw)


def test_sweep_removes_an_upload_older_than_the_ttl_and_keeps_a_fresh_one(registry_pg, tmp_path):
    async def body(env, a):
        old, d_old = stage(env, a, "art1", "old.bin", BLOB)
        new, d_new = stage(env, a, "art1", "new.bin", BLOB[:100])
        age_dir(env, d_old, TTL + 1)
        age_dir(env, d_new, TTL - 60)
        n = await swept(env, active(a), [live_grant("art1", a)])
        assert n == 1 and not d_old.exists() and d_new.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_the_ttl_counts_from_the_newest_part_not_the_oldest(registry_pg, tmp_path):
    async def body(env, a):
        _, d = stage(env, a, "art1", "big.bin", BLOB)
        age_dir(env, d, TTL + 1000)
        fresh = env.clock() - 5
        os.utime(d / "0000002", (fresh, fresh))
        assert await swept(env, active(a), [live_grant("art1", a)]) == 0 and d.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_sweep_removes_the_uploads_of_a_revoked_agent(registry_pg, tmp_path):
    async def body(env, a):
        b = uid("agent")
        _, d_a = stage(env, a, "art1", "a.bin", BLOB[:50])
        _, d_b = stage(env, b, "art1", "b.bin", BLOB[:50])
        assert await swept(env, {a: {"status": "revoked"}}, [live_grant("art1", a), live_grant("art1", b)]) == 2
        assert not d_a.exists() and not d_b.exists()      # b is absent from agents.json altogether
    sweeping(registry_pg, tmp_path, body)


@pytest.mark.parametrize("grants", [
    "none", "other_artifact", "read_only", "expired",
])
def test_sweep_removes_an_upload_whose_write_grant_is_gone(registry_pg, tmp_path, grants):
    async def body(env, a):
        _, d = stage(env, a, "art1", "a.bin", BLOB[:50])
        _, keep = stage(env, a, "art2", "b.bin", BLOB[:50])
        live = {
            "none": [], "other_artifact": [live_grant("art3", a)], "read_only": [live_grant("art1", a, "read")],
            "expired": [live_grant("art1", a, expires=datetime.now(UTC) - timedelta(seconds=1))],
        }[grants]
        n = await swept(env, active(a), [*live, live_grant("art2", a)])
        assert n == 1 and not d.exists() and keep.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_sweep_keeps_an_upload_referenced_by_a_pending_intent(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = "art1", "ver1"
        upl, d = stage(env, a, aid, "blob.bin", BLOB)
        gone, d_gone = stage(env, a, aid, "gone.bin", BLOB[:50])
        age_dir(env, d, TTL + 1)
        age_dir(env, d_gone, TTL + 1)
        env.intent(agent(a), kind="commit", artifact_id=aid,
                   payload={"base_version_id": v1, "message": "m", "changes": [staged("blob.bin", upl)]})
        n = await swept(env, {}, [])                          # revoked, no grant, past the TTL: still pending
        assert n == 1 and d.is_dir() and not d_gone.exists()
        for f in (env.outbox / "intents").iterdir():
            f.unlink()
        assert await swept(env, {}, []) == 1 and not d.exists()
    sweeping(registry_pg, tmp_path, body)


def test_sweep_keeps_a_young_dir_without_meta_and_removes_an_old_one(registry_pg, tmp_path):
    async def body(env, a):
        young = env.outbox / "staging" / a / cbf.upload_id_for(a, "art1", "y.bin", "0" * 64, 5)
        old = env.outbox / "staging" / a / cbf.upload_id_for(a, "art1", "o.bin", "1" * 64, 5)
        bad = env.outbox / "staging" / a / cbf.upload_id_for(a, "art1", "b.bin", "2" * 64, 5)
        for d in (young, old, bad):
            d.mkdir(parents=True)
            (d / "0000000").write_bytes(b"x")
        (bad / "meta.json").write_text("{not json")
        age_dir(env, old, 61)
        age_dir(env, bad, 61)
        age_dir(env, young, 30)
        n = await swept(env, active(a), [live_grant("art1", a)])
        assert n == 2 and young.is_dir() and not old.exists() and not bad.exists()
    sweeping(registry_pg, tmp_path, body)


def test_a_registered_prefix_is_exempt_from_the_agent_and_grant_rules(registry_pg, tmp_path):
    async def body(env, a):
        verdict = {"keep": True}
        seen = []

        async def keep(owner, meta):
            seen.append((owner, meta.upload_id))
            return verdict["keep"]
        env.applier.register_staging_owner("m_", keep)
        upl, d = stage(env, "m_x", "art1", "m.bin", BLOB[:50])      # no agent, no grant
        assert await swept(env, {}, []) == 0 and d.is_dir() and seen == [("m_x", upl)]
        verdict["keep"] = False
        assert await swept(env, {}, []) == 1 and not d.exists()
        _, d2 = stage(env, "m_x", "art1", "m2.bin", BLOB[:60])
        age_dir(env, d2, TTL + 1)
        verdict["keep"] = True
        assert await swept(env, {}, []) == 1 and not d2.exists()    # the TTL still applies
    sweeping(registry_pg, tmp_path, body)


def test_a_failing_keep_predicate_deletes_nothing(registry_pg, tmp_path):
    async def body(env, a):
        async def keep(owner, meta):
            raise RuntimeError(SENTINEL)
        env.applier.register_staging_owner("m_", keep)
        _, d = stage(env, "m_x", "art1", "m.bin", BLOB[:50])
        assert await swept(env, {}, []) == 0 and d.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_register_staging_owner_validates(registry_pg, tmp_path):
    async def body(env, a):
        async def keep(owner, meta):
            return True
        for bad in ("", "x_", "m", 5, "m_/"):
            with pytest.raises(ValueError):
                env.applier.register_staging_owner(bad, keep)
        with pytest.raises(ValueError):
            env.applier.register_staging_owner("m_", "nope")
        env.applier.register_staging_owner("m_", keep)
        with pytest.raises(ValueError):
            env.applier.register_staging_owner("m_", keep)
    sweeping(registry_pg, tmp_path, body)


@pytest.mark.parametrize("level", ["owner_dir", "upload_dir"])
def test_sweep_never_follows_a_symlinked_owner_or_upload_dir(registry_pg, tmp_path, level):
    async def body(env, a):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text(SENTINEL)
        upl, d = stage(env, a, "art1", "x.bin", BLOB[:50])
        age_dir(env, d, TTL + 1)
        if level == "owner_dir":
            link = env.outbox / "staging" / "evil"
            (outside / upl).mkdir()
            (outside / upl / "0000000").write_bytes(b"x")
            link.symlink_to(outside, target_is_directory=True)
        else:
            link = env.outbox / "staging" / a / cbf.upload_id_for(a, "art1", "link.bin", "3" * 64, 5)
            link.symlink_to(outside, target_is_directory=True)
        before = tree(outside)
        n = await swept(env, {}, [])
        assert n == 1 and not d.exists()                  # the real expired upload went; the link did not count
        assert link.is_symlink() and tree(outside) == before
        assert (outside / "sentinel").read_text() == SENTINEL
    sweeping(registry_pg, tmp_path, body)


def test_a_broken_entry_does_not_stop_the_sweep_and_the_count_is_returned(registry_pg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(env, a):
        broken = env.outbox / "staging" / a / cbf.upload_id_for(a, "art1", "z.bin", "4" * 64, 5)
        broken.mkdir(parents=True)
        (broken / "meta.json").write_text(SENTINEL)
        for i in range(cbf.tree_entry_cap(env.applier.limits) + 5):      # listing it trips the entry cap
            (broken / f"junk{i}").write_bytes(b"")
        age_dir(env, broken, TTL + 1)
        unreadable = env.outbox / "staging" / a / cbf.upload_id_for(a, "art1", "u.bin", "5" * 64, 5)
        unreadable.mkdir()
        (unreadable / "meta.json").write_text("{}")
        os.chmod(unreadable / "meta.json", 0)
        age_dir(env, unreadable, 100)
        _, d1 = stage(env, a, "art1", "e1.bin", BLOB[:50])
        _, d2 = stage(env, a, "art1", "e2.bin", BLOB[:60])
        age_dir(env, d1, TTL + 1)
        age_dir(env, d2, TTL + 1)
        n = await swept(env, active(a), [live_grant("art1", a)])
        assert n == 3 and broken.is_dir() and not d1.exists() and not d2.exists() and not unreadable.exists()
    sweeping(registry_pg, tmp_path, body, limits=BridgeLimits(max_file_bytes=CHUNK))
    assert SENTINEL not in caplog.text


def test_a_verified_upload_dir_is_swept_by_the_ttl_like_any_other(registry_pg, tmp_path):
    async def body(env, a):
        _, d = stage(env, a, "art1", "v.bin", BLOB[:50])
        for p in list(d.iterdir()):
            if p.name != "meta.json":
                p.unlink()
        (d / "verified").write_bytes(b"")
        age_dir(env, d, TTL + 1)
        assert await swept(env, active(a), [live_grant("art1", a)]) == 1 and not d.exists()
    sweeping(registry_pg, tmp_path, body)


def seed_mixed(env, a):
    """Four uploads of ``a``: a fresh one without a grant, an old one, an old metaless one, a fresh one with a grant."""
    _, no_grant = stage(env, a, "art_no_grant", "n.bin", BLOB[:50])
    _, old = stage(env, a, "art_ok", "o.bin", BLOB[:60])
    _, ok = stage(env, a, "art_ok", "k.bin", BLOB[:70])
    nometa = env.outbox / "staging" / a / cbf.upload_id_for(a, "art_ok", "m.bin", "6" * 64, 5)
    nometa.mkdir()
    age_dir(env, old, TTL + 1)
    age_dir(env, nometa, 100)
    return no_grant, old, ok, nometa


@pytest.mark.parametrize("case", ["stale", "missing", "grants_not_a_list", "agents_not_a_mapping"])
def test_without_a_proven_fresh_feed_only_the_ttl_and_grace_rules_apply(registry_pg, tmp_path, case):
    async def body(env, a):
        no_grant, old, ok, nometa = seed_mixed(env, a)
        agents, grants, kw = active(a), [live_grant("art_ok", a)], {}
        if case == "stale":
            kw = {"feed_age": cbf.FEED_MAX_AGE + 5}
        elif case == "missing":
            kw = {"feed_age": None}
        elif case == "grants_not_a_list":
            grants = None
        else:
            agents = None
        n = await swept(env, agents, grants, **kw)
        assert n == 2 and not old.exists() and not nometa.exists() and no_grant.is_dir() and ok.is_dir()
        # revoked and grantless, but nothing is proven: even then they stay
        agents = None if case == "agents_not_a_mapping" else {a: {"status": "revoked"}}
        grants = None if case == "grants_not_a_list" else []
        assert await swept(env, agents, grants, **kw) == 0 and no_grant.is_dir() and ok.is_dir()
        assert await swept(env, {a: {"status": "revoked"}}, []) == 2          # a fresh feed: now they go
    sweeping(registry_pg, tmp_path, body)


def test_an_intent_written_after_the_sweep_started_protects_its_upload(registry_pg, tmp_path):
    async def body(env, a):
        upl, d = stage(env, a, "art1", "blob.bin", BLOB)
        age_dir(env, d, TTL + 1)
        verdict = env.applier._stage_doomed

        async def doomed_then_intent(*args, **kw):
            out = await verdict(*args, **kw)
            assert out                              # doomed by the TTL when the verdict was taken
            env.intent(agent(a), kind="commit", artifact_id="art1",
                       payload={"base_version_id": "v", "message": "m", "changes": [staged("blob.bin", upl)]})
            return out
        env.applier._stage_doomed = doomed_then_intent
        assert await swept(env, active(a), [live_grant("art1", a)]) == 0 and d.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_a_grant_restored_after_the_snapshot_protects_the_upload(registry_pg, tmp_path):
    async def body(env, a):
        aid, _ = await seed(env)
        _, d = stage(env, a, aid, "blob.bin", BLOB[:50])
        verdict = env.applier._stage_doomed

        async def doomed_then_grant(*args, **kw):
            out = await verdict(*args, **kw)
            assert out                              # the stale snapshot says: no grant
            await grant(env, aid, a)
            return out
        env.applier._stage_doomed = doomed_then_grant
        assert await swept(env, active(a), []) == 0 and d.is_dir()
        env.applier._stage_doomed = verdict
        async with env.pool.acquire() as conn:
            await CollabGrantStore(conn).revoke(aid, a)
        assert await swept(env, active(a), []) == 1 and not d.exists()
    sweeping(registry_pg, tmp_path, body)


def test_an_agent_reactivated_after_the_snapshot_keeps_its_upload(registry_pg, tmp_path):
    async def body(env, a):
        _, d = stage(env, a, "art1", "blob.bin", BLOB[:50])
        verdict = env.applier._stage_doomed

        async def doomed_then_feed(*args, **kw):
            out = await verdict(*args, **kw)
            (env.outbox / "agents.json").write_text(json.dumps(
                {"generated_at": env.clock(), "agents": active(a)}))
            return out
        env.applier._stage_doomed = doomed_then_feed
        assert await swept(env, {a: {"status": "revoked"}}, [live_grant("art1", a)]) == 0 and d.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_an_intents_dir_over_its_cap_deletes_nothing(registry_pg, tmp_path, monkeypatch):
    monkeypatch.setattr("herdr_engineering.control_plane.collab_intents.INTENTS_MIN_ENTRIES", 3)

    async def body(env, a):
        _, d = stage(env, a, "art1", "blob.bin", BLOB[:50])
        age_dir(env, d, TTL + 1)
        for _ in range(12):
            env.intent(agent(uid("agent")))
        assert await swept(env, active(a), [live_grant("art1", a)]) == 0 and d.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_the_staging_owner_listing_over_its_cap_deletes_nothing(registry_pg, tmp_path, monkeypatch):
    monkeypatch.setattr("herdr_engineering.control_plane.collab_intents.STAGING_MIN_OWNERS", 2)

    async def body(env, a):
        _, d = stage(env, a, "art1", "blob.bin", BLOB[:50])
        age_dir(env, d, TTL + 1)
        for i in range(4):
            (env.outbox / "staging" / f"other{i}").mkdir()
        assert await swept(env, active(a), [live_grant("art1", a)]) == 0 and d.is_dir()
    sweeping(registry_pg, tmp_path, body)


def test_a_malformed_intent_file_is_ignored_next_to_a_valid_one(registry_pg, tmp_path):
    async def body(env, a):
        upl, d = stage(env, a, "art1", "keep.bin", BLOB[:50])
        _, gone = stage(env, a, "art1", "gone.bin", BLOB[:60])
        age_dir(env, d, TTL + 1)
        age_dir(env, gone, TTL + 1)
        (env.outbox / "intents" / (new_ticket() + ".json")).write_text("{not json")
        (env.outbox / "intents" / (new_ticket() + ".json")).write_bytes(b"x" * (2 * 1024 * 1024))      # oversize
        env.intent(agent(a), kind="commit", artifact_id="art1",
                   payload={"base_version_id": "v", "message": "m", "changes": [staged("keep.bin", upl)]})
        assert await swept(env, active(a), [live_grant("art1", a)]) == 1 and d.is_dir() and not gone.exists()
    sweeping(registry_pg, tmp_path, body)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
@pytest.mark.parametrize("what", ["intent_file", "intents_dir"])
def test_an_intent_that_cannot_be_read_right_now_deletes_nothing(registry_pg, tmp_path, what):
    async def body(env, a):
        _, d = stage(env, a, "art1", "blob.bin", BLOB[:50])
        age_dir(env, d, TTL + 1)
        raw = env.intent(agent(a))
        target = env.outbox / "intents" / (raw["ticket"] + ".json") if what == "intent_file" else env.outbox / "intents"
        mode = target.stat().st_mode
        os.chmod(target, 0)
        try:
            assert await swept(env, active(a), [live_grant("art1", a)]) == 0 and d.is_dir()
        finally:
            os.chmod(target, mode)
        assert await swept(env, active(a), [live_grant("art1", a)]) == 1
    sweeping(registry_pg, tmp_path, body)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
def test_a_meta_json_that_cannot_be_read_counts_as_invalid_after_the_grace(registry_pg, tmp_path):
    async def body(env, a):
        _, d = stage(env, a, "art1", "blob.bin", BLOB[:50])
        os.chmod(d / "meta.json", 0)
        age_dir(env, d, 30)
        assert await swept(env, active(a), [live_grant("art1", a)]) == 0 and d.is_dir()
        age_dir(env, d, 100)
        assert await swept(env, active(a), [live_grant("art1", a)]) == 1 and not d.exists()
    sweeping(registry_pg, tmp_path, body)


def test_a_symlinked_staging_root_is_not_followed(registry_pg, tmp_path):
    async def body(env, a):
        _, d = stage(env, a, "art1", "blob.bin", BLOB[:50])
        age_dir(env, d, TTL + 1)
        outside = tmp_path / "outside"
        (env.outbox / "staging").rename(outside)
        (env.outbox / "staging").symlink_to(outside, target_is_directory=True)
        before = tree(outside)
        assert await swept(env, {}, []) == 0 and tree(outside) == before
    sweeping(registry_pg, tmp_path, body)


def test_a_revoked_agents_empty_owner_dir_is_removed_too(registry_pg, tmp_path):
    async def body(env, a):
        b, c = uid("agent"), uid("agent")
        _, d_a = stage(env, a, "art1", "a.bin", BLOB[:50])
        _, d_b = stage(env, b, "art1", "b.bin", BLOB[:50])
        (env.outbox / "staging" / c).mkdir()
        upl, d_pend = stage(env, "pend", "art1", "p.bin", BLOB[:50])
        env.intent(agent("pend"), kind="commit", artifact_id="art1",
                   payload={"base_version_id": "v", "message": "m", "changes": [staged("p.bin", upl)]})
        n = await swept(env, active(b), [live_grant("art1", b)])
        staging = env.outbox / "staging"
        assert n == 1 and not d_a.exists() and not (staging / a).exists()           # revoked: upload and dir go
        assert d_b.is_dir() and (staging / b).is_dir()                              # active
        assert not (staging / c).exists()                                           # revoked and already empty
        assert d_pend.is_dir() and (staging / "pend").is_dir()                      # pinned by a pending intent
    sweeping(registry_pg, tmp_path, body)


def test_an_owner_with_more_uploads_than_the_scan_budget_is_still_swept(registry_pg, tmp_path):
    async def body(env, a):
        env.applier.staging_scan_entries = 4
        dirs = []
        for i in range(10):
            _, d = stage(env, a, "art1", f"f{i}.bin", BLOB[:50 + i])
            age_dir(env, d, TTL + 1)
            dirs.append(d)
        total = 0
        for _ in range(5):
            n = await swept(env, active(a), [live_grant("art1", a)])
            assert n <= 4
            total += n
        assert total == 10 and not any(d.exists() for d in dirs)
    sweeping(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- bridge integration (Task 7c)
def test_an_intent_written_by_the_bridge_commit_is_applied_and_read_back_through_result(registry_pg, tmp_path, svc):
    world = World(W(svc), tmp_path)
    seen = []

    async def handler(ctx, intent):
        seen.append((intent.actor, intent.artifact_id, intent.payload.base_version_id))
        return {"result": "created", "version_id": "ver_2", "version_number": 3}

    async def body(env):
        env.applier.register("commit", handler, replay_outcomes=frozenset({"created"}))
        out = await asyncio.to_thread(world.bridge.commit, world.a, commit_args("hello"))
        assert await asyncio.to_thread(world.bridge.result, world.a, out["ticket"], wait=0) == {"status": "pending"}
        counts = await env.tick()          # no env.feed(): the feed the worker checks is the one commit() refreshed
        assert counts["applied"] == 1 and counts["rejected"] == 0
        assert seen == [(Actor("agent", world.a.id), "art_x", "v_x2")]
        got = await asyncio.to_thread(world.bridge.result, world.a, out["ticket"], wait=0)
        assert got["result"] == "created" and got["version_id"] == "ver_2" and got["ticket"] == out["ticket"]
        assert got["actor"] == {"kind": "agent", "id": world.a.id}
        assert not env.pending(out["ticket"])
        other = await asyncio.to_thread(world.bridge.commit, world.a, commit_args("again"))   # the pending slot is free
        assert other["status"] == "pending"
    run(registry_pg, tmp_path, body, clock=world.clock)
