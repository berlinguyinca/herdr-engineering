"""Member sessions: own domain tag, never interchangeable with share or claim sessions; Cognito sign-in to a session."""

from __future__ import annotations

import time

from herdr_artifact_host import member_session as ms
from herdr_artifact_host import share_session as ss
from herdr_artifact_host.cognito import CognitoAuthenticator
from tests.artifact_host.seed import (
    CLI_CLIENT,
    OTHER_KEY,
    POOL_ID,
    SECRET,
    WEB_CLIENT,
    access,
    ident,
    jwks_doc,
)

NOW = 1_000_000.0


def authenticator() -> CognitoAuthenticator:
    return CognitoAuthenticator(
        pool_id=POOL_ID,
        client_ids=[CLI_CLIENT, WEB_CLIENT],
        required_group="unused",
        jwks_fetch=jwks_doc,
        open_signup=True,
    )


def test_round_trip_expiry_and_secret() -> None:
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    assert ms.check_session(SECRET, s, NOW) == {"identity": "user-1", "exp": NOW + 60}
    assert ms.check_session(SECRET, s, NOW + 60) is None
    assert ms.check_session(b"x" * 40, s, NOW) is None


def test_tamper_and_garbage() -> None:
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    for bad in (s[:-1], s + "A", "", "a.b", None, 5, "." * 10, s.upper()):
        assert ms.check_session(SECRET, bad, NOW) is None


def test_share_and_claim_sessions_are_not_member_sessions() -> None:
    lab = ss.mint_session(SECRET, "member", "user-1", NOW + 60, email="a@b.org", basis="g")
    claim = ss.mint_session(
        SECRET, "member", "user-1", NOW + 60, email="a@b.org", basis="g", domain=ss.CLAIM_DOMAIN
    )
    assert ms.check_session(SECRET, lab, NOW) is None
    assert ms.check_session(SECRET, claim, NOW) is None


def test_member_session_is_not_a_share_or_claim_session() -> None:
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    for share_id in ("member", "abc", "user-1"):
        assert ss.check_session(SECRET, share_id, s, NOW) is None
        assert ss.check_session(SECRET, share_id, s, NOW, ss.CLAIM_DOMAIN) is None


def test_claim_and_lab_sessions_are_distinct() -> None:
    lab = ss.mint_session(SECRET, "shr", "u", NOW + 60, email="a@b.org", basis="g")
    claim = ss.mint_session(
        SECRET, "shr", "u", NOW + 60, email="a@b.org", basis="g", domain=ss.CLAIM_DOMAIN
    )
    assert ss.check_session(SECRET, "shr", lab, NOW, ss.CLAIM_DOMAIN) is None
    assert ss.check_session(SECRET, "shr", claim, NOW) is None
    assert ss.check_session(SECRET, "shr", claim, NOW, ss.CLAIM_DOMAIN) is not None


def test_header_parsing() -> None:
    assert ms.session_from_header("Member abc") == "abc"
    for bad in (None, "", "Member", "Member  abc", "member abc", "Bearer abc", "Member abc "):
        assert ms.session_from_header(bad) is None


def test_sign_in_with_real_tokens() -> None:
    grant = ms.member_sign_in(authenticator(), SECRET, access(), ident(), session_seconds=600)
    assert (
        grant is not None
        and grant.identity == "alice"
        and grant.email.lower() == "alice@lab.example"
    )
    assert ms.check_session(SECRET, grant.session, time.time()) == {
        "identity": "alice",
        "exp": grant.exp,
    }
    assert grant.exp <= time.time() + 600


def test_sign_in_ends_at_token_expiry() -> None:
    grant = ms.member_sign_in(
        authenticator(), SECRET, access(exp=time.time() + 30), ident(), session_seconds=600
    )
    assert grant is not None and grant.exp <= time.time() + 31


def test_sign_in_failures() -> None:
    auth = authenticator()
    cases = [
        (access(key=OTHER_KEY), ident()),
        (access(), ident(key=OTHER_KEY)),
        (access(), ident(verified=False)),
        (access("alice"), ident("bob")),  # an ID token of another user
        (access(exp=time.time() - 5), ident()),
        (access(client="stranger"), ident()),
        ("not-a-token", ident()),
        ("é", ident()),
    ]
    for acc, idt in cases:
        assert ms.member_sign_in(auth, SECRET, acc, idt, session_seconds=600) is None
