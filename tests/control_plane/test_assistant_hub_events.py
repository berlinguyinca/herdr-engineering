import json
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore
from herdr_engineering.control_plane.assistant_hub.events import (
    list_events,
    record_event,
    scrub_payload,
)
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
        rec(store, w, payload={"x": "a" * 4096})
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
    assert store.max_events_per_tenant == 1000
    store.max_events_per_tenant = 5
    w, other = worker(reg), worker(reg, tenant="t2", device="d2")
    keep = rec(store, other)
    ids = [rec(store, w, payload={"i": i}).event_id for i in range(8)]
    got = [e.event_id for e in list_events(store, tenant_id="t1", user_id="u1", limit=500)]
    assert got == ids[::-1][:5]
    assert [e.event_id for e in list_events(store, tenant_id="t2", user_id="u1")] == [keep.event_id]
    assert sum(1 for e in store.worker_events.values() if e.tenant_id == "t1") == 5


def test_scrub_masks_token_shaped_values_with_innocuous_keys():
    out = scrub_payload({
        "a": "sk-abcdefghijklmnop123456", "b": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "f": "Bearer abcdefghijklmnop12345", "w": "Worker hcw_abcdefghij12",
        "j": "eyJabcdefghijk.eyJabcdefghijk.sig", "m": "see hca_abcdefghij12 and hce_abcdefghij12 ok"})
    blob = json.dumps(out)
    for leak in ("sk-abc", "ghp_abc", "Bearer abc", "hcw_abc", "eyJabc", "hca_abc", "hce_abc"):
        assert leak not in blob
    assert out["m"] == "see ***REDACTED*** and ***REDACTED*** ok"


def test_scrub_recurses_lists_and_tuples():
    out = scrub_payload({"x": [{"y": ("sk-abcdefghijklmnop123456", "fine")}], "t": ("ghp_" + "a" * 30,)})
    assert out == {"x": [{"y": ["***REDACTED***", "fine"]}], "t": ["***REDACTED***"]}


def test_scrub_masks_secret_looking_keys_and_secret_named_subtrees():
    out = scrub_payload({"sk-abcdefghijklmnop123456": 1, "Cookie": {"a": ["x", {"b": "c"}]},
                         "API_KEY": ["one", "two"], "password": {"deep": "v"}})
    assert out == {"***REDACTED***": 1, "Cookie": "***REDACTED***", "API_KEY": "***REDACTED***",
                   "password": "***REDACTED***"}


def test_scrub_leaves_harmless_values_and_rejects_non_string_keys():
    harmless = {"n": 3, "ok": True, "none": None, "s": "hello world", "l": [1, "two", {"k": "v"}]}
    assert scrub_payload(harmless) == harmless
    with pytest.raises(ValueError):
        scrub_payload({1: "x"})
    with pytest.raises(ValueError):
        scrub_payload({"a": [{2: "x"}]})


def test_record_event_scrubs_stored_payload():
    store, reg = make()
    e = rec(store, worker(reg), payload={"a": "sk-abcdefghijklmnop123456"})
    assert e.payload == {"a": "***REDACTED***"}


def test_byte_budget_drops_oldest_and_spares_other_tenants():
    store, reg = make()
    w, other = worker(reg), worker(reg, tenant="t2", device="d2")
    keep = rec(store, other, payload={"x": "z" * 100})
    store.max_event_bytes_per_tenant = 450
    ids = [rec(store, w, payload={"x": "y" * 100}).event_id for _ in range(6)]
    got = [e.event_id for e in list_events(store, tenant_id="t1", user_id="u1", limit=500)]
    assert 0 < len(got) < 6 and got == ids[::-1][:len(got)]
    assert store._event_bytes["t1"] <= 450
    assert [e.event_id for e in list_events(store, tenant_id="t2", user_id="u1")] == [keep.event_id]


def test_index_rebuilt_after_restore_so_caps_still_apply():
    store, reg = make()
    w = worker(reg)
    for i in range(4):
        rec(store, w, payload={"i": i})
    fresh = AssistantStore()
    fresh.restore(store.snapshot())
    fresh.max_events_per_tenant = 3
    rec(fresh, w)
    assert len(list_events(fresh, tenant_id="t1", user_id="u1", limit=500)) == 3


def _nest(kind, depth, leaf="x"):
    v = leaf
    for i in range(depth):
        v = {"k": v} if kind == "dict" or (kind == "mixed" and i % 2) else [v]
    return v


@pytest.mark.parametrize("kind", ["dict", "list", "mixed"])
def test_deep_nesting_raises_value_error_not_recursion_error(kind):
    store, reg = make()
    for depth in (33, 5000):
        with pytest.raises(ValueError):
            scrub_payload(_nest(kind, depth))
    with pytest.raises(ValueError):
        rec(store, worker(reg), payload={"a": _nest(kind, 5000)})


def test_nesting_at_exact_limit_works():
    out = scrub_payload(_nest("dict", 32, leaf="sk-abcdefghijklmnop123456"))
    for _ in range(32):
        out = out["k"]
    assert out == "***REDACTED***"
    with pytest.raises(ValueError):
        scrub_payload(_nest("dict", 33))


def test_token_matching_is_case_insensitive():
    out = scrub_payload({"a": "bearer abcdefghijkl", "b": "Sk-abcdefghijklmnop123456", "Sk-abcdefghijklmnop123456": 1})
    assert out == {"a": "***REDACTED***", "b": "***REDACTED***", "***REDACTED***": 1}


def test_unicode_tricks_do_not_evade_scrubbing():
    out = scrub_payload({
        "a": "ｓｋ-abcdefghijklmnop123456",
        "b": "s​k-abcdefghijklmnop123456",
        "c": "gh⁠p_" + "a" * 30,
        "d": "Bea﻿rer abcdefghijkl",
        "s‍k-abcdefghijklmnop123456": 1})
    assert out == {"a": "***REDACTED***", "b": "***REDACTED***", "c": "***REDACTED***",
                   "d": "***REDACTED***", "***REDACTED***": 1}
    assert scrub_payload({"h": "café Ａ"}) == {"h": "café Ａ"}


def test_size_cap_measured_after_scrubbing():
    store, reg = make()
    w = worker(reg)
    e = rec(store, w, payload={"a": "sk-" + "a" * 5000})
    assert e.payload == {"a": "***REDACTED***"}
    with pytest.raises(ValueError):
        rec(store, w, payload={"a": "b" * 5000})
