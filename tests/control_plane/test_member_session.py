"""Member sessions: own domain tag, never interchangeable with share sessions (Task 11)."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane import member_session as ms
from herdr_engineering.control_plane import share_session as ss

SECRET = b"k" * 40
NOW = 1_000_000.0


def test_round_trip():
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    assert ms.check_session(SECRET, s, NOW) == {"identity": "user-1", "exp": NOW + 60}


def test_expiry_is_exclusive_and_wrong_secret_fails():
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    assert ms.check_session(SECRET, s, NOW + 60) is None
    assert ms.check_session(b"x" * 40, s, NOW) is None


def test_tamper_and_garbage_are_none():
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    for bad in (s[:-1], s + "A", "", "a.b", s.replace("dXNl", "ZXZp"), None, 5, "." * 10, s.upper()):
        assert ms.check_session(SECRET, bad, NOW) is None


def test_identity_is_bound():
    a = ms.mint_session(SECRET, "user-1", NOW + 60)
    b = ms.mint_session(SECRET, "user-2", NOW + 60)
    ida, _, _, mac_a = a.partition(".")[0], None, None, a.rsplit(".", 1)[1]
    forged = b.rsplit(".", 1)[0] + "." + mac_a
    assert ida and ms.check_session(SECRET, forged, NOW) is None


def test_share_session_is_not_a_member_session():
    share = ss.mint_session(SECRET, "member", "user-1", NOW + 60, email="a@b.org", basis="g")
    assert ms.check_session(SECRET, share, NOW) is None


def test_member_session_is_not_a_share_session_whatever_the_share_id():
    s = ms.mint_session(SECRET, "user-1", NOW + 60)
    for share_id in ("member", "abc", "user-1"):
        assert ss.check_session(SECRET, share_id, s, NOW) is None


def test_domain_tags_differ():
    assert ms._DOMAIN != ss._DOMAIN and ms._DOMAIN == b"herdr-member-session-v1"


@pytest.mark.parametrize("args", [(b"short", "u", NOW), (SECRET, "", NOW), (SECRET, "a b", NOW),
                                  (SECRET, "u", 0), (SECRET, "u", float("nan")), (SECRET, "u", True)])
def test_bad_mint_inputs_raise(args):
    with pytest.raises(ValueError):
        ms.mint_session(*args)
