"""Public links: the recipient landing bar, the claim cookie, and its strict separation from lab and member sessions."""

from __future__ import annotations

import re

from herdr_artifact_host import member_session as ms
from herdr_artifact_host.share_session import CLAIM_DOMAIN, mint_session
from tests.artifact_host.seed import SECRET, Host, access, ident
from tests.artifact_host.test_public_lab import lab, sign_in

DOC = {"Sec-Fetch-Dest": "document"}


def claim_cookie(host: Host, token: str, user: str = "alice", email: str = "alice@lab.example"):
    r = sign_in(host, type("S", (), {"token": token})(), access(user), ident(user, email=email))
    return r


def test_signin_page(host: Host) -> None:
    s = host.seed_share()
    r = host.get(f"/s/{s.token}/signin")
    assert r.status == 200 and b'id="herdr-landing"' in r.body and b"Dana Doe" in r.body
    assert b"<" not in re.search(rb'id="herdr-landing">(.*?)</script>', r.body).group(1)
    assert host.get(f"/s/{lab(host).token}/signin").status == 404  # lab: no public sign-in


def test_landing_bar_only_on_document_navigation(host: Host) -> None:
    s = host.seed_share()
    page = host.get(f"/s/{s.token}/f/index.html", DOC)
    assert page.status == 200 and b"herdr-landing" in page.body and b"Weekly report" in page.body
    plain = host.get(f"/s/{s.token}/f/index.html")
    assert b"herdr-landing" not in plain.body
    assert page.header("ETag") != plain.header("ETag")
    assert page.header("Vary") == "Sec-Fetch-Dest"
    assert b"herdr-landing" not in host.get(f"/s/{s.token}/f/notes.txt", DOC).body
    assert (
        host.get(f"/s/{s.token}/f/index.html", {**DOC, "Range": "bytes=0-3"}).status == 200
    )  # injected: no ranges


def test_claim_cookie_and_claim_recorded(host: Host) -> None:
    s = host.seed_share()
    r = claim_cookie(host, s.token)
    assert r.status == 200 and r.json()["landing"] == f"/s/{s.token}/"
    cookie = r.header("Set-Cookie") or ""
    assert cookie.startswith("herdr_claim=") and f"Path=/s/{s.token}/" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    value = cookie.split(";")[0]
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", s.share_id) == []
    page = host.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": value})
    assert page.status == 200
    [claim] = host.rows(
        "SELECT member_id, title, sharer FROM share_claims WHERE share_id=$1", s.share_id
    )
    assert claim["member_id"] == "alice" and claim["title"] == "Weekly report"
    assert len(host.events(s.share_id, "claimed")) == 1
    host.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": value})
    assert len(host.events(s.share_id, "claimed")) == 1  # once per (session, share)
    for e in host.events(s.share_id):
        assert value.split("=", 1)[1] not in e["detail"]


def test_no_claim_without_cookie_or_for_non_entry(host: Host) -> None:
    s = host.seed_share()
    host.get(f"/s/{s.token}/f/index.html", DOC)
    value = (claim_cookie(host, s.token).header("Set-Cookie") or "").split(";")[0]
    host.get(f"/s/{s.token}/f/notes.txt", {"Cookie": value})
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", s.share_id) == []


def test_claim_cookie_is_bound_to_its_share(host: Host) -> None:
    a, b = host.seed_share(), host.seed_share()
    value = (claim_cookie(host, a.token).header("Set-Cookie") or "").split(";")[0]
    host.get(f"/s/{b.token}/f/index.html", {**DOC, "Cookie": value})
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", b.share_id) == []


def test_lab_session_and_member_session_are_not_claim_cookies(host: Host) -> None:
    s = host.seed_share()
    import time

    exp = int(time.time()) + 600
    lab_sess = mint_session(SECRET, s.share_id, "alice", exp, email="alice@lab.example", basis="g")
    member = ms.mint_session(SECRET, "alice", exp)
    for forged in (lab_sess, member, "garbage", ""):
        host.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": f"herdr_claim={forged}"})
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", s.share_id) == []
    assert host.events(s.share_id, "claimed") == []


def test_claim_cookie_is_not_a_lab_session_or_member_session(host: Host) -> None:
    pub = host.seed_share()
    labshare = lab(host)
    exp = int(__import__("time").time()) + 600
    claim_on_lab = mint_session(
        SECRET,
        labshare.share_id,
        "alice",
        exp,
        email="alice@lab.example",
        basis="g",
        domain=CLAIM_DOMAIN,
    )
    assert host.get(f"/s/{labshare.token}/k/{claim_on_lab}/f/notes.txt").status == 404
    assert host.get(f"/s/{labshare.token}/k/{claim_on_lab}/a/notes.txt").status == 404
    assert (
        host.get(
            f"/s/{labshare.token}/f/notes.txt", {"Cookie": f"herdr_claim={claim_on_lab}"}
        ).status
        == 404
    )
    assert ms.check_session(SECRET, claim_on_lab, exp - 600) is None
    assert pub.token  # the public share itself is unaffected


def test_lab_shares_are_not_claimable(host: Host) -> None:
    labshare = lab(host)
    r = sign_in(host, labshare)
    assert "herdr_claim" not in (r.header("Set-Cookie") or "")
    host.get(f"/s/{labshare.token}/k/{r.json()['landing'].split('/')[4]}/f/index.html", DOC)
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", labshare.share_id) == []
    assert host.events(labshare.share_id, "claimed") == []


def test_claim_session_requires_valid_tokens(host: Host) -> None:
    s = host.seed_share()
    from tests.artifact_host.seed import OTHER_KEY

    assert sign_in(host, s, access(key=OTHER_KEY)).status == 404
    assert sign_in(host, s, idt=ident(verified=False)).status == 404
    assert host.request("POST", f"/s/{s.token}/session", {"Content-Length": "0"}).status == 404
    assert (
        host.request(
            "POST",
            f"/s/{s.token}/session",
            {"Authorization": f"Bearer {access()}", "X-Id-Token": ident(), "Content-Length": "4"},
            b"body",
        ).status
        == 404
    )


def test_claim_cookie_dies_with_the_share(host: Host) -> None:
    s = host.seed_share()
    value = (claim_cookie(host, s.token).header("Set-Cookie") or "").split(";")[0]
    host.execute("UPDATE shares SET state='revoked' WHERE share_id=$1", s.share_id)
    assert host.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": value}).status == 404
