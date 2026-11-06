"""Stateless member-web sessions: an HMAC bearer binding one verified user id to an expiry.

``<b64url(identity)>.<exp>.<b64url(mac)>`` with ``mac = HMAC-SHA256(secret, domain || len-prefixed scope || identity ||
exp)``.  The domain tag (``herdr-member-session-v1``) and the fixed scope ``member`` are MAC'd, and the shape (three
parts, no email or basis) differs from a share session's five, so neither kind validates as the other whatever the
secret: a member session is refused by ``share_session.check_session`` and the reverse.  The session is sent as
``Authorization: Member <session>``; the server keeps no session state, so the per-request re-check of the user's
approval (done by the web layer) is what ends access.  ``share_session`` is deliberately left untouched.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import re

_DOMAIN = b"herdr-member-session-v1"
_SCOPE = "member"
MIN_SECRET_BYTES = 32
MAX_IDENTITY = 128
MAX_SESSION = 400
_IDENTITY_RE = re.compile(rf"[\x21-\x7e]{{1,{MAX_IDENTITY}}}")
_ID_B64_MAX = 4 * MAX_IDENTITY // 3 + 4
_SESSION_RE = re.compile(rf"([A-Za-z0-9_-]{{2,{_ID_B64_MAX}}})\.([1-9][0-9]{{0,11}})\.([A-Za-z0-9_-]{{43}})")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def _mac(secret: bytes, identity: str, exp: int) -> bytes:
    msg = _DOMAIN + b"\x00" + _field(_SCOPE) + _field(identity) + _field(str(exp))
    return hmac.new(secret, msg, hashlib.sha256).digest()


def mint_session(secret: bytes, identity: str, exp: float) -> str:
    """The session for ``identity`` valid until ``exp`` (epoch seconds, floored).  Raises ValueError."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(identity, str) or not _IDENTITY_RE.fullmatch(identity):
        raise ValueError("bad identity")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or not math.isfinite(exp) or exp < 1:
        raise ValueError("bad expiry")
    exp_int = int(math.floor(exp))
    if exp_int < 1 or exp_int >= 10 ** 12:
        raise ValueError("bad expiry")
    return f"{_b64(identity.encode('ascii'))}.{exp_int}.{_b64(_mac(secret, identity, exp_int))}"


def check_session(secret: bytes, session: object, now: float) -> dict | None:
    """``{"identity", "exp"}`` when ``session`` was minted with ``secret`` and ``now < exp``; else None.  Never raises."""
    try:
        if (not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES or not isinstance(session, str)
                or len(session) > MAX_SESSION):
            return None
        match = _SESSION_RE.fullmatch(session)
        if match is None:
            return None
        id_part, exp_part, mac_part = match.groups()
        identity = _unb64(id_part).decode("ascii")
        given = _unb64(mac_part)
        if not _IDENTITY_RE.fullmatch(identity) or _b64(identity.encode("ascii")) != id_part or _b64(given) != mac_part:
            return None                      # one canonical encoding only
        exp = int(exp_part)
        if not hmac.compare_digest(given, _mac(secret, identity, exp)):
            return None
        if not isinstance(now, (int, float)) or not now < exp:
            return None
        return {"identity": identity, "exp": float(exp)}
    except (binascii.Error, ValueError, UnicodeError):
        return None
