"""Read-only opt-in web egress policy validation; never runs UFW or mutates Docker.

Future deployment integration must use deploy.sh's existing rollout lock and
attempt snapshots. This module deliberately cannot activate its own policy.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys

PROJECT = "herdr-control-plane"
NETWORK = PROJECT + "_web-egress"
DEFAULT = PROJECT + "_default"
BRIDGE = "br-herdr-cp-eg"
CIDR = "172.31.252.0/29"
COMMENT = "herdr-cp-web-egress-v1"
LABELS = {
    "com.docker.compose.project": PROJECT,
    "com.docker.compose.network": "web-egress",
    "io.herdr.web-egress.policy": "v1",
}


def _refuse():
    raise ValueError("Invalid or conflicting web egress policy")


def _cidr(value):
    candidate = ipaddress.ip_network(value, strict=True)
    private = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if (
        candidate.version != 4
        or not 28 <= candidate.prefixlen <= 30
        or not any(candidate.subnet_of(ipaddress.ip_network(parent)) for parent in private)
    ):
        _refuse()
    return candidate


def gateway(value, addresses=None):
    """A trusted explicit private unicast /32; optionally prove host assignment."""
    network = ipaddress.ip_network(value, strict=True)
    allowed = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
    if (
        network.version != 4
        or network.prefixlen != 32
        or str(network) != value
        or not any(network.subnet_of(ipaddress.ip_network(parent)) for parent in allowed)
    ):
        _refuse()
    if addresses is not None and str(network.network_address) not in {
        entry.get("local")
        for interface in addresses
        for entry in interface.get("addr_info", [])
        if entry.get("family") == "inet"
    }:
        _refuse()
    return str(network)


def policy(
    *,
    enabled,
    cidr=CIDR,
    networks=(),
    routes=(),
    web=None,
    expected_network_id=None,
    require_attached=False,
    interfaces=(),
    destination=None,
    addresses=(),
):
    if not enabled:
        return {"enabled": False}
    candidate = _cidr(cidr)
    destination = gateway(destination, addresses)
    if not isinstance(web, dict) or not re.fullmatch(r"[0-9a-f]{64}", web.get("Id", "")):
        _refuse()
    labels = web.get("Config", {}).get("Labels") or {}
    if (
        labels.get("com.docker.compose.project") != PROJECT
        or labels.get("com.docker.compose.service") != "control-plane-web"
    ):
        _refuse()
    memberships = web.get("NetworkSettings", {}).get("Networks") or {}
    if DEFAULT not in memberships or set(memberships) - {DEFAULT, NETWORK}:
        _refuse()
    existing = [network for network in networks if network.get("Name") == NETWORK]
    if len(existing) > 1:
        _refuse()
    owned = existing[0] if existing else None
    if not owned and any(interface.get("ifname") == BRIDGE for interface in interfaces):
        _refuse()
    if owned:
        options, actual_labels = owned.get("Options") or {}, owned.get("Labels") or {}
        configs = owned.get("IPAM", {}).get("Config") or []
        if (
            not expected_network_id
            or owned.get("Id") != expected_network_id
            or not re.fullmatch(r"[0-9a-f]{64}", expected_network_id)
            or owned.get("Driver") != "bridge"
            or owned.get("Internal") is not False
            or owned.get("EnableIPv6") is not False
            or any(actual_labels.get(key) != value for key, value in LABELS.items())
            or options.get("com.docker.network.bridge.name") != BRIDGE
            or len(configs) != 1
            or configs[0].get("Subnet") != str(candidate)
            or configs[0].get("Gateway") != str(candidate.network_address + 1)
            or set(owned.get("Containers") or {}) - {web["Id"]}
        ):
            _refuse()
    elif expected_network_id or require_attached or NETWORK in memberships:
        _refuse()
    for network in networks:
        if owned is not None and network is owned:
            continue
        if (network.get("Options") or {}).get("com.docker.network.bridge.name") == BRIDGE:
            _refuse()
        for config in network.get("IPAM", {}).get("Config") or []:
            if config.get("Subnet"):
                subnet = ipaddress.ip_network(config["Subnet"], strict=True)
                if subnet.version == 4 and candidate.overlaps(subnet):
                    _refuse()
    for route in routes:
        route_destination = route.get("dst")
        if route_destination == "default":
            continue
        subnet = ipaddress.ip_network(route_destination, strict=False)
        if subnet.version != 4 or not candidate.overlaps(subnet):
            continue
        if not (
            owned
            and route.get("dev") == BRIDGE
            and route.get("protocol") == "kernel"
            and subnet.subnet_of(candidate)
        ):
            _refuse()
    attached = bool(
        owned and set(owned.get("Containers") or {}) == {web["Id"]} and NETWORK in memberships
    )
    if attached:
        endpoint = memberships[NETWORK]
        member = ipaddress.ip_interface(owned["Containers"][web["Id"]].get("IPv4Address", ""))
        if (
            endpoint.get("NetworkID") != owned["Id"]
            or member.network != candidate
            or endpoint.get("IPAddress") != str(member.ip)
            or endpoint.get("IPPrefixLen") != candidate.prefixlen
            or endpoint.get("Gateway") != str(candidate.network_address + 1)
            or member.ip
            in (
                candidate.network_address,
                candidate.broadcast_address,
                candidate.network_address + 1,
            )
        ):
            _refuse()
    if require_attached:
        priority = memberships.get(NETWORK, {}).get("GwPriority")
        if not attached or priority != 100 or memberships[DEFAULT].get("GwPriority", 0) >= priority:
            _refuse()
    allow = [
        "ufw",
        "allow",
        "in",
        "on",
        BRIDGE,
        "proto",
        "tcp",
        "from",
        str(candidate),
        "to",
        destination,
        "port",
        "443",
        "comment",
        COMMENT,
    ]
    return {
        "enabled": True,
        "cidr": str(candidate),
        "bridge": BRIDGE,
        "network": NETWORK,
        "network_id": owned["Id"] if owned else None,
        "web_id": web["Id"],
        "membership_verified": attached,
        "ufw_allow": allow,
        "ufw_delete": ["ufw", "delete", *allow[1:]],
        "requires_deploy_lock": True,
        "activation_ready": False,
        "teardown_order": [
            "remove_owned_rule",
            "restore_previous_compose_membership",
            "remove_owned_network_if_empty",
        ],
    }


def _json(command):
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def collect():
    # Do not inspect environment, mounts, credentials, fleet files or HTTP bodies.
    ids = subprocess.run(
        ["docker", "network", "ls", "-q"], capture_output=True, text=True, check=True, timeout=15
    ).stdout.split()
    networks = _json(["docker", "network", "inspect", *ids]) if ids else []
    template = (
        '{"Id":{{json .Id}},"Config":{"Labels":{{json .Config.Labels}}},'
        '"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}'
    )
    web = _json(["docker", "inspect", "--format", template, "herdr-cp-web"])
    routes = []
    for family in ("-4", "-6"):
        routes.extend(_json(["ip", "-j", family, "route", "show", "table", "all"]))
    interfaces = _json(["ip", "-j", "link", "show"])
    addresses = _json(["ip", "-j", "-4", "address", "show"])
    return networks, routes, web, interfaces, addresses


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enabled", action="store_true", help="Inspect and print proposed policy only"
    )
    parser.add_argument("--cidr", default=CIDR)
    parser.add_argument("--gateway-ip", help="Explicit assigned private gateway IPv4 /32")
    parser.add_argument("--expected-network-id")
    parser.add_argument("--require-attached", action="store_true")
    args = parser.parse_args(argv)
    if not args.enabled:
        print(json.dumps({"enabled": False}))
        return 0
    if sys.platform != "linux":
        print("Web egress enabled preflight requires a Linux host.", file=sys.stderr)
        return 1
    try:
        gateway(args.gateway_ip)
        # Reject unsafe input before invoking any inspection command.
        _cidr(args.cidr)
        networks, routes, web, interfaces, addresses = collect()
        result = policy(
            enabled=True,
            cidr=args.cidr,
            networks=networks,
            routes=routes,
            web=web,
            expected_network_id=args.expected_network_id,
            require_attached=args.require_attached,
            interfaces=interfaces,
            destination=args.gateway_ip,
            addresses=addresses,
        )
    except (ValueError, TypeError, KeyError, OSError, AttributeError, subprocess.SubprocessError):
        print("Web egress policy refused; no changes made.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
