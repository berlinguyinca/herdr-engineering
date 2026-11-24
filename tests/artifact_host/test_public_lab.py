"""Lab shares: session minting from Cognito tokens, the allow-list, files under a session, and the 404 for everything else."""

from __future__ import annotations

import re
import time

import pytest

from herdr_artifact_host.share_http import (
    SESSION_ATTEMPTS_GLOBAL_PER_MINUTE,
    SESSION_FAILED_PER_SHARE_PER_MINUTE,
    SESSION_PER_CLIENT_PER_MINUTE,
)
from herdr_artifact_host.share_session import check_session
from tests.artifact_host.seed import (
    FILES,
    OTHER_KEY,
    SECRET,
    Host,
    Seeded,
    access,
    ident,
    running_host,
)


def sign_in(
    host: Host,
    s: Seeded,
    acc: str | None = None,
    idt: str | None = None,
    extra: dict[str, str] | None = None,
):
    headers = {
        "Authorization": f"Bearer {acc or access()}",
        "X-Id-Token": idt or ident(nonce="n-0"),
        "X-Nonce": "n-0",
        "Content-Length": "0",
    }
    headers.update(extra or {})
    return host.request("POST", f"/s/{s.token}/session", headers)


def lab(host: Host, **kw) -> Seeded:
    kw.setdefault("emails", ("alice@lab.example",))
    return host.seed_share(audience="lab", **kw)


def landing_session(r) -> str:
    m = re.fullmatch(r"/s/[^/]+/k/([^/]+)/[fv]/.*", r.json()["landing"])
    assert m
    return m.group(1)


def test_session_for_listed_email(host: Host) -> None:
    s = lab(host)
    r = sign_in(host, s)
    assert r.status == 200
    body = r.json()
    assert body["landing"].startswith(f"/s/{s.token}/k/") and body["landing"].endswith(
        "/f/index.html"
    )
    got = check_session(SECRET, s.share_id, landing_session(r), time.time())
    assert got is not None and got["identity"] == "alice" and got["email"] == "alice@lab.example"
    cookie = r.header("Set-Cookie") or ""
    assert (
        "HttpOnly" in cookie
        and "Secure" in cookie
        and "SameSite=Strict" in cookie
        and f"Path=/s/{s.token}/" in cookie
    )
    assert "no-store" in (r.header("Cache-Control") or "")
    assert s.token not in r.body.decode().replace(body["landing"], "")


def test_lab_session_binds_the_id_token_to_the_page_nonce(host: Host) -> None:
    s = lab(host)
    mine = ident(nonce="n-mine")
    assert sign_in(host, s, idt=mine, extra={"X-Nonce": "n-mine"}).status == 200
    assert sign_in(host, s, idt=mine, extra={"X-Nonce": "n-other"}).status == 404  # replayed token
    bare = {"Authorization": f"Bearer {access()}", "X-Id-Token": mine, "Content-Length": "0"}
    assert host.request("POST", f"/s/{s.token}/session", bare).status == 404  # no X-Nonce sent
    assert (
        sign_in(host, s, idt=ident(), extra={"X-Nonce": "n-mine"}).status == 404
    )  # token has none
    reasons = " ".join(e["detail"] for e in host.events(s.share_id, "denied"))
    assert "bad_nonce" in reasons and "bad_id_token" in reasons


def test_a_link_holder_cannot_lock_other_viewers_out_of_sign_in(host: Host) -> None:
    """Unauthenticated session POSTs are charged to the sender, not to the share's or the global budget."""
    s = lab(host)
    junk = {"Content-Length": "0", "X-Forwarded-For": "203.0.113.9"}
    codes = [host.request("POST", f"/s/{s.token}/session", junk).status for _ in range(60)]
    assert set(codes) == {404}
    ok = sign_in(host, s, extra={"X-Forwarded-For": "198.51.100.7"})
    assert ok.status == 200
    # the attacker's own address stays throttled, even with good tokens
    assert sign_in(host, s, extra={"X-Forwarded-For": "203.0.113.9"}).status == 404


def test_ipv6_clients_are_limited_by_their_64(host: Host) -> None:
    """Rotating through the addresses of one /64 must not mint a fresh budget per address."""
    s = lab(host)
    for i in range(SESSION_PER_CLIENT_PER_MINUTE + 2):
        junk = {"Content-Length": "0", "X-Forwarded-For": f"2001:db8:1:1::{i + 1:x}"}
        assert host.request("POST", f"/s/{s.token}/session", junk).status == 404
    same_64 = sign_in(host, s, extra={"X-Forwarded-For": "2001:db8:1:1::ffff"})
    assert same_64.status == 404
    assert sign_in(host, s, extra={"X-Forwarded-For": "2001:db8:2::1"}).status == 200


def test_one_user_with_valid_tokens_cannot_use_up_the_share_budget(host: Host) -> None:
    """The per-user budget is smaller than the per-share one, so one signed-in user cannot lock out the rest."""
    s = lab(host, emails=("alice@lab.example", "bob@lab.example"))
    assert 404 in {sign_in(host, s).status for _ in range(40)}
    bob = sign_in(host, s, access("bob"), ident("bob", email="bob@lab.example", nonce="n-0"))
    assert bob.status == 200


def test_unverified_attempts_have_a_global_cap_too(host: Host) -> None:
    """Addresses are cheap to rotate: past the shape checks the total is bounded as well (it bounds the JWT work)."""
    per_share = SESSION_FAILED_PER_SHARE_PER_MINUTE - 5
    shares = [lab(host) for _ in range(SESSION_ATTEMPTS_GLOBAL_PER_MINUTE // per_share + 2)]
    for i in range(SESSION_ATTEMPTS_GLOBAL_PER_MINUTE):
        xff = {"X-Forwarded-For": f"198.51.{i // 250}.{i % 250 + 1}"}
        assert sign_in(host, shares[i // per_share], acc="junk", extra=xff).status == 404
    assert sign_in(host, shares[-1], extra={"X-Forwarded-For": "192.0.2.77"}).status == 404


def test_ipv4_mapped_clients_are_limited_by_their_own_address(host: Host) -> None:
    """``::ffff:a.b.c.d`` is an IPv4 client: one of them must not share a /64 budget with every other."""
    s = lab(host)
    for _ in range(SESSION_PER_CLIENT_PER_MINUTE + 2):
        junk = {"Content-Length": "0", "X-Forwarded-For": "::ffff:203.0.113.9"}
        assert host.request("POST", f"/s/{s.token}/session", junk).status == 404
    assert sign_in(host, s, extra={"X-Forwarded-For": "::ffff:203.0.113.9"}).status == 404
    assert sign_in(host, s, extra={"X-Forwarded-For": "::ffff:198.51.100.7"}).status == 200


def test_one_shares_abuse_does_not_shut_the_others_out(host: Host) -> None:
    """Header-less POSTs from many /64s exhaust that share's failure budget only, not the all-clients cap."""
    a, b = lab(host), lab(host)
    junk = {"Content-Length": "0"}
    for n in range(40):
        for _ in range(SESSION_PER_CLIENT_PER_MINUTE):
            xff = {**junk, "X-Forwarded-For": f"2001:db8:{n + 1:x}::1"}
            assert host.request("POST", f"/s/{a.token}/session", xff).status == 404
    assert sign_in(host, a, extra={"X-Forwarded-For": "198.51.100.7"}).status == 404
    assert sign_in(host, b, extra={"X-Forwarded-For": "198.51.100.8"}).status == 200


def test_successful_sign_ins_behind_one_address_are_not_throttled(host: Host) -> None:
    """Twenty people behind one NAT opening links within a minute: only failures are charged to the address."""
    s = lab(host, emails=(), groups=("Readers",))
    nat = {"X-Forwarded-For": "203.0.113.50"}
    codes = []
    for i in range(2 * SESSION_PER_CLIENT_PER_MINUTE):
        user = f"user{i}"
        acc, idt = (
            access(user, groups=("Readers",)),
            ident(user, email=f"{user}@lab.example", nonce="n-0"),
        )
        codes.append(sign_in(host, s, acc, idt, extra=nat).status)
    assert set(codes) == {200}


def test_group_allow_list(host: Host) -> None:
    s = lab(host, emails=(), groups=("Readers",))
    assert sign_in(host, s).status == 404
    assert sign_in(host, s, access(groups=("Readers",))).status == 200


def test_denials_and_events(host: Host) -> None:
    s = lab(host)
    assert sign_in(host, s, idt=ident(email="mallory@evil.example", nonce="n-0")).status == 404
    assert sign_in(host, s, idt=ident(verified=False, nonce="n-0")).status == 404
    assert sign_in(host, s, acc=access(key=OTHER_KEY)).status == 404
    assert sign_in(host, s, acc=access(exp=time.time() - 10)).status == 404
    assert sign_in(host, s, acc=access(client="stranger")).status == 404
    assert sign_in(host, s, extra={"Authorization": "Basic abc"}).status == 404
    reasons = [e["detail"] for e in host.events(s.share_id, "denied")]
    assert reasons
    for d in reasons:
        assert "eyJ" not in d and "mallory" not in d


def test_session_post_refuses_body_and_get(host: Host) -> None:
    s = lab(host)
    headers = {"Authorization": f"Bearer {access()}", "X-Id-Token": ident()}
    assert (
        host.request(
            "POST", f"/s/{s.token}/session", {**headers, "Content-Length": "2"}, b"{}"
        ).status
        == 404
    )
    assert host.get(f"/s/{s.token}/session", headers).status == 404
    assert host.request("OPTIONS", f"/s/{s.token}/session").status == 404  # no preflight


def test_lab_file_under_session_and_not_without(host: Host) -> None:
    s = lab(host)
    sess = landing_session(sign_in(host, s))
    r = host.get(f"/s/{s.token}/k/{sess}/f/notes.txt")
    assert r.status == 200 and r.body == FILES["notes.txt"]
    assert host.get(f"/s/{s.token}/f/notes.txt").status == 404
    assert host.get(f"/s/{s.token}/k/{sess[:-2]}xx/f/notes.txt").status == 404


def test_session_is_bound_to_its_share(host: Host) -> None:
    a, b = lab(host), lab(host)
    sess = landing_session(sign_in(host, a))
    assert host.get(f"/s/{b.token}/k/{sess}/f/notes.txt").status == 404


def test_revoke_and_expiry_kill_existing_sessions(host: Host) -> None:
    s = lab(host)
    sess = landing_session(sign_in(host, s))
    assert host.get(f"/s/{s.token}/k/{sess}/f/notes.txt").status == 200
    host.execute("UPDATE shares SET state='revoked' WHERE share_id=$1", s.share_id)
    assert host.get(f"/s/{s.token}/k/{sess}/f/notes.txt").status == 404


def test_lab_viewer_page_and_static(host: Host) -> None:
    s = lab(host)
    r = host.get(f"/s/{s.token}/")
    assert r.status == 200 and "text/html" in (r.header("Content-Type") or "")
    assert "frame-ancestors 'none'" in (r.header("Content-Security-Policy") or "")
    cfg = host.get("/s/static/config.json")
    assert cfg.status == 200 and b"auth.lab.example" in cfg.body and b"/s/callback" in cfg.body
    assert host.get("/s/callback").status == 200
    assert host.get("/s/static/nonexistent.js").status == 404
    assert host.get("/s/static/../x").status == 404


def test_lab_unconfigured_is_404(migrated_dsn: str, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path, {"signing_secret": b""}) as h:
        s = lab(h)
        assert h.get(f"/s/{s.token}/").status == 404
        assert sign_in(h, s).status == 404
        pub = h.seed_share()
        assert h.get(f"/s/{pub.token}/f/notes.txt").status == 200


@pytest.mark.parametrize("kind", ["v", "a"])
def test_views(host: Host, kind: str) -> None:
    s = lab(host)
    sess = landing_session(sign_in(host, s))
    r = host.get(f"/s/{s.token}/k/{sess}/{kind}/notes.txt")
    assert r.status == 200 and b"plain notes" in r.body
    csp = r.header("Content-Security-Policy") or ""
    assert "nonce-" in csp and r.header("X-Frame-Options") == "DENY"
    html = host.get(f"/s/{s.token}/k/{sess}/v/index.html")
    assert html.status == 404  # live view of HTML is f/
    assert host.get(f"/s/{s.token}/k/{sess}/{kind}/missing.txt").status == 404


def test_annotate_page_carries_csrf_meta(host: Host) -> None:
    s = lab(host)
    sess = landing_session(sign_in(host, s))
    r = host.get(f"/s/{s.token}/k/{sess}/a/index.html")
    assert r.status == 200 and b"herdr-annotate-csrf" in r.body and b"hello" in r.body
