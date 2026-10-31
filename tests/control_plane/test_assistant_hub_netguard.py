from herdr_engineering.control_plane.assistant_hub.netguard import (
    DEFAULT_NETWORKS,
    bind_allowed,
    host_header_allowed,
    parse_networks,
    peer_allowed,
)

NETS = parse_networks(list(DEFAULT_NETWORKS))


def test_bind_loopback_and_tailnet_ok():
    for host in ("127.0.0.1", "::1", "100.64.1.2", "fd7a:115c:a1e0::5"):
        assert bind_allowed(host, NETS)


def test_bind_wildcard_refused():
    assert not bind_allowed("0.0.0.0", NETS)
    assert not bind_allowed("::", NETS)


def test_bind_public_ip_refused():
    assert not bind_allowed("8.8.8.8", NETS)


def test_bind_hostname_refused():
    assert not bind_allowed("localhost", NETS)


def test_peer_tailnet_v4_v6():
    assert peer_allowed("100.64.1.2", NETS)
    assert peer_allowed("fd7a:115c:a1e0::1", NETS)
    assert peer_allowed("127.0.0.1", NETS)


def test_peer_ipv4_mapped_unwrapped():
    assert peer_allowed("::ffff:100.64.1.2", NETS)
    assert not peer_allowed("::ffff:8.8.8.8", NETS)


def test_peer_outside_refused():
    assert not peer_allowed("8.8.8.8", NETS)
    assert not peer_allowed("not-an-ip", NETS)
    assert not peer_allowed("", NETS)


def test_host_header_allowlist_and_port_strip():
    allowed = ["Assistant.Tail.ts.net", "100.64.1.2"]
    assert host_header_allowed("assistant.tail.ts.net:9483", allowed)
    assert host_header_allowed("100.64.1.2:9483", allowed)
    assert host_header_allowed("localhost:9483", [])
    assert host_header_allowed("[::1]:9483", [])
    assert host_header_allowed("[::1]", [])
    assert not host_header_allowed("evil.example:9483", allowed)
    assert not host_header_allowed("evil.example", [])


def test_host_header_missing_refused():
    assert not host_header_allowed(None, ["localhost"])
    assert not host_header_allowed("", [])
