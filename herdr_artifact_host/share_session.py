"""Stateless lab-share sessions: an AEAD path segment binding (share id, verified identity, email, basis, expiry).

A session is ``<basis>.<exp>.<b64url(nonce || ciphertext || tag)>`` (version 5). The ciphertext is ChaCha20-Poly1305 over a
fixed-size block holding the verified identity and the verified email (padded, so the length reveals neither), under a key
derived from the signing secret with its own label. The associated data is the version label, the share id, the basis and the
expiry, so none of them can be changed or moved to another share. Nothing user-derived appears in the clear: a URL that
leaks (history, Referer, a pasted link) discloses neither address nor user name. The tag is verified BEFORE anything is
parsed, and every failure (malformed, tampered, truncated, wrong share, old format, expired) is the same ``None``.
``basis`` is how the viewer was admitted: ``e`` (email on the allow-list) or ``g`` (group claim); ``exp`` is exclusive. The
server keeps no session state, so revoking the share (its ``state.json``) ends every live session at once.
Only the URL-safe alphabet and ``.`` appear, so a session never contains ``/`` and is safe as one path segment.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import os
import re

_VERSION = b"herdr-share-session-v5"
_KEY_LABEL = b"herdr-share-session-aead-key-v5"
_DOMAIN = _VERSION  # the lab-session domain (the default)
CLAIM_DOMAIN = b"herdr-claim-v1"  # the public link's verified-visitor cookie: never valid as a lab session, nor back
LAB_DOMAIN = _DOMAIN
_CLAIM_KEY_LABEL = b"herdr-claim-aead-key-v1"
_KEY_LABELS = {
    LAB_DOMAIN: _KEY_LABEL,
    CLAIM_DOMAIN: _CLAIM_KEY_LABEL,
}  # each domain has its own derived key
BASES = ("e", "g")
_CSRF_DOMAIN = b"herdr-annotate-csrf-v1"
MIN_SECRET_BYTES = 32
MAX_IDENTITY = 128
MAX_EMAIL = 254
MAX_SESSION = 768
_NONCE = 12
_PLAIN = 1 + MAX_IDENTITY + 1 + MAX_EMAIL  # idlen, identity (padded), emaillen, email (padded)
_BLOB_B64 = (
    (_NONCE + _PLAIN + 16) * 4 + 2
) // 3  # unpadded base64 length of nonce || ciphertext || tag
_IDENTITY_RE = re.compile(rf"[\x21-\x7e]{{1,{MAX_IDENTITY}}}")  # printable ASCII, no space
_EMAIL_RE = re.compile(rf"[\x21-\x7e]{{3,{MAX_EMAIL}}}")  # printable ASCII, no space
_SESSION_RE = re.compile(rf"([eg])\.([1-9][0-9]{{0,11}})\.([A-Za-z0-9_-]{{{_BLOB_B64}}})")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def _aead(secret: bytes, domain: bytes = _DOMAIN):
    """The session cipher. ``cryptography`` is imported here, not at module level, so this module (``MIN_SECRET_BYTES``,
    the CSRF and cookie helpers) and everything that imports it load on a host without the package."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    except ImportError as exc:
        raise RuntimeError(
            "lab-share sessions need the 'cryptography' package (pip install 'herdr-engineering[share]')"
        ) from exc
    return ChaCha20Poly1305(hmac.new(secret, _KEY_LABELS[domain], hashlib.sha256).digest())


def _ad(share_id: str, basis: str, exp: int, domain: bytes = _DOMAIN) -> bytes:
    """Associated data. For the lab domain this is exactly the version label; the claim domain has its own label AND its own
    key, so a claim cookie never decrypts as a lab session and a lab session never as a claim cookie."""
    return domain + b"\x00" + _field(share_id) + _field(basis) + _field(str(exp))


def mint_session(
    secret: bytes,
    share_id: str,
    identity: str,
    exp: float,
    *,
    email: str,
    basis: str,
    domain: bytes = _DOMAIN,
) -> str:
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
    if domain not in (LAB_DOMAIN, CLAIM_DOMAIN):
        raise ValueError("bad domain")
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
    ident, mail = identity.encode("ascii"), email.encode("ascii")
    plain = (
        bytes([len(ident)])
        + ident.ljust(MAX_IDENTITY, b"\x00")
        + bytes([len(mail)])
        + mail.ljust(MAX_EMAIL, b"\x00")
    )
    nonce = os.urandom(_NONCE)
    blob = nonce + _aead(secret, domain).encrypt(
        nonce, plain, _ad(share_id, basis, exp_int, domain)
    )
    return f"{basis}.{exp_int}.{_b64(blob)}"


def check_session(
    secret: bytes, share_id: str, session: str, now: float, domain: bytes = _DOMAIN
) -> dict | None:
    """``{"identity", "exp", "email", "basis"}`` when ``session`` was minted with ``secret`` for ``share_id`` in ``domain`` and ``now < exp``; else None.

    Never raises, and every failure is the same None: the tag is checked before anything inside is parsed.
    """
    try:
        if (
            not isinstance(secret, bytes)
            or len(secret) < MIN_SECRET_BYTES
            or not isinstance(share_id, str)
            or not share_id
            or not isinstance(session, str)
            or len(session) > MAX_SESSION
        ):
            return None
        match = _SESSION_RE.fullmatch(session)
        if match is None:
            return None
        basis, exp_part, blob_part = match.groups()
        try:
            blob = _unb64(blob_part)
        except (binascii.Error, ValueError):
            return None
        if _b64(blob) != blob_part:
            return None  # one canonical encoding only
        exp = int(exp_part)
        cipher = _aead(
            secret, domain
        )  # a missing package raises RuntimeError: caught below, so every check fails closed
        try:
            plain = cipher.decrypt(blob[:_NONCE], blob[_NONCE:], _ad(share_id, basis, exp, domain))
        except Exception:  # noqa: BLE001 - InvalidTag, or any malformed input: one uniform answer
            return None
        n = plain[0]
        m = plain[1 + MAX_IDENTITY]
        if n > MAX_IDENTITY or m > MAX_EMAIL:
            return None
        identity = plain[1 : 1 + n].decode("ascii")
        email = plain[2 + MAX_IDENTITY : 2 + MAX_IDENTITY + m].decode("ascii")
        if not _IDENTITY_RE.fullmatch(identity) or not _EMAIL_RE.fullmatch(email):
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
    digest = hashlib.sha256(
        _WRITE_DOMAIN + b"\x00name\x00" + _field(share_id) + _field(session)
    ).hexdigest()
    return "hw_" + digest[:16]


def write_cookie_value(secret: bytes, share_id: str, session: str) -> str:
    """b64url(HMAC-SHA256(secret, write domain || share_id || session)); raises ValueError like ``csrf_token``."""
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("signing secret is missing or too short")
    if not isinstance(share_id, str) or not share_id or not isinstance(session, str) or not session:
        raise ValueError("bad cookie input")
    msg = _WRITE_DOMAIN + b"\x00" + _field(share_id) + _field(session)
    return _b64(hmac.new(secret, msg, hashlib.sha256).digest())


def write_cookie_header(
    secret: bytes, share_id: str, token: str, session: str, max_age: int
) -> str:
    """The ``Set-Cookie`` value for a freshly minted session; ``max_age`` is the session's remaining seconds."""
    return (
        f"{write_cookie_name(share_id, session)}={write_cookie_value(secret, share_id, session)}; "
        f"Max-Age={max(int(max_age), 0)}; Path=/s/{token}/; HttpOnly; Secure; SameSite=Strict"
    )


def check_write_cookie(secret: bytes, share_id: str, session: str, cookie_headers) -> bool:
    """True when one of the ``Cookie`` header lines carries this session's cookie with the right MAC; never raises."""
    try:
        name = write_cookie_name(share_id, session)
        expected = write_cookie_value(secret, share_id, session).encode("ascii")
        found = False
        for line in cookie_headers or ():
            for pair in line.split(";"):
                key, sep, value = pair.strip().partition("=")
                if (
                    sep
                    and key == name
                    and _COOKIE_VALUE_RE.fullmatch(value)
                    and hmac.compare_digest(value.encode("ascii"), expected)
                ):
                    found = True
        return found
    except Exception:
        return False
