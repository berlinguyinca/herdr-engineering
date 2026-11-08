"""Member sessions: own domain tag, never interchangeable with share or claim sessions; sign-in itself is tested over HTTP."""

from __future__ import annotations

from herdr_artifact_host import member_session as ms
from herdr_artifact_host import share_session as ss
from tests.artifact_host.seed import SECRET

NOW = 1_000_000.0


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
