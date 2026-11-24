"""Pure decision functions of the dashboard proxy's Host gate and Origin check."""
import pytest

from herdr_engineering.control_plane.dashboard_proxy import (
    host_allowed,
    origin_allowed,
    parse_authority,
)


@pytest.mark.parametrize("value,expected", [
    ("dash.example", ("dash.example", None)), ("Dash.Example:8443", ("dash.example", 8443)),
    ("10.0.0.5:80", ("10.0.0.5", 80)), ("[::1]:8081", ("::1", 8081)), ("[::1]", ("::1", None)),
    ("dash.example:99999", None), ("dash.example:", None), (":80", None), ("", None), ("a b", None),
    ("https://dash.example", None), ("dash.example/x", None), ("u@dash.example", None), ("[abc]", None),
    ("dash.example:x", None),
])
def test_parse_authority(value, expected):
    assert parse_authority(value) == expected


def entries(*values):
    return tuple(parse_authority(v) for v in values)


@pytest.mark.parametrize("host,listed,ok", [
    ("10.0.0.5:8080", ("dash.example",), True),                 # IP literal
    ("localhost", ("dash.example",), True),
    ("[::1]:8081", ("dash.example",), True),
    ("dash.example", ("dash.example",), True),                  # port-less entry, any port
    ("dash.example:9999", ("dash.example",), True),
    ("dash.example:8081", ("dash.example:8081",), True),        # entry port equals Host port
    ("dash.example:8082", ("dash.example:8081",), False),
    ("dash.example", ("dash.example:443",), True),              # missing Host port equals 80 or 443
    ("dash.example", ("dash.example:80",), True),
    ("dash.example", ("dash.example:8081",), False),
    ("evil.example", ("dash.example",), False),
    (None, ("dash.example",), False),
    ("dash.example:99999", ("dash.example",), False),
])
def test_host_gate(host, listed, ok):
    assert host_allowed(host, entries(*listed)) is ok


@pytest.mark.parametrize("origin,host,listed,scheme,ok", [
    # listed name: scheme, then port = entry port / Host port / scheme default
    ("https://dash.example", "dash.example", ("dash.example",), "https", True),
    ("http://dash.example", "dash.example", ("dash.example",), "https", False),
    ("http://dash.example", "dash.example", ("dash.example:443",), "https", False),
    ("https://dash.example:9999", "dash.example", ("dash.example",), None, False),
    ("https://dash.example", "dash.example", ("dash.example:443",), None, True),
    ("https://dash.example:444", "dash.example", ("dash.example:443",), None, False),
    ("http://dash.example:8081", "dash.example:8081", ("dash.example",), None, True),
    ("http://dash.example:8082", "dash.example:8081", ("dash.example",), None, False),
    ("https://dash.example", "dashboard-proxy:8081", ("dash.example", "dashboard-proxy"), "https", True),
    ("http://10.0.0.5", "10.0.0.5", ("10.0.0.5",), "https", False),     # scheme applies to a listed IP
    # unlisted name: equal to the Host name; Host port, else default / any for IP and localhost
    ("http://10.0.0.5:8080", "10.0.0.5:8080", (), None, True),
    ("http://10.0.0.5:8080", "10.0.0.5:9090", (), None, False),
    ("http://10.0.0.5:8080", "10.0.0.5", (), None, True),                # port-stripping front, IP
    ("http://localhost:8080", "localhost", (), None, True),
    ("http://dash.example:8080", "dash.example", (), None, False),       # port-stripping front, DNS name
    ("http://dash.example", "dash.example", (), None, True),
    ("https://dash.example", "dash.example", (), None, True),
    ("http://evil.example", "dash.example", (), None, False),
    ("http://dash.example", None, (), None, False),
    # malformed
    ("http://[x", "10.0.0.5", (), None, False),
    ("null", "10.0.0.5", (), None, False),
    ("ftp://10.0.0.5", "10.0.0.5", (), None, False),
    ("http://10.0.0.5:99999", "10.0.0.5", (), None, False),
])
def test_origin_decision(origin, host, listed, scheme, ok):
    assert origin_allowed(origin, host, entries(*listed), scheme) is ok
