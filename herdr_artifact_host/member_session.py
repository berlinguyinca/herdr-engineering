"""Stateless member-web sessions: an HMAC bearer binding one verified user id to an expiry.

``<b64url(identity)>.<exp>.<b64url(mac)>`` with ``mac = HMAC-SHA256(secret, domain || len-prefixed scope || identity ||
exp)``.  The domain tag (``herdr-member-session-v1``) and the fixed scope ``member`` are MAC'd, and the two kinds differ in shape and key use: a share session is
``<basis>.<exp>.<AEAD blob>`` (ChaCha20-Poly1305, a single ``e``/``g`` first), so neither kind validates as the other
whatever the secret: a member session is refused by ``share_session.check_session`` and the reverse.  The session is sent as
``Authorization: Member <session>``; the server keeps no session state, so the per-request re-check of the user's
approval (done by the web layer) is what ends access.  ``share_session`` is a separate format.

``member_sign_in`` turns verified Cognito tokens into such a session. The public link's claim cookie
(``share_session.CLAIM_DOMAIN``) is a different cookie, format and key use: it is never accepted here, and a member
session is never accepted as a claim cookie.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

_DOMAIN = b"herdr-member-session-v1"
_SCOPE = "member"
MIN_SECRET_BYTES = 32
MAX_IDENTITY = 128
MAX_SESSION = 400
_IDENTITY_RE = re.compile(rf"[\x21-\x7e]{{1,{MAX_IDENTITY}}}")
_ID_B64_MAX = 4 * MAX_IDENTITY // 3 + 4
_SESSION_RE = re.compile(
    rf"([A-Za-z0-9_-]{{2,{_ID_B64_MAX}}})\.([1-9][0-9]{{0,11}})(?:_([1-9][0-9]{{0,14}}))?\.([A-Za-z0-9_-]{{43}})"
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def _mac(secret: bytes, identity: str, exp: int, iat: int | None = None) -> bytes:
    msg = _DOMAIN + b"\x00" + _field(_SCOPE) + _field(identity) + _field(str(exp))
    if iat is not None:
        msg += _field(str(iat))
    return hmac.new(secret, msg, hashlib.sha256).digest()


def mint_session(secret: bytes, identity: str, exp: float, iat_ms: int | None = None) -> str:
    """The session for ``identity`` valid until ``exp`` (epoch seconds, floored).  ``iat_ms`` (issue time, epoch
    milliseconds) is MAC'd and carried when given, so a per-member ``sessions_valid_after`` can end older sessions.
    Raises ValueError."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(identity, str) or not _IDENTITY_RE.fullmatch(identity):
        raise ValueError("bad identity")
    if (
        not isinstance(exp, (int, float))
        or isinstance(exp, bool)
        or not math.isfinite(exp)
        or exp < 1
    ):
        raise ValueError("bad expiry")
    exp_int = int(math.floor(exp))
    if exp_int < 1 or exp_int >= 10**12:
        raise ValueError("bad expiry")
    if iat_ms is not None and (
        isinstance(iat_ms, bool) or not isinstance(iat_ms, int) or not 1 <= iat_ms < 10**15
    ):
        raise ValueError("bad issue time")
    stamp = "" if iat_ms is None else f"_{iat_ms}"
    return f"{_b64(identity.encode('ascii'))}.{exp_int}{stamp}.{_b64(_mac(secret, identity, exp_int, iat_ms))}"


def check_session(secret: bytes, session: object, now: float) -> dict | None:
    """``{"identity", "exp"}`` when ``session`` was minted with ``secret`` and ``now < exp``; else None.  Never raises."""
    try:
        if (
            not isinstance(secret, bytes)
            or len(secret) < MIN_SECRET_BYTES
            or not isinstance(session, str)
            or len(session) > MAX_SESSION
        ):
            return None
        match = _SESSION_RE.fullmatch(session)
        if match is None:
            return None
        id_part, exp_part, iat_part, mac_part = match.groups()
        identity = _unb64(id_part).decode("ascii")
        given = _unb64(mac_part)
        if (
            not _IDENTITY_RE.fullmatch(identity)
            or _b64(identity.encode("ascii")) != id_part
            or _b64(given) != mac_part
        ):
            return None  # one canonical encoding only
        exp = int(exp_part)
        iat = int(iat_part) if iat_part else None
        if not hmac.compare_digest(given, _mac(secret, identity, exp, iat)):
            return None
        if not isinstance(now, (int, float)) or not now < exp:
            return None
        out: dict = {"identity": identity, "exp": float(exp)}
        if iat is not None:
            out["iat_ms"] = iat
        return out
    except (binascii.Error, ValueError, UnicodeError):
        return None


AUTH_SCHEME = "Member"


def session_from_header(value: str | None) -> str | None:
    """The session of an ``Authorization: Member <session>`` header value, or None for any other shape."""
    if value is None:
        return None
    scheme, sep, rest = value.partition(" ")
    if scheme != AUTH_SCHEME or not sep or rest != rest.strip():
        return None
    return rest


class Verifier(Protocol):
    def verify_claims(self, token: str) -> dict | None: ...

    def verify_id_token(self, token: str, *, user: str) -> str | None: ...


@dataclass(frozen=True)
class MemberGrant:
    identity: str
    email: str
    session: str
    exp: int


def member_sign_in(
    verifier: Verifier,
    secret: bytes,
    access_token: str,
    id_token: str,
    *,
    session_seconds: int,
    now: Callable[[], float] = time.time,
) -> MemberGrant | None:
    """Exchange a Cognito access token plus ID token for a member session; None on any failure.

    The session ends at the earlier of ``session_seconds`` from now and the access token's own expiry. The email
    comes only from the ID token that names the same user as the access token."""
    try:
        if not (access_token.isascii() and id_token.isascii()):
            return None
        claims = verifier.verify_claims(access_token)
        user = claims.get("user") if claims else None
        exp = claims.get("exp") if claims else None
        if not isinstance(user, str) or not isinstance(exp, (int, float)) or isinstance(exp, bool):
            return None
        email = verifier.verify_id_token(id_token, user=user)
        if not isinstance(email, str) or not email:
            return None
        until = int(min(now() + session_seconds, float(exp)) // 1)
        if until <= now():
            return None
        return MemberGrant(user, email, mint_session(secret, user, until), until)
    except Exception:
        return None
