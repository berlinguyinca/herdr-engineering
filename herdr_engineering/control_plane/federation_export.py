"""Signed, read-only federation export endpoints (ADR-0008 phase P2, Decision 6 and Decision 12).

``GET /api/v1/federation/shares`` and ``GET /api/v1/federation/shares/<share_id>/events?cursor=&limit=``. Nothing else:
no write, no other verb, no other path. :class:`FederationGateway` is the whole decision, kept free of ``http.server``
so every guard is testable in process; ``api.py`` only moves bytes.

Order of every request, ONE policy snapshot throughout:

1. federation off, no ``server_id``, no readable signing key, no signing library, a degraded collab log, or any verb but
   GET: uniform 404, before any signature work;
2. verify the request signature against the FULL pinned key (``federation_sign.verify_request``): uniform 404;
3. the peer's per-minute request quota and hourly byte quota: 429 (an authenticated peer may be told);
4. authorize the share (``CollabService.federation_events``): unshared, revoked, unlisted, another peer's: uniform 404;
5. build the export from the field allowlist, redacted, with a bounded validity window, serialize ONCE, sign those
   exact bytes and send them unchanged.

Every refusal before step 3 is the byte-identical 404, so a probe learns nothing about which guard fired.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from urllib.parse import parse_qsl, urlsplit

from . import federation_sign as sign
from .federation import key_fingerprint_of, policy_from_source

_DIGITS = re.compile(r"[0-9]{1,16}\Z")
PREFIX = "/api/v1/federation/"
MAX_EXPORT_BYTES = 512 * 1024
NOT_FOUND = (404, json.dumps({"error": "not found"}).encode(), {})        # byte-identical to api.py's _deny(404)


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class _Quota:
    """Per-peer sliding windows: requests in the last minute, bytes in the last hour. In memory; a restart resets it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._req: dict[str, deque] = {}
        self._bytes: dict[str, deque] = {}

    def admit_request(self, peer: str, limit: int, now: float) -> bool:
        with self._lock:
            q = self._req.setdefault(peer, deque())
            while q and now - q[0] >= 60.0:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def admit_bytes(self, peer: str, size: int, cap: int, now: float) -> bool:
        with self._lock:
            q = self._bytes.setdefault(peer, deque())
            while q and now - q[0][0] >= 3600.0:
                q.popleft()
            if sum(n for _, n in q) + size > cap:
                return False
            q.append((now, size))
            return True


class FederationGateway:
    def __init__(self, service, source, clock=time.time):
        self.service = service
        self.source = source              # callable -> raw YAML section (hot), shared with ingest
        self.clock = clock
        self.replay = sign.ReplayGuard()
        self.quota = _Quota()

    def handle(self, method: str, target: str, headers, body: bytes = b"") -> tuple[int, bytes, dict]:
        """``(status, body bytes, extra headers)``. Never raises."""
        try:
            return self._handle(method, target, headers, body)
        except Exception:      # noqa: BLE001 - nothing internal ever reaches a peer
            return NOT_FOUND

    def _handle(self, method, target, headers, body):
        now = self.clock()
        policy = policy_from_source(self.source)                  # the single snapshot for this request
        svc = self.service
        # (federation off, no server_id, a missing signing key and a degraded log are refused by verify_request,
        # load_seed and CollabService below; a missing service by the catch-all in handle())
        if method != "GET" or not target.startswith(PREFIX):
            return NOT_FOUND
        authed = sign.verify_request(policy, method=method, target=target, headers=headers, body=body, now=now,
                                     replay=self.replay)
        if authed is None:
            return NOT_FOUND
        peer, _fp = authed
        seed = sign.load_seed(policy.signing_key_file)
        if seed is None:
            return NOT_FOUND
        if not self.quota.admit_request(peer.peer_id, peer.requests_per_minute, now):
            return 429, b'{"error":"rate limit exceeded"}', {"Retry-After": "60"}
        split = urlsplit(target)
        parts = [p for p in split.path[len(PREFIX):].split("/")]
        doc = None
        if parts == ["shares"] and not split.query:
            doc = svc.federation_shares(peer.peer_id, policy)
            kind = "shares"
        elif len(parts) == 3 and parts[0] == "shares" and parts[2] == "events":
            args = self._events_args(split.query, policy)
            if args is None:
                return 400, b'{"error":"cursor and limit must be integers in range"}', {}
            doc = svc.federation_events(peer.peer_id, parts[1], args[0], args[1], MAX_EXPORT_BYTES, policy)
            kind = "events"
        if doc is None:
            return NOT_FOUND
        doc.update(scheme=sign.SCHEME, kind=kind, exporter=policy.server_id, audience=peer.peer_id,
                   issued_at=int(now), expires_at=int(now) + policy.export_validity_seconds)
        out = canonical(doc)
        if not self.quota.admit_bytes(peer.peer_id, len(out), peer.max_bytes_per_hour, now):
            return 429, b'{"error":"byte quota exceeded"}', {"Retry-After": "300"}
        return 200, out, {sign.H_EXPORT_SIG: sign.sign_export(seed, out),
                          sign.H_EXPORT_KEY: key_fingerprint_of(sign.public_key_hex(seed))}

    @staticmethod
    def _events_args(query: str, policy):
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query), max_num_fields=4)
        except ValueError:
            return None
        names = [k for k, _ in pairs]
        if len(set(names)) != len(names) or set(names) - {"cursor", "limit"}:
            return None
        q = dict(pairs)
        raw_cursor, raw_limit = q.get("cursor", "0"), q.get("limit", str(policy.max_export_events))
        if not (_DIGITS.fullmatch(raw_cursor) and _DIGITS.fullmatch(raw_limit)):
            return None                           # int() would also take " 5", "+5", "1_0" and non-ASCII digits
        cursor, limit = int(raw_cursor), int(raw_limit)
        if not 0 <= cursor < 2 ** 53 or not 1 <= limit <= policy.max_export_events:
            return None
        return cursor, limit
