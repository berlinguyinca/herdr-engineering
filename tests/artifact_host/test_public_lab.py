"""Lab shares: session minting from Cognito tokens, the allow-list, files under a session, and the 404 for everything else."""

from __future__ import annotations

import re
import time

import pytest

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
        "X-Id-Token": idt or ident(),
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


def test_group_allow_list(host: Host) -> None:
    s = lab(host, emails=(), groups=("Readers",))
    assert sign_in(host, s).status == 404
    assert sign_in(host, s, access(groups=("Readers",))).status == 200


def test_denials_and_events(host: Host) -> None:
    s = lab(host)
    assert sign_in(host, s, idt=ident(email="mallory@evil.example")).status == 404
    assert sign_in(host, s, idt=ident(verified=False)).status == 404
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
