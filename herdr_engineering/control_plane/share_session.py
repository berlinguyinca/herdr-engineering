"""Stateless lab-share sessions: an HMAC path segment binding (share id, verified identity, expiry).

A session is ``<b64url(identity)>.<exp>.<b64url(mac)>`` where ``mac = HMAC-SHA256(secret, domain tag || len-prefixed
share_id || identity || exp)``. It is a bearer secret for one identity and one share until ``exp`` (exclusive); the
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

_DOMAIN = b"herdr-share-session-v1"
MIN_SECRET_BYTES = 32
MAX_IDENTITY = 128
MAX_SESSION = 512
_IDENTITY_RE = re.compile(rf"[\x21-\x7e]{{1,{MAX_IDENTITY}}}")       # printable ASCII, no space
_ID_B64_MAX = 4 * MAX_IDENTITY // 3 + 4
_SESSION_RE = re.compile(rf"([A-Za-z0-9_-]{{2,{_ID_B64_MAX}}})\.([1-9][0-9]{{0,11}})\.([A-Za-z0-9_-]{{43}})")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def _mac(secret: bytes, share_id: str, identity: str, exp: int) -> bytes:
    msg = _DOMAIN + b"\x00" + _field(share_id) + _field(identity) + _field(str(exp))
    return hmac.new(secret, msg, hashlib.sha256).digest()


def mint_session(secret: bytes, share_id: str, identity: str, exp: float) -> str:
    """The session for ``identity`` on ``share_id`` valid until ``exp`` (epoch seconds, floored). Raises ValueError."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(share_id, str) or not share_id or len(share_id) > 128:
        raise ValueError("bad share id")
    if not isinstance(identity, str) or not _IDENTITY_RE.fullmatch(identity):
        raise ValueError("bad identity")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or not math.isfinite(exp) or exp < 1:
        raise ValueError("bad expiry")
    exp_int = int(math.floor(exp))
    if exp_int < 1 or exp_int >= 10 ** 12:
        raise ValueError("bad expiry")
    return f"{_b64(identity.encode('ascii'))}.{exp_int}.{_b64(_mac(secret, share_id, identity, exp_int))}"


def check_session(secret: bytes, share_id: str, session: str, now: float) -> dict | None:
    """``{"identity", "exp"}`` when ``session`` was minted with ``secret`` for ``share_id`` and ``now < exp``; else None.

    Never raises: malformed, oversize, tampered, truncated, expired or other-share sessions are all None.
    """
    try:
        if (not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES or not isinstance(share_id, str)
                or not share_id or not isinstance(session, str) or len(session) > MAX_SESSION):
            return None
        match = _SESSION_RE.fullmatch(session)
        if match is None:
            return None
        id_part, exp_part, mac_part = match.groups()
        try:
            identity = _unb64(id_part).decode("ascii")
            given = _unb64(mac_part)
        except (binascii.Error, ValueError):
            return None
        if not _IDENTITY_RE.fullmatch(identity) or _b64(identity.encode("ascii")) != id_part:
            return None      # one canonical encoding only
        if _b64(given) != mac_part:
            return None
        exp = int(exp_part)
        if not hmac.compare_digest(given, _mac(secret, share_id, identity, exp)):
            return None
        if not isinstance(now, (int, float)) or not now < exp:
            return None
        return {"identity": identity, "exp": float(exp)}
    except Exception:
        return None
