from herdr_engineering.control_plane.assistant_hub.netguard import (
    DEFAULT_NETWORKS,
    bind_allowed,
    host_header_allowed,
    normalize_host_entry,
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


def test_host_header_strict_malformed_refused():
    bad = [
        "[::1]evil", "[::1]:x:y", "[::1]:", "[::1]:123456", "localhost:abc", "localhost:",
        "localhost:80:80", "localhost..", "\tlocalhost", "localhost ", " localhost",
        "a@localhost", "localhost/x", "localhost\n",
    ]  # fmt: skip
    for header in bad:
        assert not host_header_allowed(header, []), header
    assert host_header_allowed("localhost.", [])
    assert host_header_allowed("localhost:9483", [])


def test_scope_ids_and_whitespace_refused():
    for value in ("::1%eth0", "::1%lo", "::ffff:127.0.0.1 ", " 127.0.0.1", "127.0.0.1\n"):
        assert not peer_allowed(value, NETS), value
        assert not bind_allowed(value, NETS), value


def test_bind_ipv4_mapped_refused_but_peer_unwrapped():
    assert not bind_allowed("::ffff:127.0.0.1", NETS)
    assert peer_allowed("::ffff:127.0.0.1", NETS)


def test_allowed_hosts_bracketed_ipv6_entries_match():
    assert host_header_allowed("[fd7a::1]:9483", ["[fd7a::1]"])
    assert host_header_allowed("[fd7a::1]:9483", ["fd7a::1"])
    assert host_header_allowed("[FD7A::1]", ["[fd7a::1]:9483"])
    assert not host_header_allowed("[fd7a::2]:9483", ["[fd7a::1]"])


def test_normalize_host_entry():
    assert normalize_host_entry("Box.TS.net:9483") == "box.ts.net"
    assert normalize_host_entry("[fd7a::1]:9483") == "fd7a::1"
    assert normalize_host_entry("fd7a::1") == "fd7a::1"
    for bad in ("http://x", "a b", "x; ls", "$(id)", "u@h", "h/path", "", None):
        assert normalize_host_entry(bad) is None
