"""The write cookie (final opus review I3): a stolen session URL alone must not read or write annotations.

Real sockets and the real session exchange. A request that carries only the URL (curl with forged ``Origin`` and
``Sec-Fetch-*``) is refused; the HttpOnly, Secure, SameSite=Strict cookie set by the session POST is the second factor.
"""
import json
import re

import pytest

from herdr_engineering.control_plane import share_session as ss
from herdr_engineering.control_plane.share_session import csrf_token, mint_session
from tests.control_plane.test_share_annotate import (
    EMAIL,
    ORIGIN,
    api,
    create_body,
    good_headers,
    lines,
    make_env,
)
from tests.control_plane.test_share_lab import NOW0, SECRET, access, ident
from tests.control_plane.test_share_public import TOKEN

FORGED = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}


@pytest.fixture
def lab(tmp_path):
    e = make_env(tmp_path)
    yield e
    e.close()


def sign_in(env, user="alice"):
    """``(session, set_cookie_header_value)`` from the real session exchange."""
    headers = {"Authorization": "Bearer " + access(user), "X-Id-Token": ident(user), "Content-Length": "0"}
    status, h, body = env.request("POST", f"/s/{TOKEN}/session", headers)
    assert status == 200, body
    session = json.loads(body)["landing"].split("/")[4]
    return session, h.get("set-cookie")


def cookie_pair(set_cookie):
    return set_cookie.split(";", 1)[0].strip()


def get_api(env, session, cookie=None, **headers):
    sent = {**FORGED, **headers}
    if cookie is not None:
        sent["Cookie"] = cookie
    return env.request("GET", api(session), sent)


def post_api(env, session, cookie=None, body=None, **over):
    body = body or create_body()
    headers = good_headers(session, body, **over)
    headers.pop("Cookie", None)
    if cookie is not None:
        headers["Cookie"] = cookie
    return env.request("POST", api(session), headers, body)


def test_the_session_exchange_sets_a_scoped_httponly_strict_secure_cookie(lab):
    session, set_cookie = sign_in(lab)
    assert set_cookie is not None
    name, _, rest = set_cookie.partition("=")
    value, *attrs = (part.strip() for part in rest.split(";"))
    assert name == ss.write_cookie_name("shr-1", session) and re.fullmatch(r"hw_[0-9a-f]{16}", name)
    assert value == ss.write_cookie_value(SECRET, "shr-1", session)
    assert {"HttpOnly", "Secure", "SameSite=Strict", f"Path=/s/{TOKEN}/"} <= set(attrs)
    max_age = [int(a.split("=")[1]) for a in attrs if a.startswith("Max-Age=")]
    assert len(max_age) == 1 and 0 < max_age[0] <= 3600           # capped at the access token's remaining life
    assert session not in set_cookie and "Domain" not in set_cookie


def test_the_cookie_value_is_domain_separated_from_session_and_csrf():
    session = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email=EMAIL, basis="e")
    value = ss.write_cookie_value(SECRET, "shr-1", session)
    assert value != csrf_token(SECRET, "shr-1", session) and value != session.split(".")[4]
    assert ss._WRITE_DOMAIN not in (ss._DOMAIN, ss._CSRF_DOMAIN)
    assert value != ss.write_cookie_value(SECRET, "shr-2", session)
    assert not ss.check_csrf(SECRET, "shr-1", session, value)


def test_a_stolen_url_with_forged_headers_gets_nothing(lab):
    session, set_cookie = sign_in(lab)
    status, _, body = get_api(lab, session)                                  # read: the URL alone
    assert status == 404
    status, _, _ = post_api(lab, session)                                    # write: URL + valid CSRF + Origin, no cookie
    assert status == 404 and lines(lab) == []
    # the same requests with the cookie work
    assert get_api(lab, session, cookie_pair(set_cookie))[0] == 200
    assert post_api(lab, session, cookie_pair(set_cookie))[0] == 201 and len(lines(lab)) == 1


@pytest.mark.parametrize("make", [
    lambda s, c: c.replace("=", "=x", 1),                                                   # tampered value
    lambda s, c: c[:-1] + ("A" if c[-1] != "A" else "B"),                                   # last char changed
    lambda s, c: "hw_0000000000000000=" + c.split("=", 1)[1],                               # right value, wrong name
    lambda s, c: ss.write_cookie_name("shr-1", s) + "=",                                    # empty value
    lambda s, c: "other=" + c.split("=", 1)[1],                                             # another cookie name
    lambda s, c: ss.write_cookie_name("shr-1", s) + "=" + ss.write_cookie_value(SECRET, "shr-2", s),        # other share
    lambda s, c: ss.write_cookie_name("shr-1", s) + "=" + ss.write_cookie_value(b"z" * 48, "shr-1", s),     # other secret
])
def test_a_wrong_cookie_is_refused_for_read_and_write(lab, make):
    session, set_cookie = sign_in(lab)
    bad = make(session, cookie_pair(set_cookie))
    assert get_api(lab, session, bad)[0] == 404
    assert post_api(lab, session, bad)[0] == 404 and lines(lab) == []


def test_another_sessions_cookie_is_refused(lab):
    mine, mine_cookie = sign_in(lab, "alice")
    other = mint_session(SECRET, "shr-1", "alice", NOW0 + 3000, email=EMAIL, basis="e")
    other_cookie = f"{ss.write_cookie_name('shr-1', other)}={ss.write_cookie_value(SECRET, 'shr-1', other)}"
    assert get_api(lab, mine, other_cookie)[0] == 404                       # cookie of another session of the same user
    assert post_api(lab, mine, other_cookie)[0] == 404
    assert get_api(lab, other, other_cookie)[0] == 200                      # and it is good for its own session
    assert get_api(lab, other, cookie_pair(mine_cookie))[0] == 404


def test_an_expired_session_is_refused_even_with_its_cookie(lab):
    session, set_cookie = sign_in(lab)
    assert get_api(lab, session, cookie_pair(set_cookie))[0] == 200
    lab.clock[0] = NOW0 + 3601
    assert get_api(lab, session, cookie_pair(set_cookie))[0] == 404
    assert post_api(lab, session, cookie_pair(set_cookie))[0] == 404


def test_the_cookie_may_be_one_of_several_and_several_headers(lab):
    session, set_cookie = sign_in(lab)
    pair = cookie_pair(set_cookie)
    assert get_api(lab, session, f"a=1; {pair}; b=2")[0] == 200
    assert get_api(lab, session, f"hw_0000000000000000=junk; {pair}")[0] == 200


def test_pages_do_not_need_the_cookie(lab):
    """f/ and v/ and the a/ page must open from a cross-site navigation (the f/ page is an opaque origin)."""
    session = mint_session(SECRET, "shr-1", "alice", NOW0 + 3600, email=EMAIL, basis="e")
    for kind, name in (("f", "index.html"), ("a", "index.html"), ("v", "notes.txt")):
        status, _, _ = lab.request("GET", f"/s/{TOKEN}/k/{session}/{kind}/{name}", {"Sec-Fetch-Site": "cross-site"})
        assert status == 200, kind


def test_lab_pdfs_on_f_are_served_under_the_sandbox_policy(lab):
    """(public shares keep frame-ancestors only: test_share_public)"""
    session = mint_session(SECRET, "shr-1", "alice", NOW0 + 3600, email=EMAIL, basis="e")
    status, h, _ = lab.request("GET", f"/s/{TOKEN}/k/{session}/f/doc.pdf")
    assert status == 200 and h["content-type"] == "application/pdf"
    assert h["content-security-policy"].startswith("sandbox ")
