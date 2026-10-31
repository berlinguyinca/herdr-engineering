import json
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore
from herdr_engineering.control_plane.assistant_hub.events import list_events, record_event
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry


def make():
    store = AssistantStore()
    reg = WorkerRegistry(store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30),
                         now=lambda: datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    return store, reg


def worker(reg, tenant="t1", user="u1", device="d1"):
    _, code = reg.create_enrollment(tenant_id=tenant, user_id=user, name="box", harness="pi")
    return reg.redeem(code, device_id=device)[0]


def rec(store, w, **kw):
    args = {"kind": "report", "provenance": "worker_reported", "session_id": "s1", "payload": {"a": 1}}
    args.update(kw)
    return record_event(store, w, **args)


def test_record_and_list_scoped_to_tenant_user():
    store, reg = make()
    w1, w2 = worker(reg), worker(reg, user="u2", device="d2")
    e1 = rec(store, w1)
    rec(store, w2)
    rec(store, worker(reg, tenant="t2", device="d3"))
    assert (e1.tenant_id, e1.user_id, e1.worker_id) == ("t1", "u1", w1.worker_id)
    got = list_events(store, tenant_id="t1", user_id="u1")
    assert [e.event_id for e in got] == [e1.event_id]
    assert list_events(store, tenant_id="t1", user_id="u1", worker_id="nope") == []
    assert list_events(store, tenant_id="t1", user_id="u1", kind="ask") == []


def test_newest_first_and_limit_capped():
    store, reg = make()
    w = worker(reg)
    ids = [rec(store, w, payload={"i": i}).event_id for i in range(5)]
    got = list_events(store, tenant_id="t1", user_id="u1", limit=3)
    assert [e.event_id for e in got] == ids[::-1][:3]
    assert len(list_events(store, tenant_id="t1", user_id="u1", limit=10**6)) == 5


def test_payload_secrets_redacted():
    store, reg = make()
    e = rec(store, worker(reg), payload={"token": "hcw_supersecretvalue", "api_key": "sk-abcdefghijklmnop123456"})
    blob = json.dumps(e.payload)
    assert "hcw_supersecretvalue" not in blob and "sk-abcdefghijklmnop123456" not in blob


def test_oversize_payload_rejected():
    store, reg = make()
    w = worker(reg)
    with pytest.raises(ValueError):
        rec(store, w, payload={"x": "a" * (16 * 1024)})
    assert list_events(store, tenant_id="t1", user_id="u1") == []
    rec(store, w, payload={"x": "a" * 1000})


def test_unknown_kind_or_provenance_rejected():
    store, reg = make()
    w = worker(reg)
    for kw in ({"kind": "approve"}, {"provenance": "owner"}, {"session_id": ""}, {"session_id": "s" * 129},
               {"session_id": 5}):
        with pytest.raises(ValueError):
            rec(store, w, **kw)


def test_payload_claims_do_not_change_provenance():
    store, reg = make()
    e = rec(store, worker(reg), payload={"provenance": "harness_attested", "note": "owner approved"})
    assert e.provenance == "worker_reported"


def test_events_survive_snapshot_restore():
    store, reg = make()
    e = rec(store, worker(reg), kind="ask", provenance="harness_attested")
    fresh = AssistantStore()
    fresh.restore(json.loads(json.dumps(store.snapshot())))
    got = list_events(fresh, tenant_id="t1", user_id="u1")
    assert got == [e]
    snap = store.snapshot()
    snap.pop("worker_events")
    AssistantStore().restore(snap)


def test_retention_cap_per_tenant():
    store, reg = make()
    assert store.max_events_per_tenant == 10_000
    store.max_events_per_tenant = 5
    w, other = worker(reg), worker(reg, tenant="t2", device="d2")
    keep = rec(store, other)
    ids = [rec(store, w, payload={"i": i}).event_id for i in range(8)]
    got = [e.event_id for e in list_events(store, tenant_id="t1", user_id="u1", limit=500)]
    assert got == ids[::-1][:5]
    assert [e.event_id for e in list_events(store, tenant_id="t2", user_id="u1")] == [keep.event_id]
    assert sum(1 for e in store.worker_events.values() if e.tenant_id == "t1") == 5
