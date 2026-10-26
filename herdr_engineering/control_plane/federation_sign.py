"""Pinned-key request and export signing for server federation (ADR-0008 phase P2, Decision 6 and Owner decision D3).

Scheme ``herdr-federation-v1``: Ed25519 over a fixed, newline-delimited message. The library is ``cryptography``
(``cryptography.hazmat.primitives.asymmetric.ed25519``), already a dependency through the ``collab`` and
``review-gate`` extras (``PyJWT[crypto]``); P2 adds no package. It is imported lazily: without it
:func:`available` is False and federation answers 404, so a base install is unchanged.

A request is signed over: the scheme, the HTTP method, the RAW request target exactly as sent (path and query, before
any percent-decoding, so a re-encoded or re-ordered query is a different message), the timestamp, a nonce, the
SHA-256 of the body, the requesting peer id and the AUDIENCE (the receiving server's ``server_id``). The audience is
what stops a request signed for server A being replayed at server C that pins the same key. The timestamp bounds
replay to a window and the nonce makes each signature single-use inside it.

An export is signed over the exact response bytes behind a fixed domain-separation prefix, so a relay or cache
cannot alter it and an export can never be mistaken for a request signature.

Nothing here raises on bad input from the network: a verifier returns None / False.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict

SCHEME = "herdr-federation-v1"
H_PEER = "X-Herdr-Peer"
H_KEY = "X-Herdr-Key"
H_TS = "X-Herdr-Timestamp"
H_NONCE = "X-Herdr-Nonce"
H_SIG = "X-Herdr-Signature"
H_EXPORT_SIG = "X-Herdr-Export-Signature"
H_EXPORT_KEY = "X-Herdr-Export-Key"
REQUEST_HEADERS = (H_PEER, H_KEY, H_TS, H_NONCE, H_SIG)

_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
_TS_RE = re.compile(r"[0-9]{1,11}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_TARGET = 2048


def available() -> bool:
    """Is the signing library importable? (False: federation is off, answers 404.)"""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    except Exception:    # noqa: BLE001
        return False
    return True


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def request_message(method: str, target: str, ts: int, nonce: str, body_sha256: str, peer_id: str,
                    audience: str) -> bytes:
    """The exact bytes a request signature covers. Every field is validated by the caller (none may hold a newline)."""
    return "\n".join((SCHEME, "request", method, target, str(int(ts)), nonce, body_sha256, peer_id, audience)).encode()


def export_message(body: bytes) -> bytes:
    return f"{SCHEME}\nexport\n".encode() + body


# ----------------------------------------------------------------------------- keys
def seed_hex_new() -> str:
    return secrets.token_hex(32)


def public_key_hex(seed_hex: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def load_seed(path: str | None) -> str | None:
    """The 64-hex private seed in ``path``, or None when it is missing, unreadable, not a regular file, readable by
    group or others, or malformed. The file is the operator's secret: it is never logged and never in a response."""
    if not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > 256:
            return None
        text = os.read(fd, 257).decode("ascii", "ignore").strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    return text if _HEX64.fullmatch(text) else None


def _sign(seed_hex: str, message: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    return _b64e(ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)).sign(message))


def _verify(public_hex: str, signature_b64: str, message: bytes) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        raw = _b64d(signature_b64)
        if _b64e(raw) != signature_b64:                    # the last base64 character carries spare bits: one encoding only
            return False
        ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(raw, message)
        return True
    except Exception:    # noqa: BLE001 - InvalidSignature, a bad key, a missing library: all "no"
        return False


def sign_export(seed_hex: str, body: bytes) -> str:
    return _sign(seed_hex, export_message(body))


def verify_export(public_hex: str, body: bytes, signature_b64: str) -> bool:
    return _verify(public_hex, signature_b64, export_message(body))


# ----------------------------------------------------------------------------- requests
def sign_request(seed_hex: str, peer_id: str, audience: str, method: str, target: str, body: bytes = b"",
                 now: float | None = None, nonce: str | None = None) -> dict:
    """Client side (the P3 puller and the tests): the five headers for one request."""
    ts = int(time.time() if now is None else now)
    nonce = nonce or secrets.token_urlsafe(18)
    msg = request_message(method, target, ts, nonce, hashlib.sha256(body).hexdigest(), peer_id, audience)
    from .federation import key_fingerprint_of
    return {H_PEER: peer_id, H_KEY: key_fingerprint_of(public_key_hex(seed_hex)), H_TS: str(ts), H_NONCE: nonce,
            H_SIG: _sign(seed_hex, msg)}


def _one(headers, name: str) -> str | None:
    """The single value of header ``name``; None when it is absent OR repeated (a duplicate is refused outright)."""
    getter = getattr(headers, "get_all", None)
    if getter is not None:
        vals = getter(name) or []
    else:
        vals = [v for k, v in dict(headers).items() if k.lower() == name.lower()]
    if len(vals) != 1 or not isinstance(vals[0], str):
        return None
    return vals[0]


class ReplayGuard:
    """Single-use nonces inside the validity window. Bounded per peer; a full table refuses (fail closed)."""

    def __init__(self, per_peer: int = 16384):
        self._seen: dict[str, OrderedDict] = {}
        self._cap = per_peer
        self._lock = threading.Lock()

    def first_use(self, peer_id: str, nonce: str, now: float, ttl: float) -> bool:
        with self._lock:
            table = self._seen.setdefault(peer_id, OrderedDict())
            while table and next(iter(table.values())) <= now:
                table.popitem(last=False)
            if nonce in table or len(table) >= self._cap:
                return False
            table[nonce] = now + ttl
            return True


def verify_request(policy, *, method: str, target: str, headers, body: bytes, now: float,
                   replay: ReplayGuard | None = None):
    """Authenticate one request against ONE policy snapshot. Returns ``(PeerPolicy, full_fingerprint)`` or None.

    Every refusal is the same None: the caller answers one uniform 404, so a probe cannot tell an unknown peer from
    a disabled one, a stale clock from a wrong key, or an expired key from a bad signature."""
    if not policy.enabled or not policy.server_id or not available():
        return None
    if not isinstance(target, str) or len(target) > MAX_TARGET or not target.isascii() \
            or any(c in target for c in "\r\n\x00") or not isinstance(method, str) or method != method.upper():
        return None
    got = [_one(headers, h) for h in REQUEST_HEADERS]
    if any(v is None for v in got):
        return None
    peer_id, key_fp, ts_s, nonce, sig = got
    if not _HEX64.fullmatch(key_fp) or not _TS_RE.fullmatch(ts_s) or not _NONCE_RE.fullmatch(nonce):
        return None
    peer = policy.peer(peer_id)          # (an exact lookup: every configured id already matches PEER_ID_RE)
    if peer is None or not peer.enabled:
        return None
    ts = int(ts_s)
    if abs(now - ts) > policy.request_window_seconds:
        return None
    msg = request_message(method, target, ts, nonce, hashlib.sha256(body).hexdigest(), peer_id, policy.server_id)
    for public_hex, fp in peer.accepted_keys(now):
        if hmac.compare_digest(fp, key_fp) and _verify(public_hex, sig, msg):
            # consume the nonce only once the signature is good, so an unauthenticated sender cannot fill the table
            if replay is not None and not replay.first_use(peer_id, nonce, now, 2.0 * policy.request_window_seconds + 5):
                return None
            return peer, fp
    return None
