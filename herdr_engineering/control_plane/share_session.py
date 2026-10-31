"""Stateless lab-share sessions: an HMAC path segment binding (share id, verified identity, expiry).

A session is ``<b64url(identity)>.<b64url(email)>.<basis>.<exp>.<b64url(mac)>`` where ``mac = HMAC-SHA256(secret, domain
tag || len-prefixed share_id || identity || email || basis || exp)``. ``basis`` is how the viewer was admitted: ``e`` (their
email is on the share's allow-list) or ``g`` (a group claim); it is MAC'd, so a viewer cannot upgrade it. It is a bearer secret for one verified identity (and the verified
email it signed in with) on one share until ``exp`` (exclusive); the
server keeps no session state, so revoking the share (its ``state.json``) is what ends every live session at once.
Only the URL-safe alphabet and ``.`` appear, so a session never contains ``/`` and is safe as one path segment.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import re

_DOMAIN = b"herdr-share-session-v3"
BASES = ("e", "g")
_CSRF_DOMAIN = b"herdr-annotate-csrf-v1"
MIN_SECRET_BYTES = 32
MAX_IDENTITY = 128
MAX_EMAIL = 254
MAX_SESSION = 768
_IDENTITY_RE = re.compile(rf"[\x21-\x7e]{{1,{MAX_IDENTITY}}}")       # printable ASCII, no space
_EMAIL_RE = re.compile(rf"[\x21-\x7e]{{3,{MAX_EMAIL}}}")             # printable ASCII, no space
_ID_B64_MAX = 4 * MAX_IDENTITY // 3 + 4
_EMAIL_B64_MAX = 4 * MAX_EMAIL // 3 + 4
_SESSION_RE = re.compile(rf"([A-Za-z0-9_-]{{2,{_ID_B64_MAX}}})\.([A-Za-z0-9_-]{{4,{_EMAIL_B64_MAX}}})\."
                         rf"([eg])\.([1-9][0-9]{{0,11}})\.([A-Za-z0-9_-]{{43}})")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def _mac(secret: bytes, share_id: str, identity: str, email: str, basis: str, exp: int) -> bytes:
    msg = (_DOMAIN + b"\x00" + _field(share_id) + _field(identity) + _field(email) + _field(basis)
           + _field(str(exp)))
    return hmac.new(secret, msg, hashlib.sha256).digest()


def mint_session(secret: bytes, share_id: str, identity: str, exp: float, *, email: str, basis: str) -> str:
    """The session for ``identity`` on ``share_id`` valid until ``exp`` (epoch seconds, floored). Raises ValueError."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(share_id, str) or not share_id or len(share_id) > 128:
        raise ValueError("bad share id")
    if not isinstance(identity, str) or not _IDENTITY_RE.fullmatch(identity):
        raise ValueError("bad identity")
    if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email):
        raise ValueError("bad email")
    if basis not in BASES:
        raise ValueError("bad basis")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or not math.isfinite(exp) or exp < 1:
        raise ValueError("bad expiry")
    exp_int = int(math.floor(exp))
    if exp_int < 1 or exp_int >= 10 ** 12:
        raise ValueError("bad expiry")
    mac = _mac(secret, share_id, identity, email, basis, exp_int)
    return f"{_b64(identity.encode('ascii'))}.{_b64(email.encode('ascii'))}.{basis}.{exp_int}.{_b64(mac)}"


def check_session(secret: bytes, share_id: str, session: str, now: float) -> dict | None:
    """``{"identity", "exp", "email", "basis"}`` when ``session`` was minted with ``secret`` for ``share_id`` and ``now < exp``; else None.

    Never raises: malformed, oversize, tampered, truncated, expired or other-share sessions are all None.
    """
    try:
        if (not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES or not isinstance(share_id, str)
                or not share_id or not isinstance(session, str) or len(session) > MAX_SESSION):
            return None
        match = _SESSION_RE.fullmatch(session)
        if match is None:
            return None
        id_part, email_part, basis, exp_part, mac_part = match.groups()
        try:
            identity = _unb64(id_part).decode("ascii")
            email = _unb64(email_part).decode("ascii")
            given = _unb64(mac_part)
        except (binascii.Error, ValueError):
            return None
        if not _IDENTITY_RE.fullmatch(identity) or _b64(identity.encode("ascii")) != id_part:
            return None      # one canonical encoding only
        if not _EMAIL_RE.fullmatch(email) or _b64(email.encode("ascii")) != email_part:
            return None
        if _b64(given) != mac_part:
            return None
        exp = int(exp_part)
        if not hmac.compare_digest(given, _mac(secret, share_id, identity, email, basis, exp)):
            return None
        if not isinstance(now, (int, float)) or not now < exp:
            return None
        return {"identity": identity, "exp": float(exp), "email": email, "basis": basis}
    except Exception:
        return None


def csrf_token(secret: bytes, share_id: str, session: str) -> str:
    """The annotate CSRF token for one session of one share: b64url(HMAC-SHA256), 43 characters. Raises ValueError."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(share_id, str) or not share_id or not isinstance(session, str) or not session:
        raise ValueError("bad csrf input")
    msg = _CSRF_DOMAIN + b"\x00" + _field(share_id) + _field(session)
    return _b64(hmac.new(secret, msg, hashlib.sha256).digest())


def check_csrf(secret: bytes, share_id: str, session: str, value: object) -> bool:
    """True only when ``value`` is the exact token for this share and session (constant-time); never raises."""
    try:
        if not isinstance(value, str):
            return False
        expected = csrf_token(secret, share_id, session)
        return hmac.compare_digest(value.encode("ascii"), expected.encode("ascii"))
    except Exception:
        return False
