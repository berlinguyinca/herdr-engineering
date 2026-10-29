"""Publish the fabric's public name in Route 53 (A records only, tailnet addresses only).

``local.metabolomics.us`` and ``*.local.metabolomics.us`` resolve to the gateway's tailnet IP(s): publicly
resolvable, but reachable only from inside the tailnet. IPv6 is deliberately not published, because the router binds
IPv4 only and a AAAA answer would make dual-stack clients stall on a dead address.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_TTL = 60   # low: failover is DNS round-robin, so a dead host should age out quickly


class DnsError(RuntimeError):
    pass


def validate_ips(ips: Iterable[str]) -> list[str]:
    """Only tailnet (CGNAT) addresses may be published; a public or LAN IP would leak or misroute the name."""
    out: list[str] = []
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise DnsError(f"not an IP address: {raw!r}") from exc
        if ip.version != 4 or ip not in TAILNET_V4:
            raise DnsError(f"{raw} is not a tailnet IPv4 address (100.64.0.0/10); refusing to publish it")
        out.append(str(ip))
    if not out:
        raise DnsError("at least one gateway IP is required")
    return list(dict.fromkeys(out))


def record_names(domain: str) -> list[str]:
    d = domain.lower().strip(".")
    return [d, f"*.{d}"]


def change_batch(domain: str, ips: Iterable[str], ttl: int = DEFAULT_TTL) -> dict[str, Any]:
    values = [{"Value": ip} for ip in validate_ips(ips)]
    return {"Comment": "herdr fabric gateway", "Changes": [
        {"Action": "UPSERT", "ResourceRecordSet": {"Name": f"{name}.", "Type": "A", "TTL": ttl, "ResourceRecords": values}}
        for name in record_names(domain)]}


def find_zone(client, domain: str) -> str:
    """Hosted zone id of the longest public zone that contains ``domain``."""
    d = domain.lower().strip(".")
    best: tuple[int, str] | None = None
    for page in client.get_paginator("list_hosted_zones").paginate():
        for zone in page["HostedZones"]:
            if zone.get("Config", {}).get("PrivateZone"):
                continue
            zname = zone["Name"].lower().rstrip(".")
            if (d == zname or d.endswith("." + zname)) and (best is None or len(zname) > best[0]):
                best = (len(zname), zone["Id"].split("/")[-1])
    if best is None:
        raise DnsError(f"no public hosted zone contains {d}")
    return best[1]


def current(client, zone_id: str, domain: str) -> dict[str, list[str]]:
    """The A values presently published for the apex and wildcard."""
    out: dict[str, list[str]] = {}
    for name in record_names(domain):
        resp = client.list_resource_record_sets(HostedZoneId=zone_id, StartRecordName=f"{name}.", StartRecordType="A", MaxItems="1")
        out[name] = []
        for rr in resp.get("ResourceRecordSets", []):
            if rr["Name"].rstrip(".").replace("\\052", "*") == name and rr["Type"] == "A":
                out[name] = sorted(r["Value"] for r in rr.get("ResourceRecords", []))
    return out


def publish(client, domain: str, ips: Iterable[str], *, ttl: int = DEFAULT_TTL, dry_run: bool = False,
            zone_id: str | None = None) -> dict[str, Any]:
    batch = change_batch(domain, ips, ttl)          # validates before any API call
    zone = zone_id or find_zone(client, domain)
    want = sorted(r["Value"] for r in batch["Changes"][0]["ResourceRecordSet"]["ResourceRecords"])
    have = current(client, zone, domain)
    result: dict[str, Any] = {"zone": zone, "names": record_names(domain), "ips": want, "ttl": ttl,
                              "previous": have, "changed": any(v != want for v in have.values())}
    if dry_run or not result["changed"]:
        result["applied"] = False
        return result
    client.change_resource_record_sets(HostedZoneId=zone, ChangeBatch=batch)
    result["applied"] = True
    return result


def route53_client():
    try:
        import boto3
    except ImportError as exc:   # pragma: no cover - environment dependent
        raise DnsError("boto3 is required for DNS publishing: pip install 'herdr-engineering[acme]'") from exc
    return boto3.client("route53")
