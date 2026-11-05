"""Stateless lab-share sessions: an HMAC path segment binding (share id, verified identity, expiry).

A session is ``<b64url(identity)>.<b64url(iv || encrypted email)>.<basis>.<exp>.<b64url(mac)>``: the signed-in email is
ENCRYPTED in the URL (HMAC-SHA256 counter-mode keystream under a key derived from the signing secret, synthetic IV), so a
URL that leaks (history, Referer, a pasted link) discloses no address; the server decrypts after the MAC verifies. ``mac`` is
the HMAC over the plaintext fields, where ``mac = HMAC-SHA256(secret, domain
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

_DOMAIN = b"herdr-share-session-v4"
_ENC_DOMAIN = b"herdr-share-session-email-enc-v1"
_IV_DOMAIN = b"herdr-share-session-email-iv-v1"
_IV_BYTES = 16
BASES = ("e", "g")
_CSRF_DOMAIN = b"herdr-annotate-csrf-v1"
MIN_SECRET_BYTES = 32
MAX_IDENTITY = 128
MAX_EMAIL = 254
MAX_SESSION = 768
_IDENTITY_RE = re.compile(rf"[\x21-\x7e]{{1,{MAX_IDENTITY}}}")       # printable ASCII, no space
_EMAIL_RE = re.compile(rf"[\x21-\x7e]{{3,{MAX_EMAIL}}}")             # printable ASCII, no space
_ID_B64_MAX = 4 * MAX_IDENTITY // 3 + 4
_EMAIL_B64_MAX = 4 * (MAX_EMAIL + _IV_BYTES) // 3 + 4
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


def _keystream_xor(secret: bytes, iv: bytes, data: bytes) -> bytes:
    key = hmac.new(secret, _ENC_DOMAIN, hashlib.sha256).digest()
    out = bytearray()
    for counter in range(0, (len(data) + 31) // 32):
        block = hmac.new(key, iv + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out += block
    return bytes(a ^ b for a, b in zip(data, out))


def _seal_email(secret: bytes, mac: bytes, email: str) -> bytes:
    """``iv || ciphertext``: the IV is derived from the session MAC (unique per distinct session fields)."""
    iv = hmac.new(secret, _IV_DOMAIN + mac, hashlib.sha256).digest()[:_IV_BYTES]
    return iv + _keystream_xor(secret, iv, email.encode("ascii"))


def _open_email(secret: bytes, sealed: bytes) -> str:
    iv, body = sealed[:_IV_BYTES], sealed[_IV_BYTES:]
    return _keystream_xor(secret, iv, body).decode("ascii")


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
    return f"{_b64(identity.encode('ascii'))}.{_b64(_seal_email(secret, mac, email))}.{basis}.{exp_int}.{_b64(mac)}"


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
            sealed = _unb64(email_part)
            if len(sealed) < _IV_BYTES + 3:
                return None
            email = _open_email(secret, sealed)
            given = _unb64(mac_part)
        except (binascii.Error, ValueError):
            return None
        if not _IDENTITY_RE.fullmatch(identity) or _b64(identity.encode("ascii")) != id_part:
            return None      # one canonical encoding only
        if not _EMAIL_RE.fullmatch(email) or _b64(sealed) != email_part:
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


# The write cookie. A session lives in the URL path, so anything that can read the URL (a script on an f/ page, a
# Referer sent by an author <meta name=referrer>) holds the whole credential. The annotation routes therefore ALSO
# require this cookie: HttpOnly (no script reads it), Secure, SameSite=Strict (never sent from the opaque-origin f/
# page or any cross-site context), Path=/s/<token>/. Its value is a MAC under its own domain label, so it is derived
# from nothing a URL contains and equals neither the session MAC nor the CSRF token.
_WRITE_DOMAIN = b"herdr-annotate-write-v1"
_COOKIE_VALUE_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def write_cookie_name(share_id: str, session: str) -> str:
    """``hw_`` + 16 hex of SHA-256(share_id, session): one cookie per session, so two sessions never overwrite each other."""
    digest = hashlib.sha256(_WRITE_DOMAIN + b"\x00name\x00" + _field(share_id) + _field(session)).hexdigest()
    return "hw_" + digest[:16]


def write_cookie_value(secret: bytes, share_id: str, session: str) -> str:
    """b64url(HMAC-SHA256(secret, write domain || share_id || session)); raises ValueError like ``csrf_token``."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(share_id, str) or not share_id or not isinstance(session, str) or not session:
        raise ValueError("bad cookie input")
    msg = _WRITE_DOMAIN + b"\x00" + _field(share_id) + _field(session)
    return _b64(hmac.new(secret, msg, hashlib.sha256).digest())


def write_cookie_header(secret: bytes, share_id: str, token: str, session: str, max_age: int) -> str:
    """The ``Set-Cookie`` value for a freshly minted session; ``max_age`` is the session's remaining seconds."""
    return (f"{write_cookie_name(share_id, session)}={write_cookie_value(secret, share_id, session)}; "
            f"Max-Age={max(int(max_age), 0)}; Path=/s/{token}/; HttpOnly; Secure; SameSite=Strict")


def check_write_cookie(secret: bytes, share_id: str, session: str, cookie_headers) -> bool:
    """True when one of the ``Cookie`` header lines carries this session's cookie with the right MAC; never raises."""
    try:
        name = write_cookie_name(share_id, session)
        expected = write_cookie_value(secret, share_id, session).encode("ascii")
        found = False
        for line in cookie_headers or ():
            for pair in line.split(";"):
                key, sep, value = pair.strip().partition("=")
                if sep and key == name and _COOKIE_VALUE_RE.fullmatch(value) \
                        and hmac.compare_digest(value.encode("ascii"), expected):
                    found = True
        return found
    except Exception:
        return False
