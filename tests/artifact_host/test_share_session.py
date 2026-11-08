"""Lab share sessions: stateless HMAC sessions and the Cognito allow-list (pure functions, real crypto)."""

import json
import re
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_artifact_host import share_session as ss
from herdr_artifact_host.cognito import CognitoAuthenticator
from herdr_artifact_host.share_lab import authorise_lab
from herdr_artifact_host.share_session import check_csrf, check_session, csrf_token, mint_session

POOL = "us-west-2_TestPool1"
ISS = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
CLI_CLIENT = "cli-client"
WEB_CLIENT = "web-client"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SECRET = b"k" * 48
NOW0 = 1_900_000_000.0  # PyJWT checks exp against the real clock, which is earlier


def jwks_doc(key=KEY, kid="k1"):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**d, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _encode(claims, key, kid):
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def access(user="alice", *, groups=(), client=WEB_CLIENT, exp=None, key=KEY, kid="k1", **over):
    claims = {
        "iss": ISS,
        "sub": f"sub-{user}",
        "username": user,
        "token_use": "access",
        "client_id": client,
        "cognito:groups": list(groups),
        "exp": NOW0 + 3600 if exp is None else exp,
        "iat": time.time(),
    }
    claims.update(over)
    return _encode(claims, key, kid)


def ident(
    user="alice",
    *,
    email="Alice@Lab.example",
    verified=True,
    aud=WEB_CLIENT,
    exp=None,
    key=KEY,
    kid="k1",
    **over,
):
    claims = {
        "iss": ISS,
        "sub": f"sub-{user}",
        "cognito:username": user,
        "token_use": "id",
        "aud": aud,
        "email": email,
        "email_verified": verified,
        "exp": NOW0 + 3600 if exp is None else exp,
        "iat": time.time(),
    }
    claims.update(over)
    return _encode(claims, key, kid)


LAB_STATE = {
    "share_id": "shr-1",
    "audience": "lab",
    "allow_emails": ["alice@lab.example", "carol@lab.example"],
    "allow_groups": ["Readers"],
}


def authenticator():
    return CognitoAuthenticator(
        pool_id=POOL,
        client_ids=[CLI_CLIENT, WEB_CLIENT],
        required_group="unused",
        jwks_fetch=lambda: jwks_doc(),
        open_signup=True,
    )


# ----------------------------------------------------------------------------- sessions


def test_session_round_trip_and_shape():
    s = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="e")
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", s) and "/" not in s and len(s) <= 768
    assert check_session(SECRET, "shr-1", s, NOW0) == {
        "identity": "alice",
        "exp": NOW0 + 100,
        "email": "alice@lab.example",
        "basis": "e",
    }
    assert check_session(SECRET, "shr-1", s, NOW0 + 99.9) is not None


def test_session_rejections():
    s = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="e")
    assert check_session(SECRET, "shr-2", s, NOW0) is None  # another share (associated data)
    assert check_session(b"x" * 48, "shr-1", s, NOW0) is None  # another secret
    assert check_session(SECRET, "shr-1", s, NOW0 + 100) is None  # expired (exp is exclusive)
    assert check_session(SECRET, "shr-1", s, NOW0 + 10_000) is None
    for i in range(len(s)):  # every single-character tamper
        for repl in ("A", "_"):
            if s[i] != repl:
                assert check_session(SECRET, "shr-1", s[:i] + repl + s[i + 1 :], NOW0) is None, i
    for cut in range(len(s)):  # every truncation
        assert check_session(SECRET, "shr-1", s[:cut], NOW0) is None
    assert check_session(SECRET, "shr-1", s + "A", NOW0) is None
    basis, exp, blob = s.split(".")
    assert (
        check_session(SECRET, "shr-1", ".".join(("g", exp, blob)), NOW0) is None
    )  # basis is authenticated
    assert (
        check_session(SECRET, "shr-1", ".".join((basis, str(int(exp) + 1000), blob)), NOW0) is None
    )  # so is exp
    for garbage in (
        None,
        1,
        b"bytes",
        "",
        "...",
        "a.b.c",
        "x" * 100_000,
        s.replace(".", "/"),
        s + "\n",
    ):
        assert check_session(SECRET, "shr-1", garbage, NOW0) is None
    assert check_session(b"", "shr-1", s, NOW0) is None
    assert check_session(SECRET, None, s, NOW0) is None


def test_mint_refuses_bad_inputs():
    mail = "alice@lab.example"
    for args in (
        (b"", "shr-1", "alice", NOW0),
        (b"short", "shr-1", "alice", NOW0),
        (SECRET, "", "alice", NOW0),
        (SECRET, "shr-1", "", NOW0),
        (SECRET, "shr-1", "a" * 300, NOW0),
        (SECRET, "shr-1", "a\x00b", NOW0),
        (SECRET, "shr-1", "alice", float("nan")),
        (SECRET, "shr-1", "alice", -1),
    ):
        with pytest.raises(ValueError):
            mint_session(*args, email=mail, basis="e")
    for bad in ("", "a b@c.d", "a@\u00e9.d", "x" * 255, "a\x00@b.c", None):
        with pytest.raises(ValueError):
            mint_session(SECRET, "shr-1", "alice", NOW0, email=bad, basis="e")


def test_session_basis_is_authenticated():
    e = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="e")
    g = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="g")
    assert check_session(SECRET, "shr-1", g, NOW0)["basis"] == "g" and e != g
    _, exp, blob = g.split(".")
    for flipped in ("e", "x", "G", ""):  # a viewer cannot upgrade or drop their basis
        assert check_session(SECRET, "shr-1", ".".join((flipped, exp, blob)), NOW0) is None
    assert check_session(SECRET, "shr-1", ".".join((exp, blob)), NOW0) is None
    for bad in ("", "x", None, "ge"):
        with pytest.raises(ValueError):
            mint_session(SECRET, "shr-1", "alice", NOW0, email="alice@lab.example", basis=bad)


def test_mint_requires_an_email():
    with pytest.raises(TypeError):
        mint_session(SECRET, "shr-1", "alice", NOW0)  # type: ignore[call-arg]


def test_old_format_sessions_are_gone():
    import base64
    import hashlib
    import hmac as hm

    ln = lambda v: len(v).to_bytes(4, "big") + v  # noqa: E731
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    exp = NOW0 + 100
    mac = hm.new(
        SECRET,
        b"herdr-share-session-v1\x00" + ln(b"shr-1") + ln(b"alice") + ln(str(exp).encode()),
        hashlib.sha256,
    ).digest()
    v1 = ".".join((b64(b"alice"), str(exp), b64(mac)))
    mac4 = hm.new(
        SECRET,
        b"herdr-share-session-v4\x00"
        + ln(b"shr-1")
        + ln(b"alice")
        + ln(b"alice@lab.example")
        + ln(b"e")
        + ln(str(exp).encode()),
        hashlib.sha256,
    ).digest()
    v4 = ".".join((b64(b"alice"), b64(b"x" * 33), "e", str(exp), b64(mac4)))
    for old in (v1, v4):
        assert check_session(SECRET, "shr-1", old, NOW0) is None


def test_longest_session_fits_the_limit():
    s = mint_session(SECRET, "shr-1", "a" * 128, NOW0 + 100, email="e" * 250 + "@b.c", basis="e")
    assert len(s) <= 768
    assert check_session(SECRET, "shr-1", s, NOW0)["email"] == "e" * 250 + "@b.c"


def test_csrf_token_binds_share_and_session():
    s1 = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="e")
    s2 = mint_session(SECRET, "shr-1", "bob", NOW0 + 100, email="bob@lab.example", basis="e")
    t = csrf_token(SECRET, "shr-1", s1)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", t)
    assert t == csrf_token(SECRET, "shr-1", s1)
    assert t != csrf_token(SECRET, "shr-1", s2) and t != csrf_token(SECRET, "shr-2", s1)
    assert t != csrf_token(b"y" * 48, "shr-1", s1)
    assert check_csrf(SECRET, "shr-1", s1, t)
    assert not check_csrf(SECRET, "shr-1", s2, t) and not check_csrf(SECRET, "shr-2", s1, t)
    flipped = t[:-1] + ("A" if t[-1] != "A" else "B")
    for bad in (flipped, t[:-1], t + "A", "", None, 5, b"x", "\u00e9" * 43):
        assert not check_csrf(SECRET, "shr-1", s1, bad)
    assert not check_csrf(b"", "shr-1", s1, t)


# ----------------------------------------------------------------------------- authorise_lab


def test_email_allows_without_any_group():
    got = authorise_lab(LAB_STATE, access("alice"), ident("alice"), authenticator())
    assert got == {
        "identity": "alice",
        "email": "alice@lab.example",
        "exp": NOW0 + 3600,
        "basis": "e",
    }


def test_non_ascii_emails_never_match():
    """``str.lower`` folds U+212A KELVIN SIGN to ASCII "k": a non-ASCII address must never match an ASCII entry
    (or a non-ASCII entry match anything)."""
    state = {
        **LAB_STATE,
        "allow_emails": ["kim@lab.example", "\u212aim@lab.example"],
        "allow_groups": [],
    }
    for email in ("\u212aim@lab.example", "\u212aIM@LAB.example", "kim@lab.exampl\u0435"):
        assert (
            authorise_lab(state, access("kim"), ident("kim", email=email), authenticator()) is None
        ), email
    assert (
        authorise_lab(state, access("kim"), ident("kim", email="KIM@lab.example"), authenticator())
        is not None
    )
    only_kelvin = {**state, "allow_emails": ["\u212aim@lab.example"]}
    assert (
        authorise_lab(
            only_kelvin, access("kim"), ident("kim", email="kim@lab.example"), authenticator()
        )
        is None
    )


def test_group_claim_allows_and_is_case_sensitive():
    tok = access("dave", groups=("Readers",))
    got = authorise_lab(LAB_STATE, tok, ident("dave", email="dave@else.example"), authenticator())
    assert got is not None and got["identity"] == "dave" and got["email"] == "dave@else.example"
    assert got["basis"] == "g"
    tok = access("dave", groups=("readers", "Admins"))
    assert (
        authorise_lab(LAB_STATE, tok, ident("dave", email="dave@else.example"), authenticator())
        is None
    )


def test_cli_client_is_allow_listed_too():
    tok = access("alice", client=CLI_CLIENT)
    assert (
        authorise_lab(LAB_STATE, tok, ident("alice", aud=CLI_CLIENT), authenticator()) is not None
    )


@pytest.mark.parametrize(
    "name,make",
    [
        ("in pool, not on list", lambda: (access("eve"), ident("eve", email="eve@lab.example"))),
        ("unverified email", lambda: (access("alice"), ident("alice", verified=False))),
        (
            "unverified email, group member",
            lambda: (access("alice", groups=("Readers",)), ident("alice", verified=False)),
        ),
        (
            "email_verified string false",
            lambda: (access("alice"), ident("alice", verified="false")),
        ),
        ("id token of another user", lambda: (access("eve"), ident("alice"))),
        (
            "id token of another user, eve in group",
            lambda: (access("eve", groups=("Readers",)), ident("alice")),
        ),
        (
            "access token from a foreign client",
            lambda: (access("alice", client="other-client"), ident("alice")),
        ),
        (
            "id token for a foreign client",
            lambda: (access("alice"), ident("alice", aud="other-client")),
        ),
        ("expired access token", lambda: (access("alice", exp=time.time() - 5), ident("alice"))),
        ("expired id token", lambda: (access("alice"), ident("alice", exp=time.time() - 5))),
        ("wrong issuer", lambda: (access("alice", iss=ISS + "x"), ident("alice"))),
        ("wrong issuer on id token", lambda: (access("alice"), ident("alice", iss=ISS + "x"))),
        ("signed by another key", lambda: (access("alice", key=OTHER_KEY), ident("alice"))),
        ("id token as access token", lambda: (ident("alice"), ident("alice"))),
        ("access token as id token", lambda: (access("alice"), access("alice"))),
        ("no email", lambda: (access("alice"), ident("alice", email=None))),
    ],
)
def test_identity_mix_ups_are_refused(name, make):
    a, i = make()
    assert authorise_lab(LAB_STATE, a, i, authenticator()) is None, name


def test_garbage_never_raises():
    good_a, good_i = access("alice"), ident("alice")
    auth = authenticator()
    for a, i in (
        (None, good_i),
        (good_a, None),
        (1, good_i),
        (b"x", good_i),
        ("", ""),
        ("x" * 9000, good_i),
        (good_a, "x" * 9000),
        (good_a + "x" * 9000, good_i),
        ("é", good_i),
        ("not.a.jwt", good_i),
    ):
        assert authorise_lab(LAB_STATE, a, i, auth) is None
    for state in (
        None,
        [],
        {},
        dict(LAB_STATE, audience="public"),
        dict(LAB_STATE, allow_emails="alice@lab.example"),
        dict(LAB_STATE, allow_groups=None, allow_emails=None),
        dict(LAB_STATE, allow_emails=[None, 3]),
    ):
        assert authorise_lab(state, good_a, good_i, auth) is None
    assert authorise_lab(LAB_STATE, good_a, good_i, None) is None


def test_blob_must_be_canonical_base64():
    s = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="e")
    basis, exp, blob = s.split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    assert len(blob) % 4 != 0
    unused = {2: 0b1111, 3: 0b11}[len(blob) % 4]
    last = alphabet.index(blob[-1])
    assert last & unused == 0
    twin = blob[:-1] + alphabet[last | 1]  # same decoded bytes, non-canonical text
    assert check_session(SECRET, "shr-1", s, NOW0) is not None
    assert check_session(SECRET, "shr-1", ".".join((basis, exp, twin)), NOW0) is None


def test_csrf_domain_is_separated_from_the_session():
    s = mint_session(SECRET, "shr-1", "alice", NOW0 + 100, email="alice@lab.example", basis="e")
    t = csrf_token(SECRET, "shr-1", s)
    assert t not in s and ss._CSRF_DOMAIN != ss._VERSION and ss._CSRF_DOMAIN != ss._KEY_LABEL
    assert check_session(SECRET, "shr-1", t, NOW0) is None  # a token is never a session


def test_session_url_discloses_neither_email_nor_identity():
    """The session is a URL path segment: no plain, base64 or hex form of the email or the user name may be readable from
    it, the length is the same for any user, and two mints for one user differ (random nonce)."""
    import base64
    import binascii

    email, user = "alice.longname@lab.example", "alice-user"
    s = mint_session(SECRET, "shr-1", user, NOW0 + 100, email=email, basis="e")
    t = mint_session(SECRET, "shr-1", "b", NOW0 + 100, email="b@x.yz", basis="e")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    blob = base64.urlsafe_b64decode(pad(s.split(".")[2]))
    for needle in (email, "@lab", "lab.example", "alice", user, "-user"):
        assert (
            needle not in s
            and needle.encode() not in blob
            and binascii.hexlify(needle.encode()).decode() not in s
        )
        assert base64.urlsafe_b64encode(needle.encode()).rstrip(b"=").decode() not in s
    assert len(s) == len(t)  # padded: the length reveals nothing
    assert check_session(SECRET, "shr-1", s, NOW0) == {
        "identity": user,
        "exp": NOW0 + 100,
        "email": email,
        "basis": "e",
    }
    assert s != mint_session(SECRET, "shr-1", user, NOW0 + 100, email=email, basis="e")
    assert check_session(b"B" * 32, "shr-1", s, NOW0) is None


def test_longest_values_fit_the_segment():
    s = mint_session(SECRET, "shr-1", "a" * 128, NOW0 + 100, email="e" * 250 + "@b.c", basis="g")
    assert len(s) <= 768
    assert check_session(SECRET, "shr-1", s, NOW0)["identity"] == "a" * 128


def test_modules_import_and_fail_clearly_without_cryptography():
    """deploy.sh runs render-share on a host python and ``api`` imports share_public: neither may need the package at
    import time; minting a session says what is missing and checking one fails closed."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'cryptography' or name.startswith('cryptography.'):\n"
        "            raise ImportError('blocked')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import herdr_artifact_host.share_session\n"
        "from herdr_engineering.control_plane import share_session as ss\n"
        "assert ss.MIN_SECRET_BYTES == 32\n"
        "try:\n"
        "    ss.mint_session(b'x' * 32, 'shr-1', 'alice', 2_000_000_000, email='a@b.co', basis='e')\n"
        "except RuntimeError as e:\n"
        "    assert 'cryptography' in str(e)\n"
        "else:\n"
        "    raise SystemExit('mint did not fail')\n"
        "assert ss.check_session(b'x' * 32, 'shr-1', 'e.2000000000.' + 'A' * 550, 1.0) is None\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[2])},
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-800:]


def test_claim_domain_and_key_labels_are_unchanged():
    assert ss.CLAIM_DOMAIN == b"herdr-claim-v1"
    assert ss._CLAIM_KEY_LABEL == b"herdr-claim-aead-key-v1"
    assert ss._KEY_LABELS[ss.CLAIM_DOMAIN] == ss._CLAIM_KEY_LABEL
