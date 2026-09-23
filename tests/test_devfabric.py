"""Transparent dev fabric tests (spec 0110)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.devfabric import DevServiceRegistry, _add_seconds
from herdr_engineering.errors import ConflictError


def test_collision_free_port_allocation(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    l1 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=8080,
                    owner_process_id="p1")
    l2 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=8081,
                    owner_process_id="p2")
    assert l1.external_port != l2.external_port


def test_idempotent_registration(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    l1 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=9000,
                    owner_herdr_session_id="s1", owner_process_id="p1")
    l2 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=9000,
                    owner_herdr_session_id="s1", owner_process_id="p1")
    assert l1.id == l2.id  # same lease renewed, not duplicated


def test_ttl_heartbeat_reconcile(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json", lease_ttl_seconds=60)
    lease = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=7000)
    # simulate expiry by setting expires_at in the past
    lease.expires_at = _add_seconds(lease.expires_at, -120)
    expired = r.reconcile()
    assert lease.id in expired
    assert r.active_leases() == []
    # heartbeat revives
    hb = r.heartbeat(lease.id)
    assert hb is not None and hb.state == "active"


def test_resolve_port(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=5000)
    assert r.resolve_port(lease.external_port).id == lease.id


def test_close(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=5001)
    assert r.close(lease.id) is True
    assert r.active_leases() == []


def test_port_exhaustion_raises_conflict(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json", port_range=(18000, 18001))
    r.register(target_machine_id="a", target_host="h", target_port=1, owner_process_id="p1")
    r.register(target_machine_id="a", target_host="h", target_port=2, owner_process_id="p2")
    with pytest.raises(ConflictError):
        r.register(target_machine_id="a", target_host="h", target_port=3,
                   owner_process_id="p3")
