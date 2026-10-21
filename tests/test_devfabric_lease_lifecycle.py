"""Regression tests for the devfabric lease lifecycle (issues #89, #90).

#89 — registry/heartbeat robustness:
  * one process's persist must not clobber a lease a concurrent process
    just wrote (per-lease merge at write time, #89 point 1);
  * a heartbeat issued against a stale base (the record moved under the
    caller) must not renew from that base (#89 suggested direction);
  * the serve renewal loop must be LOUD when a lease is missing from the
    registry or lapsed between cycles — silent expiry is how an origin
    disappears fleet-wide with no log line (#89 point 2).

#90 — forwarder/lease identity:
  * route drift updates the lease in place (same id + same external port)
    instead of close+re-register churn;
  * a failed forwarder start leaves the slot retryable for the next sync
    instead of giving up for the life of the process.
"""
import logging
import socket

import pytest

from herdr_engineering.cli import _enroll_origin
from herdr_engineering.contracts import Route
from herdr_engineering.devfabric import (DevServiceRegistry, OriginEdge,
                                         _add_seconds, _now_iso)

_RNG = (24000, 24999)


def _reg(tmp_path, name="leases.json"):
    return DevServiceRegistry(store_path=tmp_path / name, port_range=_RNG)


# -- #89: registry robustness -----------------------------------------------

def test_persist_merges_concurrent_external_lease(tmp_path):
    """A persist from one registry instance must not clobber a lease that a
    concurrent instance (another process on the same store) just wrote."""
    store = tmp_path / "leases.json"
    a = DevServiceRegistry(store_path=store, port_range=_RNG)
    la = a.register(target_machine_id="m1", target_host="127.0.0.1",
                    target_port=8101)
    # A second "process" opens the same store after A's write and registers
    # its own lease.
    b = DevServiceRegistry(store_path=store, port_range=_RNG)
    lb = b.register(target_machine_id="m2", target_host="127.0.0.1",
                    target_port=8102)
    assert b.get(la.id) is not None  # B saw A's lease
    # A heartbeats its own lease; A's in-memory state predates B's write.
    a.heartbeat(la.id)
    # A's persist must merge B's concurrent lease, not clobber it (#89).
    c = DevServiceRegistry(store_path=store, port_range=_RNG)
    assert c.get(la.id) is not None
    assert c.get(lb.id) is not None, (
        "A's persist clobbered B's concurrently-written lease")


def test_heartbeat_guard_rejects_stale_base(tmp_path, caplog):
    """A heartbeat whose caller observed an older `renewed_at` than the record
    now has (another process renewed it) must not renew from the stale base —
    the record's newer state is kept and the caller can retry from a fresh
    read."""
    reg = _reg(tmp_path)
    lease = reg.register(target_machine_id="m", target_host="127.0.0.1",
                         target_port=8201)
    stale_base = lease.renewed_at
    # Another process renews the record under us.
    other = _add_seconds(_now_iso(), 5)
    reg._leases[lease.id].renewed_at = other
    reg._leases[lease.id].expires_at = _add_seconds(other, reg._ttl)
    with caplog.at_level(logging.WARNING):
        result = reg.heartbeat(lease.id, expected_renewed_at=stale_base)
    assert result is not None
    assert reg.get(lease.id).renewed_at == other, (
        "stale-base heartbeat clobbered the newer renewal")


def test_renewal_loop_warns_on_missing_and_lapsed_lease(tmp_path, caplog):
    """The --serve renewal loop must log loudly (not silently pass) when a
    lease is missing from the registry, and when a lease lapsed between
    cycles (expired while the forwarder still held the port). The lapsed
    lease must still be renewed."""
    from herdr_engineering.cli import heartbeat_local_leases
    reg = _reg(tmp_path)
    lm = reg.register(target_machine_id="m", target_host="127.0.0.1",
                      target_port=8301)
    ll = reg.register(target_machine_id="m", target_host="127.0.0.1",
                      target_port=8302)
    # lm: lapsed between cycles (expired while the forwarder held the port).
    reg._leases[lm.id].expires_at = _add_seconds(_now_iso(), -60)
    reg._leases[lm.id].state = "expired"
    # ll: gone from the registry (clobbered/pruned by another process).
    del reg._leases[ll.id]
    with caplog.at_level(logging.WARNING):
        heartbeat_local_leases(
            reg, {"a": {"lease_id": lm.id}, "b": {"lease_id": ll.id}})
    assert reg.get(lm.id).state == "active", "lapsed lease was renewed"
    msg = caplog.text
    assert lm.id in msg and "lapsed" in msg
    assert ll.id in msg and "missing" in msg


# -- #90: lease identity stability -------------------------------------------

def test_update_routes_keeps_lease_identity(tmp_path):
    """update_routes mutates the lease in place: same id, same external port,
    persisted (no close+re-register churn)."""
    reg = _reg(tmp_path)
    lease = reg.register(
        target_machine_id="m", target_host="127.0.0.1", target_port=8401,
        routes=[Route("/api", "127.0.0.1", 8402)], preferred_port=24500)
    assert lease.external_port == 24500
    updated = reg.update_routes(
        lease.id, [Route("/api", "127.0.0.1", 8402),
                   Route("/ws", "127.0.0.1", 8403)])
    assert updated is not None
    assert updated.id == lease.id
    assert updated.external_port == lease.external_port == 24500
    assert len(updated.routes) == 2
    again = _reg(tmp_path)
    assert len(again.get(lease.id).routes) == 2, "in-place update persisted"


def test_enroll_origin_route_drift_keeps_lease_and_port(tmp_path):
    """Re-enrolling with drifted routes must keep the lease identity (id AND
    external port) even while the old forwarder still holds the port — the
    close+re-register path used to churn the port (allocation sees the old
    port still OS-bound) and leave the operator with a stale origin."""
    reg = _reg(tmp_path)
    spec = {"machine": "m", "repo": "r", "label": "app", "protocol": "http",
            "origin_port": 24600,
            "catchall": {"host": "127.0.0.1", "port": 8501},
            "routes": [{"prefix": "/api", "host": "127.0.0.1", "port": 8502}]}
    out1, lease1 = _enroll_origin(spec, None, reg, bind="127.0.0.1")
    assert out1["via"] == "local" and out1["changed"]
    edge = OriginEdge(lease1, bind_host="127.0.0.1").start()
    try:
        spec2 = dict(spec, routes=[
            {"prefix": "/api", "host": "127.0.0.1", "port": 8502},
            {"prefix": "/ws", "host": "127.0.0.1", "port": 8503}])
        out2, lease2 = _enroll_origin(spec2, None, reg, bind="127.0.0.1")
        assert out2["changed"] is True
        assert lease2.id == lease1.id, "stable lease id churned"
        assert lease2.external_port == lease1.external_port == 24600, (
            "route drift churned the external port")
        # The running edge sees the drift without a restart (same lease
        # object, routes read per connection).
        assert edge.lease is lease2 or edge.lease.id == lease2.id
        assert [ (r.prefix, r.host, r.port) for r in edge.lease.routes
                 ] == [("/api", "127.0.0.1", 8502), ("/ws", "127.0.0.1", 8503)]
    finally:
        edge.stop()


def test_register_renew_applies_route_drift_in_place(tmp_path):
    """The gateway path: re-registering a stable lease id with changed routes
    (raw dicts, as the gateway API passes them) must update the routes IN
    PLACE — same id, same port, normalized to Route objects — instead of
    silently keeping the stale routes."""
    reg = _reg(tmp_path)
    l1 = reg.register(target_machine_id="m", target_host="127.0.0.1",
                      target_port=8701, lease_id="lease_stable1",
                      routes=[{"prefix": "/api", "host": "127.0.0.1", "port": 8702}],
                      preferred_port=24800)
    l2 = reg.register(target_machine_id="m", target_host="127.0.0.1",
                      target_port=8701, lease_id="lease_stable1",
                      routes=[{"prefix": "/api", "host": "127.0.0.1", "port": 8702},
                              {"prefix": "/ws", "host": "127.0.0.1", "port": 8703}],
                      preferred_port=24800)
    assert l2.id == l1.id == "lease_stable1"
    assert l2.external_port == l1.external_port == 24800
    assert len(l2.routes) == 2, "stale routes kept on renew"
    assert all(not isinstance(r, dict) for r in l2.routes)


def test_failed_forwarder_start_is_retried_on_next_sync(tmp_path):
    """A forwarder start that fails (e.g. the port is still OS-bound) must
    leave the slot retryable: the NEXT sync starts it once the port frees,
    instead of remembering the failure for the life of the process."""
    from herdr_engineering.cli import _ensure_origin_forwarder
    reg = _reg(tmp_path)
    lease = reg.register(target_machine_id="m", target_host="127.0.0.1",
                         target_port=8601, preferred_port=24700)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 24700))
    blocker.listen(1)
    fwd: dict = {}
    err = _ensure_origin_forwarder(fwd, "app", lease, "127.0.0.1")
    assert err is not None, "start failure must be reported"
    assert fwd.get("app") is None, "failed slot must stay retryable"
    blocker.close()
    err2 = _ensure_origin_forwarder(fwd, "app", lease, "127.0.0.1")
    assert err2 is None and fwd.get("app") is not None
    assert fwd["app"].bound
    fwd["app"].stop()
