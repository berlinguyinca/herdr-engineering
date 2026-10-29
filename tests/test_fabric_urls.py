import pytest

from herdr_engineering.fabric_urls import (
    UnsafeTarget,
    resolve_url,
    validate_target_host,
)

BASE = "dev.lan"


def lease(machine, tport, eport, **kw):
    return {"id": f"l_{machine}_{tport}", "state": "active", "external_port": eport,
            "target": {"machine_id": machine, "host": "127.0.0.1", "port": tport},
            "label": kw.pop("label", None), "owner": kw.pop("owner", {}), **kw}


def r(url, leases, **kw):
    return resolve_url(url, leases, url_base=BASE, **kw)


def test_local_agent_host_url_is_rewritten_to_https():
    res = r("http://localhost:3188/benchmarks", [lease("bender", 3188, 3188)], source_machine="bender")
    assert res.status == "resolved"
    assert res.canonical == "https://dev.lan:3188/benchmarks"
    assert res.original == "http://localhost:3188/benchmarks"
    assert (res.host, res.port) == ("127.0.0.1", 3188)


def test_remote_host_url_uses_the_lease_port_not_the_target_port():
    res = r("http://127.0.0.1:3000/x", [lease("beast", 3000, 18010)], source_machine="beast")
    assert res.canonical == "https://dev.lan:18010/x"


def test_two_hosts_same_port_source_disambiguates():
    ls = [lease("bender", 3000, 3000), lease("beast", 3000, 18011)]
    assert r("http://localhost:3000/", ls, source_machine="beast").canonical == "https://dev.lan:18011/"
    assert r("http://localhost:3000/", ls, source_machine="bender").canonical == "https://dev.lan:3000/"


def test_unknown_source_with_ambiguous_port_is_ambiguous_not_guessed():
    ls = [lease("bender", 3000, 3000), lease("beast", 3000, 18011)]
    res = r("http://localhost:3000/", ls, source_machine=None)
    assert res.status == "ambiguous" and res.canonical is None


def test_unknown_source_unique_match_resolves():
    res = r("http://localhost:3000/", [lease("beast", 3000, 18011)], source_machine=None)
    assert res.status == "resolved"


def test_source_without_lease_does_not_borrow_another_hosts_service():
    res = r("http://localhost:3000/", [lease("beast", 3000, 18011)], source_machine="bender")
    assert res.status == "unmapped" and res.canonical is None


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "LOCALHOST", "127.1.2.3"])
def test_loopback_spellings(host):
    res = r(f"http://{host}:3188/", [lease("bender", 3188, 3188)], source_machine="bender")
    assert res.status == "resolved", host


def test_path_query_fragment_and_encoding_preserved_byte_for_byte():
    url = "http://localhost:3188/a%20b/%E2%9C%93?q=1&r=%2F+x#frag%20ment"
    res = r(url, [lease("bender", 3188, 3188)], source_machine="bender")
    assert res.canonical == "https://dev.lan:3188/a%20b/%E2%9C%93?q=1&r=%2F+x#frag%20ment"


def test_external_urls_never_rewritten():
    res = r("https://example.com:3188/", [lease("bender", 3188, 3188)], source_machine="bender")
    assert res.status == "not_loopback" and res.canonical is None


def test_lookalike_hosts_are_not_loopback():
    for u in ("http://localhost.evil.com:3188/", "http://127.0.0.1.evil.com:3188/",
              "http://evil.com/@localhost:3188/", "http://localhost@evil.com:3188/"):
        assert r(u, [lease("bender", 3188, 3188)], source_machine="bender").status in (
            "not_loopback", "invalid"), u


def test_userinfo_is_rejected():
    assert r("http://u:p@localhost:3188/", [lease("bender", 3188, 3188)], source_machine="bender").status == "invalid"


def test_missing_port_and_bad_port_invalid_or_unmapped():
    assert r("http://localhost/", [lease("bender", 80, 80)], source_machine="bender").status == "resolved"
    assert r("http://localhost:99999/", [], source_machine="bender").status == "invalid"


def test_https_loopback_source_scheme_still_resolves():
    res = r("https://localhost:3188/", [lease("bender", 3188, 3188)], source_machine="bender")
    assert res.canonical == "https://dev.lan:3188/"


def test_non_http_schemes_untouched():
    assert r("ftp://localhost:3188/", [lease("bender", 3188, 3188)], source_machine="bender").status == "not_loopback"
    assert r("javascript:alert(1)", [], source_machine="bender").status in ("not_loopback", "invalid")


def test_expired_and_unhealthy_leases_report_reason():
    exp = lease("bender", 3188, 3188, state="expired")
    res = r("http://localhost:3188/", [exp], source_machine="bender")
    assert res.status == "expired" and res.canonical is None
    sick = lease("bender", 3188, 3188, health_last_error="refused")
    res = r("http://localhost:3188/", [sick], source_machine="bender")
    assert res.status == "unhealthy" and "refused" in res.reason


def test_service_identity_and_access_class():
    res = r("http://localhost:3188/", [lease("bender", 3188, 3188, label="web", owner={"herdr_session_id": "s1"})],
            source_machine="bender")
    assert res.service == "web" and res.session == "s1" and res.access == "private-fabric"


def test_registered_fabric_hostname_is_canonicalized():
    res = r("http://dev.lan:3188/p", [lease("bender", 3188, 3188)], source_machine="bender")
    assert res.status == "resolved" and res.canonical == "https://dev.lan:3188/p"


def test_registered_hostname_for_unknown_port_unmapped():
    assert r("http://dev.lan:4000/", [lease("bender", 3188, 3188)], source_machine="bender").status == "unmapped"


# ---- SSRF: target_host validation -------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "100.64.1.2", "fd7a:115c:a1e0::1"])
def test_safe_targets_accepted(host):
    validate_target_host(host)


@pytest.mark.parametrize("host", ["169.254.169.254", "169.254.0.1", "fe80::1", "0.0.0.0", "224.0.0.1",
                                  "metadata.google.internal", "", "evil.com", "127.0.0.1@evil.com",
                                  "::ffff:169.254.169.254", "fd00:ec2::254"])
def test_unsafe_targets_rejected(host):
    with pytest.raises(UnsafeTarget):
        validate_target_host(host)


def test_extra_networks_extend_the_allowlist():
    with pytest.raises(UnsafeTarget):
        validate_target_host("192.168.1.5")
    validate_target_host("192.168.1.5", ["192.168.0.0/16"])
    with pytest.raises(UnsafeTarget):  # metadata stays blocked even inside an allowed range
        validate_target_host("169.254.169.254", ["169.254.0.0/16"])
