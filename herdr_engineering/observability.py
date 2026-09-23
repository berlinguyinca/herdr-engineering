"""Observability, metrics and security detection (spec 0170).

Correlated IDs across machine/session/mission/worktree/dev lease/test/CI/
artifact. In-memory metrics counters plus a security listener detector that
reports any public (non-loopback, non-tailnet) listener as a finding. No
secrets or hidden reasoning in telemetry.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from typing import Any

METRIC_NAMES = [
    "machine_reconnects", "plugin_capability_healthy", "event_ingest_lag_sec",
    "event_ingest_errors", "event_dedupes", "artifact_publish_failures",
    "dev_lease_allocations", "dev_lease_active", "dev_route_health",
    "dev_lease_expirations", "test_adapter_runs", "test_adapter_errors",
    "ci_api_errors", "ci_stale_views", "web_latency_ms", "websocket_reconnects",
    "notification_deliveries", "notification_dedupes", "lock_drift",
]


@dataclass
class MetricsRegistry:
    counters: dict[str, int] = field(default_factory=lambda: {k: 0 for k in METRIC_NAMES})

    def inc(self, name: str, by: int = 1) -> None:
        if name in self.counters:
            self.counters[name] += by
        else:
            self.counters[name] = by

    def set(self, name: str, value: int) -> None:
        self.counters[name] = value

    def snapshot(self) -> dict[str, int]:
        return dict(self.counters)


@dataclass
class ListenerFinding:
    address: str
    port: int
    public: bool
    process: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "port": self.port,
                "public": self.public, "process": self.process}


def _decode_addr(addr_hex: str, ipv6: bool = False) -> str:
    """Decode a /proc/net/tcp{6} local address (stored little-endian).

    IPv4: 4 bytes little-endian. IPv6: 4 little-endian 32-bit words.
    """
    try:
        raw = bytes.fromhex(addr_hex)
        if ipv6:
            words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)]
            raw = b"".join(words)
            return socket.inet_ntop(socket.AF_INET6, raw)
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    except (ValueError, OSError):
        return "0.0.0.0"


def _is_public_ip(ip: str) -> bool:
    """Return True only for addresses reachable from the public internet.

    Loopback, RFC1918, link-local, CGNAT, the Tailscale IPv4 CGNAT range and
    the Tailscale IPv6 ULA (fd7a:115c:a1e0::/48) are private.
    """
    if ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        return False
    if ":" in ip:
        # IPv6: link-local fe80::/10, unique-local fc00::/7, tailscale ULA
        low = ip.lower()
        if low.startswith("fe80") or low.startswith("fc") or low.startswith("fd"):
            return False
        if low.startswith("fd7a:115c:a1e0"):
            return False
        return True
    try:
        ipaddr = socket.inet_aton(ip)
    except OSError:
        return True
    first = ipaddr[0]
    if first == 10 or first == 127:
        return False
    if first == 172 and 16 <= ipaddr[1] <= 31:
        return False
    if first == 192 and ipaddr[1] == 168:
        return False
    if first == 169 and ipaddr[1] == 254:
        return False
    if first == 100 and 64 <= ipaddr[1] <= 127:
        return False  # CGNAT / Tailscale IPv4
    return True


def detect_listeners(include_tailscale: bool = True) -> list[ListenerFinding]:
    """Detect TCP listeners via /proc/net/tcp (Linux). Public listeners are
    flagged unless on the tailscale interface (100.x.x.x)."""
    findings: list[ListenerFinding] = []
    proc_paths = ["/proc/net/tcp", "/proc/net/tcp6"]
    for path in proc_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                lines = f.readlines()[1:]
            for line in lines:
                parts = line.split()
                if len(parts) < 4:
                    continue
                local = parts[1]  # HEX:HEX
                state = parts[3]
                if state != "0A":   # only LISTEN
                    continue
                addr_hex, port_hex = local.split(":")
                port = int(port_hex, 16)
                ip = _decode_addr(addr_hex, ipv6=path.endswith("6"))
                public = _is_public_ip(ip)
                findings.append(ListenerFinding(address=ip, port=port, public=public))
        except OSError:
            continue
    return findings
