"""The recipient landing bar, the sign-in step and server-side claim recording on a public share (real sockets)."""
import json
import re

from herdr_engineering.control_plane import share_public
from herdr_engineering.control_plane.share_session import mint_session
from tests.control_plane.test_share_lab import NOW0, SECRET, access, ident, lab_config
from tests.control_plane.test_share_public import TOKEN, Env

DOC = {"Sec-Fetch-Dest": "document"}
ENTRY = f"/s/{TOKEN}/f/index.html"
NOTICE = "Sign in with your lab account to add this to your library. Signing in does not change who can see it."


def public(tmp_path, **extra):
    return Env(tmp_path, audience="public", config_extra=lab_config(tmp_path), state_extra=extra or None)


def exchange(env, user="alice"):
    headers = {"Authorization": "Bearer " + access(user), "X-Id-Token": ident(user, email=f"{user}@lab.example"),
               "Content-Length": "0"}
    return env.request("POST", f"/s/{TOKEN}/session", headers)


def cookie_of(headers):
    return headers["set-cookie"].split(";")[0]


def claims(env):
    out = [json.loads(line) for line in env.event_lines()]
    return [e for e in out if e["kind"] == "claimed"]


def text(body):
    return body.decode("utf-8")


def test_viewer_has_shared_by_bar_and_signin_notice_when_signed_out(tmp_path):
    e = public(tmp_path, shared_by="Dana Lee", title="Results")
    try:
        status, _, body = e.request("GET", ENTRY, DOC)
        page = text(body)
        assert status == 200
        assert "Shared by Dana Lee · Results · expires in 1 day" in page
        assert NOTICE in page and "Sign in to keep this" in page
        assert f'href="/s/{TOKEN}/signin"' in page
        assert "hello" in page                                   # the content is still there
    finally:
        e.close()


def test_sharer_falls_back_when_the_share_names_nobody(tmp_path):
    e = public(tmp_path)
    try:
        page = text(e.request("GET", ENTRY, DOC)[2])
        assert "Shared by a lab member" in page
    finally:
        e.close()


def test_no_bar_for_subresources_or_without_lab_signin(tmp_path):
    e = public(tmp_path)
    plain = Env(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, audience="public")
    try:
        assert "data-herdr-landing" not in text(e.request("GET", ENTRY)[2])
        assert "data-herdr-landing" not in text(e.request("GET", ENTRY, {"Sec-Fetch-Dest": "iframe"})[2])
        assert "data-herdr-landing" not in text(plain.request("GET", ENTRY, DOC)[2])
    finally:
        e.close()
        plain.close()


def test_session_exchange_sets_an_httponly_claim_cookie_and_hides_the_signin_notice(tmp_path):
    e = public(tmp_path, shared_by="Dana Lee")
    try:
        status, h, body = exchange(e)
        assert status == 200 and json.loads(body)["landing"] == f"/s/{TOKEN}/"
        cookie = h["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
        assert f"Path=/s/{TOKEN}/" in cookie
        assert "alice" not in body.decode() and access("alice") not in body.decode()
        page = text(e.request("GET", ENTRY, {**DOC, "Cookie": cookie_of(h)})[2])
        assert "Shared by Dana Lee" in page
        assert NOTICE not in page and "Sign in to keep this" not in page
    finally:
        e.close()


def test_forged_or_foreign_claim_cookie_counts_as_signed_out(tmp_path):
    e = public(tmp_path)
    try:
        forged = mint_session(b"x" * 48, "shr-1", "alice", int(NOW0) + 100, email="a@lab.example", basis="g")
        other = mint_session(SECRET, "shr-other", "alice", int(NOW0) + 100, email="a@lab.example", basis="g")
        for value in (forged, other, "garbage"):
            page = text(e.request("GET", ENTRY, {**DOC, "Cookie": f"herdr_claim={value}"})[2])
            assert NOTICE in page
        assert claims(e) == []
    finally:
        e.close()


def test_session_exchange_failures_are_refused(tmp_path):
    e = public(tmp_path)
    try:
        bad = {"Authorization": "Bearer nope", "X-Id-Token": "nope", "Content-Length": "0"}
        assert e.request("POST", f"/s/{TOKEN}/session", bad)[0] == 404
        assert e.request("POST", f"/s/{TOKEN}/session", {"Content-Length": "0"})[0] == 404
    finally:
        e.close()


def test_claim_is_recorded_server_side_once_per_session(tmp_path):
    e = public(tmp_path)
    try:
        cookie = cookie_of(exchange(e)[1])
        for _ in range(3):
            assert e.request("GET", ENTRY, {**DOC, "Cookie": cookie})[0] == 200
        got = claims(e)
        assert len(got) == 1
        assert got[0]["actor"] == "alice" and got[0]["share_id"] == "shr-1"
        assert got[0]["detail"] == {"share_id": "shr-1"}
        second = cookie_of(exchange(e, "bob")[1])
        e.request("GET", ENTRY, {**DOC, "Cookie": second})
        assert [c["actor"] for c in claims(e)] == ["alice", "bob"]
    finally:
        e.close()


def test_signed_out_visitor_records_no_claim(tmp_path):
    e = public(tmp_path)
    try:
        e.request("GET", ENTRY, DOC)
        e.request("GET", f"/s/{TOKEN}/signin", DOC)
        e.request("GET", f"/s/{TOKEN}/", DOC)
        assert claims(e) == []
    finally:
        e.close()


def test_signin_page_carries_share_data_but_no_token_and_no_return_path(tmp_path):
    e = public(tmp_path, shared_by="Dana <b>Lee</b>", title="Results")
    try:
        status, h, body = e.request("GET", f"/s/{TOKEN}/signin", DOC)
        page = text(body)
        assert status == 200 and "no-store" in h["cache-control"]
        block = re.search(r'<script type="application/json" id="herdr-landing">(.*?)</script>', page, re.S)
        data = json.loads(block.group(1))
        assert data["sharer"] == "Dana <b>Lee</b>" and data["title"] == "Results" and data["expiresAt"]
        assert "<b>" not in block.group(1)                       # escaped inside the JSON block
        assert TOKEN not in page.replace(f"/s/{TOKEN}/signin", "")
        assert "return" not in page.lower().replace("herdr.share.return", "")
        for href in re.findall(r'href="([^"]*)"', page):
            assert "?" not in href
    finally:
        e.close()


def test_return_path_is_in_no_url_of_the_landing_page(tmp_path):
    e = public(tmp_path)
    try:
        page = text(e.request("GET", ENTRY, DOC)[2])
        for href in re.findall(r'href="([^"]*)"', page):
            assert "?" not in href and "return" not in href.lower()
    finally:
        e.close()


def test_viewer_csp_allows_same_origin_font_but_nothing_external(tmp_path):
    e = public(tmp_path)
    try:
        csp = e.request("GET", f"/s/{TOKEN}/signin", DOC)[1]["content-security-policy"]
        assert "font-src https://share.example/s/static/;" in csp
        assert re.findall(r"https?://[^\s;]+", csp) == ["https://auth.lab.example", "https://share.example/s/static/"]
        assert "https://auth.lab.example" in csp.split("connect-src")[1].split(";")[0]
    finally:
        e.close()


def test_landing_assets_are_served_from_the_static_allow_list(tmp_path):
    e = public(tmp_path)
    try:
        assert "landing.js" in share_public.STATIC_FILES and "landing.css" in share_public.STATIC_FILES
        for name, kind in (("landing.js", "text/javascript"), ("landing.css", "text/css")):
            status, h, _ = e.request("GET", f"/s/static/{name}")
            assert status == 200 and h["content-type"].startswith(kind)
    finally:
        e.close()


def test_expires_text():
    day = 86400.0
    assert share_public.expires_text(None, 0) == ""
    assert share_public.expires_text(day * 5 + 5, 0) == "expires in 5 days"
    assert share_public.expires_text(day + 1, 0) == "expires in 1 day"
    assert share_public.expires_text(day - 1, 0) == "expires today"
    assert share_public.expires_text(-10, 0) == "expires today"


def test_lab_share_pages_record_a_claim_for_the_verified_session(tmp_path):
    e = Env(tmp_path, audience="lab", expires_in=None, config_extra=lab_config(tmp_path),
            state_extra={"allow_emails": ["alice@lab.example"], "allow_groups": []})
    try:
        headers = {"Authorization": "Bearer " + access("alice"), "X-Id-Token": ident("alice"), "Content-Length": "0"}
        status, _, body = e.request("POST", f"/s/{TOKEN}/session", headers)
        assert status == 200
        landing = json.loads(body)["landing"]
        base = re.match(r"(/s/[^/]+/k/[^/]+/)", landing).group(1)
        for _ in range(2):
            assert e.request("GET", base + "f/index.html", DOC)[0] == 200
        got = claims(e)
        assert len(got) == 1 and got[0]["actor"] == "alice" and got[0]["detail"] == {"share_id": "shr-1"}
    finally:
        e.close()
