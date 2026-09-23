"""Observability/security detector tests (spec 0170)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.observability import (
    MetricsRegistry,
    _decode_addr,
    _is_public_ip,
    detect_listeners,
)


def test_decode_addr_ipv4():
    assert _decode_addr("0100007F") == "127.0.0.1"
    assert _decode_addr("00000000") == "0.0.0.0"


def test_decode_addr_ipv6():
    assert _decode_addr("00000000000000000000000001000000", ipv6=True) == "::1"


def test_public_classification():
    assert _is_public_ip("127.0.0.1") is False
    assert _is_public_ip("10.0.0.1") is False
    assert _is_public_ip("192.168.1.1") is False
    assert _is_public_ip("100.64.0.1") is False  # tailscale CGNAT
    assert _is_public_ip("8.8.8.8") is True
    assert _is_public_ip("fd7a:115c:a1e0::1") is False  # tailnet ULA
    assert _is_public_ip("2606:4700::1111") is True


def test_metrics_registry():
    m = MetricsRegistry()
    m.inc("event_ingest_errors")
    m.inc("event_ingest_errors", 2)
    m.set("dev_lease_active", 3)
    s = m.snapshot()
    assert s["event_ingest_errors"] == 3
    assert s["dev_lease_active"] == 3


def test_detect_listeners_no_public():
    # loopback-only environment should not flag public listeners
    public = [f for f in detect_listeners() if f.public]
    assert public == []
