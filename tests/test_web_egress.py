"""Opt-in policy validation never grants an entire Docker subnet host access."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from herdr_engineering import web_egress as egress


@pytest.fixture
def facts():
    web = {
        "Id": "a" * 64,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "herdr-control-plane",
                "com.docker.compose.service": "control-plane-web",
            }
        },
        "NetworkSettings": {"Networks": {"herdr-control-plane_default": {"GwPriority": 0}}},
    }
    networks = [
        {
            "Id": "b" * 64,
            "Name": "herdr-control-plane_default",
            "Driver": "bridge",
            "Internal": False,
            "Labels": {},
            "Options": {},
            "Containers": {},
            "IPAM": {"Config": [{"Subnet": "172.16.4.0/24"}]},
        }
    ]
    routes = [
        {"dst": "default", "dev": "eth0"},
        {"dst": "100.64.0.0/10", "dev": "tailscale0", "table": 52},
        {"dst": "172.16.4.0/24", "dev": "br-default"},
        {"dst": "fd7a:115c:a1e0::/48"},
    ]
    return networks, routes, web


def policy(facts, **kwargs):
    networks, routes, web = facts
    return egress.policy(
        enabled=True,
        cidr="172.31.252.0/29",
        networks=networks,
        routes=routes,
        web=web,
        destination="100.104.39.6/32",
        addresses=[{"addr_info": [{"local": "100.104.39.6", "family": "inet"}]}],
        **kwargs,
    )


def attach(facts):
    networks, routes, web = facts
    network = {
        "Id": "c" * 64,
        "Name": "herdr-control-plane_web-egress",
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "Labels": {
            "com.docker.compose.project": "herdr-control-plane",
            "com.docker.compose.network": "web-egress",
            "io.herdr.web-egress.policy": "v1",
        },
        "Options": {"com.docker.network.bridge.name": "br-herdr-cp-eg"},
        "IPAM": {"Config": [{"Subnet": "172.31.252.0/29", "Gateway": "172.31.252.1"}]},
        "Containers": {web["Id"]: {"Name": "herdr-cp-web", "IPv4Address": "172.31.252.2/29"}},
    }
    networks.append(network)
    routes.extend(
        [
            {"dst": "172.31.252.0/29", "dev": "br-herdr-cp-eg", "protocol": "kernel"},
            {"dst": "172.31.252.1", "dev": "br-herdr-cp-eg", "protocol": "kernel", "type": "local"},
        ]
    )
    web["NetworkSettings"]["Networks"]["herdr-control-plane_web-egress"] = {
        "GwPriority": 100,
        "NetworkID": "c" * 64,
        "IPAddress": "172.31.252.2",
        "IPPrefixLen": 29,
        "Gateway": "172.31.252.1",
    }
    return network


def test_no_opt_in_collects_nothing_and_emits_no_policy(capsys):
    with patch.object(egress, "collect", side_effect=AssertionError("must stay disabled")):
        assert egress.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}


def test_exact_host_input_rule_and_owned_removal(facts):
    result = policy(facts)
    assert result["ufw_allow"] == [
        "ufw",
        "allow",
        "in",
        "on",
        "br-herdr-cp-eg",
        "proto",
        "tcp",
        "from",
        "172.31.252.0/29",
        "to",
        "100.104.39.6/32",
        "port",
        "443",
        "comment",
        "herdr-cp-web-egress-v1",
    ]
    assert result["ufw_delete"] == ["ufw", "delete", *result["ufw_allow"][1:]]
    assert result["activation_ready"] is False
    assert result["requires_deploy_lock"] is True
    assert result["teardown_order"] == [
        "remove_owned_rule",
        "restore_previous_compose_membership",
        "remove_owned_network_if_empty",
    ]


@pytest.mark.parametrize(
    "route",
    [
        {"dst": "172.31.0.0/16", "dev": "tailscale0", "table": 52},
        {"dst": "172.31.252.4/32", "dev": "vpn0", "table": 100},
        {"dst": "172.31.252.0/29", "dev": "other-bridge"},
        {"dst": "128.0.0.0/1", "dev": "vpn0"},
        {"dst": "malformed"},
    ],
)
def test_host_vpn_route_conflicts_refuse(facts, route):
    facts[1].append(route)
    with pytest.raises(ValueError):
        policy(facts)


def test_docker_subnet_conflict_refuses_even_without_host_route(facts):
    facts[0].append(
        {"Name": "foreign", "Id": "d" * 64, "IPAM": {"Config": [{"Subnet": "172.31.252.0/28"}]}}
    )
    with pytest.raises(ValueError):
        policy(facts)


@pytest.mark.parametrize(
    "cidr",
    ["0.0.0.0/0", "100.64.0.0/29", "127.0.0.0/29", "172.31.252.1/29", "172.31.0.0/16", "::/0"],
)
def test_noncanonical_or_broad_cidr_refused(facts, cidr):
    with pytest.raises(ValueError):
        egress.policy(enabled=True, cidr=cidr, networks=facts[0], routes=facts[1], web=facts[2])


def test_explicit_alternate_small_private_cidr(facts):
    result = egress.policy(
        enabled=True,
        cidr="172.30.254.0/29",
        networks=facts[0],
        routes=facts[1],
        web=facts[2],
        destination="100.104.39.6/32",
        addresses=[{"addr_info": [{"local": "100.104.39.6", "family": "inet"}]}],
    )
    assert result["cidr"] == "172.30.254.0/29"
    assert "172.30.254.0/29" in result["ufw_allow"]


def test_verified_network_identity_and_connected_routes(facts):
    attach(facts)
    result = policy(facts, expected_network_id="c" * 64, require_attached=True)
    assert result["network_id"] == "c" * 64
    assert result["membership_verified"] is True
    assert result["activation_ready"] is False  # route and endpoint proof is still a separate gate


@pytest.mark.parametrize(
    "change",
    ["id", "label", "bridge", "subnet", "foreign", "wrong-service", "priority", "missing-default"],
)
def test_existing_network_collision_and_membership_refused(facts, change):
    network = attach(facts)
    if change == "id":
        network["Id"] = "d" * 64
    elif change == "label":
        network["Labels"]["com.docker.compose.project"] = "other"
    elif change == "bridge":
        network["Options"]["com.docker.network.bridge.name"] = "br-foreign"
    elif change == "subnet":
        network["IPAM"]["Config"][0]["Subnet"] = "172.30.0.0/29"
    elif change == "foreign":
        network["Containers"]["d" * 64] = {"Name": "collab-public"}
    elif change == "wrong-service":
        facts[2]["Config"]["Labels"]["com.docker.compose.service"] = "share-public"
    elif change == "priority":
        facts[2]["NetworkSettings"]["Networks"]["herdr-control-plane_default"]["GwPriority"] = 101
    else:
        del facts[2]["NetworkSettings"]["Networks"]["herdr-control-plane_default"]
    with pytest.raises(ValueError):
        policy(facts, expected_network_id="c" * 64, require_attached=True)


def test_existing_network_requires_recorded_id_not_name_only(facts):
    attach(facts)
    with pytest.raises(ValueError):
        policy(facts)


def test_compose_override_adds_only_private_web_without_changing_ports():
    root = Path(__file__).parents[1]
    base = yaml.safe_load((root / "deploy/control-plane/compose.yaml").read_text())
    overlay = yaml.safe_load((root / "deploy/control-plane/compose.web-egress.yaml").read_text())
    assert set(overlay["services"]) == {"control-plane-web"}
    web = overlay["services"]["control-plane-web"]
    assert set(web) == {"networks"}
    assert web["networks"]["web-egress"]["gw_priority"] == 100
    assert web["networks"]["default"]["gw_priority"] == 0
    assert not any(
        "web-egress" in item.get("networks", {}) for name, item in base["services"].items()
    )
    network = overlay["networks"]["web-egress"]
    assert network["driver_opts"]["com.docker.network.bridge.name"] == "br-herdr-cp-eg"
    assert network["labels"]["io.herdr.web-egress.policy"] == "v1"


def test_inspection_failure_is_sanitized_and_no_firewall_is_executed(capsys):
    with patch.object(egress, "collect", side_effect=OSError("secret environment detail")):
        assert egress.main(["--enabled", "--gateway-ip", "100.104.39.6/32"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "Web egress policy refused; no changes made."


def test_host_bridge_name_collision_without_docker_network_refuses(facts):
    with pytest.raises(ValueError):
        egress.policy(
            enabled=True,
            networks=facts[0],
            routes=facts[1],
            web=facts[2],
            destination="100.104.39.6/32",
            addresses=[{"addr_info": [{"local": "100.104.39.6", "family": "inet"}]}],
            interfaces=[{"ifname": "br-herdr-cp-eg", "ifindex": 14}],
        )


def test_disabled_policy_does_not_grant_other_host_ports_or_sibling_access(facts):
    result = egress.policy(enabled=False)
    assert result == {"enabled": False}
    sibling = copy.deepcopy(facts[2])
    sibling["Id"] = "f" * 64
    sibling["Config"]["Labels"]["com.docker.compose.service"] = "collab-public"
    with pytest.raises(ValueError):
        egress.policy(
            enabled=True,
            networks=facts[0],
            routes=facts[1],
            web=sibling,
            destination="100.104.39.6/32",
            addresses=[{"addr_info": [{"local": "100.104.39.6", "family": "inet"}]}],
        )
    result = policy(facts)
    rule = result["ufw_allow"]
    assert rule[rule.index("port") + 1] == "443"
    assert rule[rule.index("to") + 1] == "100.104.39.6/32"
    assert rule[rule.index("from") + 1] == "172.31.252.0/29"
    assert rule[rule.index("on") + 1] == "br-herdr-cp-eg"


def test_strict_inspection_only_commands_and_all_route_tables():
    commands = []

    def run(command, **kwargs):
        commands.append(command)

        class Result:
            stdout = "[]"

        if command[:3] == ["docker", "network", "ls"]:
            Result.stdout = ""
        elif command[:2] == ["docker", "inspect"]:
            Result.stdout = "{}"
        return Result()

    with patch.object(egress.subprocess, "run", side_effect=run):
        egress.collect()
    assert ["ip", "-j", "-4", "route", "show", "table", "all"] in commands
    assert ["ip", "-j", "-6", "route", "show", "table", "all"] in commands
    assert ["ip", "-j", "link", "show"] in commands
    assert ["ip", "-j", "-4", "address", "show"] in commands
    assert all(command[0] in ("ip", "docker") for command in commands)
    assert not any(
        "exec" in command or "connect" in command or "create" in command for command in commands
    )


def test_web_endpoint_must_reference_exact_owned_network_and_source_cidr(facts):
    attach(facts)
    endpoint = facts[2]["NetworkSettings"]["Networks"]["herdr-control-plane_web-egress"]
    endpoint["NetworkID"] = "e" * 64
    endpoint["IPAddress"] = "172.16.4.90"
    with pytest.raises(ValueError):
        policy(facts, expected_network_id="c" * 64, require_attached=True)


@pytest.mark.parametrize(
    "gateway",
    [
        "100.104.39.6",
        "100.104.39.6/24",
        "8.8.8.8/32",
        "127.0.0.1/32",
        "0.0.0.0/32",
        "224.0.0.1/32",
        "169.254.1.1/32",
        "100.71.1.1/32",
    ],
)
def test_gateway_must_be_explicit_private_unicast_assigned_host_address(facts, gateway):
    with pytest.raises(ValueError):
        egress.policy(
            enabled=True,
            networks=facts[0],
            routes=facts[1],
            web=facts[2],
            destination=gateway,
            addresses=[{"addr_info": [{"local": "100.104.39.6", "family": "inet"}]}],
        )


def test_no_gateway_default_and_non_linux_enabled_preflight(capsys):
    with patch.object(egress, "collect", side_effect=AssertionError("must not inspect")):
        assert egress.main(["--enabled"]) == 1
    with (
        patch.object(egress.sys, "platform", "darwin"),
        patch.object(egress, "collect", side_effect=AssertionError("must not inspect")),
    ):
        assert egress.main(["--enabled", "--gateway-ip", "100.104.39.6/32"]) == 1
    assert "Linux" in capsys.readouterr().err
