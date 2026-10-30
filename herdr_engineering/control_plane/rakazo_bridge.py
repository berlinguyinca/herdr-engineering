"""Rakazo mobile bridge: the slice of the Rakazo server protocol the iPhone app needs, mapped onto the Herdr assistant.

The Rakazo app stores only ``scheme://host:port`` for a custom server and then calls ``POST /rpc/<procedure>`` and
``/api/auth/*`` at the origin root. This module answers those, and ONLY those, from the existing assistant service
(:mod:`.assistant`): a Rakazo "bot" is an assistant, its single thread is an assistant thread, a message is an assistant
message, a run is an assistant run and an approval card is an assistant approval decided through ``decide_approval``.
It owns no assistant data. It owns exactly two things: the sign-in accounts/tokens, and an in-memory journal that numbers
events for ``threads/subscribe`` (:mod:`.rakazo_events`).

Off unless the fleet YAML ``control_plane.rakazo_bridge.enabled`` is true; then every ``/rpc/*`` and ``/api/auth/*`` path
answers 404 exactly as before. Sign-in accepts only the configured accounts; the password is checked against a scrypt hash
from the YAML (never a plaintext), a bearer token is 32 random bytes of which only the SHA-256 is kept, and neither the
password, a token, a hash nor a request body is ever logged or written to the audit trail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from . import rakazo_bots as bots
from . import rakazo_events as ev
from . import rakazo_prefs as pf
from . import rakazo_rooms as rooms
from . import rakazo_spaces as sp
from .assistant import (
    ApprovalRequired,
    AssistantError,
    AssistantForbidden,
    AssistantNotFound,
    AssistantUnavailable,
    RateLimited,
    redact_secrets,
)

log = logging.getLogger(__name__)

SCRYPT_N, SCRYPT_R, SCRYPT_P, SALT_BYTES, HASH_BYTES = 2 ** 15, 8, 1, 16, 32
_MAX_N = 2 ** 17                     # 128 MiB: a hash in the YAML cannot make one sign-in allocate more than this
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\Z")
_COLLAB_USER = re.compile(r"[A-Za-z0-9._@+:-]{1,200}\Z")
_PROC = re.compile(r"[A-Za-z][A-Za-z0-9/]{0,63}\Z")
_ROLES = ("operator",)
_CONSENT_VERSION = "herdr-1"
MAX_BODY = 1_000_000
MAX_AUTH_BODY = 8_192
MAX_STREAMS = 8
MAX_SENDS = 4
MAX_NONCES = 512
MAX_BOTS = 20                        # assistants per account
MAX_RUN_ROWS = 20
MAX_ACTIVE_ROWS = 50


# --------------------------------------------------------------------------------------------- password hashing
@dataclass(frozen=True)
class ScryptParams:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes


def _derive(password: str, params: ScryptParams) -> bytes:
    return hashlib.scrypt(password.encode("utf-8", "surrogatepass"), salt=params.salt, n=params.n, r=params.r,
                          p=params.p, maxmem=256 * params.n * params.r + (1 << 20), dklen=len(params.digest))


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """``scrypt$N$r$p$salt_b64$hash_b64``: a fresh 16-byte salt and a 32-byte key. Only this string goes in the YAML."""
    if not password:
        raise ValueError("the password is empty")
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, ScryptParams(n, r, p, salt, b"\0" * HASH_BYTES))
    return "$".join(["scrypt", str(n), str(r), str(p), base64.b64encode(salt).decode(), base64.b64encode(key).decode()])


def parse_scrypt(text: Any, where: str) -> ScryptParams:
    """Parse ``scrypt$N$r$p$salt_b64$hash_b64``; anything else is an error naming ``where`` (never echoing the value)."""
    if not isinstance(text, str):
        raise ValueError(f"{where} must be a string of the form scrypt$N$r$p$salt_b64$hash_b64")
    parts = text.split("$")
    bad = ValueError(f"{where} is not a valid scrypt hash (expected scrypt$N$r$p$salt_b64$hash_b64; "
                     "create one with `herdr-eng bridge hash-password`)")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise bad
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except ValueError:
        raise bad from None
    if not (2 ** 14 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4
            and len(salt) >= 16 and 16 <= len(digest) <= 64):
        raise bad
    return ScryptParams(n, r, p, salt, digest)


# --------------------------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class Account:
    account_id: str          # stable: derived from the normalised email
    email: str
    principal_id: str        # the assistant user id for everything this account does
    role: str
    params: ScryptParams = field(repr=False)
    tokens_valid_after: float = 0.0     # epoch seconds: a token created before this is dead, whatever the database says
    collab_user: str | None = None      # the collaboration user this account acts as (YAML only); None = no rooms or agents


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _cutoff(value: Any, where: str, now: float) -> float:
    """``tokens_valid_after``: an ISO-8601 time with a zone (e.g. 2026-11-02T09:30:00Z). Not in the future."""
    if value is None:
        return 0.0
    bad = ValueError(f"{where} must be an ISO-8601 timestamp with a time zone, not in the future "
                     "(e.g. 2026-11-02T09:30:00Z)")
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise bad from None
    else:
        raise bad
    if stamp.tzinfo is None or stamp.timestamp() > now + 300:
        raise bad
    return stamp.timestamp()


def rakazo_bridge_config(raw: Any, now: float | None = None) -> dict[str, Any]:
    """Validate ``control_plane.rakazo_bridge``. Disabled (and unchecked beyond its type) unless ``enabled`` is true;
    when enabled, anything missing or malformed raises ValueError so the control plane refuses to start."""
    if raw is None:
        return {"enabled": False}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.rakazo_bridge must be a mapping")
    if raw.get("enabled") is not True:
        return {"enabled": False}
    now = time.time() if now is None else now
    unknown = sorted(set(raw) - {"enabled", "accounts", "token_ttl_days", "max_tokens", "trusted_proxies"})
    if unknown:
        raise ValueError(f"control_plane.rakazo_bridge has unknown keys: {', '.join(map(str, unknown))} "
                         "(accounts are listed under accounts: [{email, password_scrypt, role}])")

    def bounded(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"control_plane.rakazo_bridge.{key} must be an integer from {low} to {high}")
        return value

    ttl_days = bounded("token_ttl_days", 30, 1, 365)
    max_tokens = bounded("max_tokens", 5, 1, 50)
    proxies = raw.get("trusted_proxies", [])
    if not isinstance(proxies, list):
        raise ValueError("control_plane.rakazo_bridge.trusted_proxies must be a list of addresses or CIDR ranges")
    nets = []
    for i, item in enumerate(proxies):
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            raise ValueError(f"control_plane.rakazo_bridge.trusted_proxies[{i}] is not an address or CIDR range") from None
    rows = raw.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("control_plane.rakazo_bridge.accounts must list at least one {email, password_scrypt, role}")
    accounts: list[Account] = []
    for i, row in enumerate(rows):
        where = f"control_plane.rakazo_bridge.accounts[{i}]"
        if not isinstance(row, dict) or set(row) - {"email", "password_scrypt", "role", "tokens_valid_after", "collab_user"}:
            raise ValueError(f"{where} must be a mapping with only email, password_scrypt, role, tokens_valid_after and collab_user")
        email = row.get("email")
        if not isinstance(email, str) or not _EMAIL.match(normalize_email(email)) or len(email) > 254:
            raise ValueError(f"{where}.email must be an email address")
        role = row.get("role", "operator")
        if role != "operator":
            raise ValueError(f"{where}.role {role!r} is not supported: only 'operator' is implemented "
                             "('family' is reserved for a later multi-user mode)")
        params = parse_scrypt(row.get("password_scrypt"), f"{where}.password_scrypt")
        cutoff = _cutoff(row.get("tokens_valid_after"), f"{where}.tokens_valid_after", now)
        email_n = normalize_email(email)
        account_id = hashlib.sha256(b"rakazo-bridge-account:" + email_n.encode()).hexdigest()[:16]
        if any(a.account_id == account_id for a in accounts):
            raise ValueError(f"{where}.email repeats an earlier account")
        link = row.get("collab_user")
        if link is not None and (not isinstance(link, str) or not _COLLAB_USER.match(link)):
            raise ValueError(f"{where}.collab_user must be a collaboration user id (letters, digits and . _ @ + : -)")
        accounts.append(Account(account_id, email_n, f"rakazo:{account_id}", role, params, cutoff, link))
    return {"enabled": True, "token_ttl_days": ttl_days, "max_tokens": max_tokens, "accounts": accounts,
            "trusted_proxies": tuple(nets)}


# --------------------------------------------------------------------------------------------- tokens
class TokenStoreError(Exception):
    """The persistent token table could not be written. Sign-in fails closed rather than issue an unrecorded token."""


DB_TIMEOUT_SECONDS = 3
MAX_LIMITER_ENTRIES = 20_000


class PostgresTokenPersistence:
    """``rakazo_bridge_tokens`` (migration 0036): the SHA-256 of each live token, never the token itself.

    Sync psycopg2 like the assistant's store; a connection factory is injectable for tests. Credentials come from the DSN
    and are never logged. Every connection gets a connect timeout, and every transaction a statement/lock timeout set with
    SET LOCAL (safe behind PgBouncer), whatever the DSN says, so a stuck database costs a request a few seconds, never a
    hang."""

    def __init__(self, dsn: str, connect: Callable | None = None):
        self._dsn = dsn
        self._connect = connect

    def _run(self, sql: str, args: tuple = (), fetch: bool = False):
        timeouts = 0
        if self._connect is not None:
            conn = self._connect()
        else:
            import psycopg2
            conn = psycopg2.connect(self._dsn, connect_timeout=DB_TIMEOUT_SECONDS)
            timeouts = DB_TIMEOUT_SECONDS * 1000
        try:
            with conn.cursor() as cur:
                if timeouts:
                    # SET LOCAL, not a startup option: PgBouncer rejects unknown startup parameters, and a plain SET would
                    # outlive this transaction on a pooled server connection
                    cur.execute(f"SET LOCAL statement_timeout = {timeouts}; SET LOCAL lock_timeout = {timeouts}")
                cur.execute(sql, args)
                rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    @staticmethod
    def _ts(epoch: float) -> datetime:
        return datetime.fromtimestamp(epoch, UTC)

    def load(self) -> list[dict[str, Any]]:
        rows = self._run("SELECT token_sha256, account_id, created_at, expires_at, last_used_at, last_ip "
                         "FROM rakazo_bridge_tokens WHERE expires_at > now()", fetch=True)
        return [{"hash": r[0], "account_id": r[1], "created": r[2].timestamp(), "expires": r[3].timestamp(),
                 "last_used": r[4].timestamp(), "last_ip": r[5] or ""} for r in rows]

    def upsert(self, digest: str, rec: dict[str, Any]) -> None:
        self._run("INSERT INTO rakazo_bridge_tokens (token_sha256, account_id, created_at, expires_at, last_used_at, "
                  "last_ip) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (token_sha256) DO UPDATE SET "
                  "expires_at = EXCLUDED.expires_at, last_used_at = EXCLUDED.last_used_at, last_ip = EXCLUDED.last_ip",
                  (digest, rec["account_id"], self._ts(rec["created"]), self._ts(rec["expires"]),
                   self._ts(rec["last_used"]), rec["last_ip"]))

    def delete(self, digest: str) -> None:
        self._run("DELETE FROM rakazo_bridge_tokens WHERE token_sha256 = %s", (digest,))

    def touch(self, digest: str, when: float, ip: str) -> None:
        self._run("UPDATE rakazo_bridge_tokens SET last_used_at = %s, last_ip = %s WHERE token_sha256 = %s",
                  (self._ts(when), ip, digest))


class PostgresSpacePersistence(PostgresTokenPersistence):
    """``rakazo_spaces`` (migration 0037): the registry of extra spaces. Same connection rules as the token table."""

    def load(self) -> list[dict[str, Any]]:
        rows = self._run("SELECT space_id, account_id, name, created_at, overrides, deleted FROM rakazo_spaces", fetch=True)
        return [{"space_id": r[0], "account_id": r[1], "name": r[2], "created": r[3].timestamp(),
                 "overrides": list(r[4] or ()), "deleted": bool(r[5])} for r in rows]

    def upsert(self, row: dict[str, Any]) -> None:  # type: ignore[override]
        self._run("INSERT INTO rakazo_spaces (space_id, account_id, name, created_at, overrides, deleted) "
                  "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (space_id) DO UPDATE SET name = EXCLUDED.name, "
                  "overrides = EXCLUDED.overrides, deleted = EXCLUDED.deleted",
                  (row["space_id"], row["account_id"], row["name"], self._ts(row["created"]), row["overrides"],
                   row["deleted"]))


class PostgresPrefsPersistence(PostgresTokenPersistence):
    """``rakazo_prefs`` (migration 0038): small per-account, per-space settings."""

    def load(self) -> list[dict[str, Any]]:
        rows = self._run("SELECT account_id, space_id, kind, key, value FROM rakazo_prefs", fetch=True)
        return [{"account_id": r[0], "space_id": r[1], "kind": r[2], "key": r[3],
                 "value": r[4] if not isinstance(r[4], str) else json.loads(r[4])} for r in rows]

    def upsert(self, row: dict[str, Any]) -> None:  # type: ignore[override]
        self._run("INSERT INTO rakazo_prefs (account_id, space_id, kind, key, value, updated_at) "
                  "VALUES (%s, %s, %s, %s, %s::jsonb, now()) ON CONFLICT (account_id, space_id, kind, key) "
                  "DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                  (row["account_id"], row["space_id"], row["kind"], row["key"], json.dumps(row["value"])))

    def delete(self, account_id: str, space_id: str, kind: str, key: str) -> None:  # type: ignore[override]
        self._run("DELETE FROM rakazo_prefs WHERE account_id = %s AND space_id = %s AND kind = %s AND key = %s",
                  (account_id, space_id, kind, key))


class TokenStore:
    """Bearer tokens: 32 random bytes handed out once; only ``sha256(token)`` is kept, with expiry, creation, last use and
    last address. At most ``max_tokens`` live per account (the oldest is evicted). Memory is authoritative; the optional
    persistence lets tokens survive a restart.

    Revocation never depends on the database. A token that is dropped (sign-out, eviction, expiry, cut-off) is dead in
    memory at once; its row is deleted afterwards, outside the lock, and a delete that fails is remembered (a tombstone)
    and retried with backoff on later operations. A tombstone lives only in memory, so if the process restarts before the
    delete succeeded the row is loaded again: ``tokens_valid_after`` (per account, in the YAML) is the cut-off that kills
    such tokens whatever the database holds. No database call happens while the lock is held."""

    TOUCH_EVERY = 300.0
    RETRY_MIN, RETRY_MAX = 5.0, 300.0

    def __init__(self, ttl_seconds: float, max_tokens: int, persistence=None, clock: Callable[[], float] = time.time,
                 cutoffs: dict[str, float] | None = None):
        self.ttl, self.max_tokens, self._p, self._clock = ttl_seconds, max_tokens, persistence, clock
        self._cutoffs = dict(cutoffs or {})
        self._tokens: dict[str, dict[str, Any]] = {}
        self._tomb: set[str] = set()          # rows whose durable delete is still owed
        self._retry_at, self._backoff = 0.0, 0.0
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._serial = 0                      # breaks ties between tokens minted in the same instant
        if persistence is not None:
            try:
                for rec in persistence.load():
                    digest = rec.pop("hash")
                    if rec["created"] < self._cutoffs.get(rec["account_id"], 0.0):
                        self._tomb.add(digest)            # revoked by the YAML cut-off: never live, delete the row
                    elif digest not in self._tomb:
                        self._tokens[digest] = {**rec, "persisted": rec["last_used"]}
            except Exception as exc:  # noqa: BLE001 - fail closed: nobody is signed in until they sign in again
                log.error("rakazo bridge: could not load persisted tokens (%s); all sessions are signed out",
                          type(exc).__name__)
        self._flush()

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @property
    def pending_deletes(self) -> int:
        with self._lock:
            return len(self._tomb)

    def _drop_locked(self, digest: str) -> None:
        """Dead in memory now; the row's delete is owed (settled by ``_flush`` after the lock is released)."""
        if self._tokens.pop(digest, None) is not None and self._p is not None:
            self._tomb.add(digest)

    def _flush(self, force: bool = False) -> bool:
        """Settle owed deletes; True when none remain. A failure keeps the tombstones and backs off (5 s, doubling to 5 min)
        before the next attempt, which any later token operation makes. Never called with the lock held."""
        if self._p is None:
            return True
        now = self._clock()
        with self._lock:
            if not self._tomb:
                return True
            if not force and now < self._retry_at:
                return False
            batch = list(self._tomb)
        if not self._flush_lock.acquire(blocking=False):
            return False
        try:
            for digest in batch:
                try:
                    self._p.delete(digest)
                except Exception as exc:  # noqa: BLE001 - the database is likely down: stop and back off
                    log.warning("rakazo bridge: could not delete a revoked token row (%s); will retry",
                                type(exc).__name__)
                    with self._lock:
                        self._backoff = min(self.RETRY_MAX, max(self.RETRY_MIN, self._backoff * 2))
                        self._retry_at = now + self._backoff
                    return False
                with self._lock:
                    self._tomb.discard(digest)
            with self._lock:
                self._backoff = 0.0
            return True
        finally:
            self._flush_lock.release()

    def mint(self, account_id: str, ip: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        digest = self.digest(token)
        rec = {"account_id": account_id, "created": now, "expires": now + self.ttl, "last_used": now,
               "last_ip": ip[:64], "persisted": now}
        with self._lock:
            self._serial += 1
            rec["serial"] = self._serial
            for d in [d for d, r in self._tokens.items() if r["expires"] <= now]:
                self._drop_locked(d)
            self._tokens[digest] = rec
        if self._p is not None:
            try:
                self._p.upsert(digest, rec)
            except Exception as exc:  # noqa: BLE001
                log.error("rakazo bridge: token table write failed (%s)", type(exc).__name__)
                with self._lock:
                    self._tokens.pop(digest, None)
                self._flush()
                raise TokenStoreError from None
        with self._lock:
            # only after the new one is recorded: a failed write must not cost the account a working session
            mine = sorted((r["created"], r.get("serial", 0), d) for d, r in self._tokens.items()
                          if r["account_id"] == account_id)
            for *_, d in mine[:max(0, len(mine) - self.max_tokens)]:
                self._drop_locked(d)
        self._flush()
        return token

    def lookup(self, token: str, ip: str = "") -> str | None:
        """The account a live token belongs to, or None. Records last use and address."""
        digest = self.digest(token)
        now = self._clock()
        touch = False
        with self._lock:
            rec = self._tokens.get(digest)
            if rec is None:
                account = None
            elif rec["expires"] <= now or rec["created"] < self._cutoffs.get(rec["account_id"], 0.0):
                self._drop_locked(digest)
                account = None
            else:
                account = rec["account_id"]
                rec["last_used"], rec["last_ip"] = now, ip[:64]
                if self._p is not None and now - rec["persisted"] >= self.TOUCH_EVERY:
                    rec["persisted"] = touch = now
        if touch:
            try:
                self._p.touch(digest, touch, ip[:64])
            except Exception as exc:  # noqa: BLE001 - bookkeeping only
                log.warning("rakazo bridge: could not record token use (%s)", type(exc).__name__)
        self._flush()
        return account

    def valid(self, token: str) -> bool:
        with self._lock:
            rec = self._tokens.get(self.digest(token))
            return (rec is not None and rec["expires"] > self._clock()
                    and rec["created"] >= self._cutoffs.get(rec["account_id"], 0.0))

    def revoke(self, token: str) -> tuple[bool, bool]:
        """(was it live, is its row gone). The token is dead in memory either way."""
        digest = self.digest(token)
        with self._lock:
            known = digest in self._tokens
            if known:
                self._drop_locked(digest)
        return known, self._flush(force=True)

    def count(self, account_id: str | None = None) -> int:
        with self._lock:
            return sum(1 for r in self._tokens.values() if account_id in (None, r["account_id"]))


# --------------------------------------------------------------------------------------------- sign-in limiter
class SignInLimiter:
    """Failed sign-ins lock a SOURCE ADDRESS out for exponentially longer: from the ``free``-th failure on the lock is
    ``base * 2**(failures beyond that)`` seconds, up to ``cap``. A success clears that address; quiet time forgets it.
    The lock is checked BEFORE any password work. There is deliberately no lock across all addresses: anyone on the
    tailnet could then keep the owner out. The all-address failure count is for logging only."""

    WINDOW = 600.0

    def __init__(self, clock: Callable[[], float] = time.monotonic, *, free_ip: int = 5, base_ip: float = 30.0,
                 cap: float = 3600.0, forget_after: float = 3600.0):
        self._clock, self._cap, self._forget, self._free, self._base = clock, cap, forget_after, free_ip, base_ip
        self._state: dict[str, dict[str, float]] = {}
        self._recent: list[float] = []
        self._lock = threading.Lock()

    def _entry(self, key: str, now: float) -> dict[str, float]:
        entry = self._state.get(key)
        if entry is not None and now - entry["last"] > self._forget:
            entry = None
        if entry is None:
            entry = self._state[key] = {"fails": 0, "until": 0.0, "last": now}
            if len(self._state) > MAX_LIMITER_ENTRIES:
                # forget quiet addresses first; a locked-out address is only dropped if nothing else can be, and then the
                # one whose lock ends soonest, so flooding with new addresses cannot reset an active lockout
                order = sorted((k for k in self._state if k != key),
                               key=lambda k: (self._state[k]["until"] > now, self._state[k]["until"],
                                              self._state[k]["last"]))
                for stale in order[:MAX_LIMITER_ENTRIES // 4]:
                    del self._state[stale]
        return entry

    def retry_after(self, ip: str) -> int:
        now = self._clock()
        with self._lock:                  # a read: probing with new addresses must not allocate state
            entry = self._state.get(ip)
            wait = entry["until"] - now if entry is not None and now - entry["last"] <= self._forget else 0.0
        return int(wait) + 1 if wait > 0 else 0

    def failure(self, ip: str) -> int:
        """Record a failure; returns how many failures (from any address) the last ten minutes saw, for logging."""
        now = self._clock()
        with self._lock:
            entry = self._entry(ip, now)
            entry["fails"] += 1
            entry["last"] = now
            if entry["fails"] >= self._free:
                entry["until"] = now + min(self._cap, self._base * 2 ** (entry["fails"] - self._free))
            self._recent = [t for t in self._recent if now - t < self.WINDOW] + [now]
            return len(self._recent)

    def success(self, ip: str) -> None:
        with self._lock:
            self._state.pop(ip, None)


# --------------------------------------------------------------------------------------------- responses and errors
class BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


@dataclass
class BridgeResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    frames: Any = None                       # an iterator of bytes for a server-sent-event stream
    on_close: Callable[[], None] | None = None

    def close(self) -> None:
        if self.frames is not None and hasattr(self.frames, "close"):
            self.frames.close()
        if self.on_close is not None:
            self.on_close()
            self.on_close = None


def _json_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> BridgeResponse:
    return BridgeResponse(status, json.dumps(payload, separators=(",", ":")).encode(),
                          {"Content-Type": "application/json", "Cache-Control": "no-store", **(headers or {})})


def _rpc_error(err: BridgeError) -> BridgeResponse:
    """oRPC's failure shape, which the app reads: ``{"json": {code, status, message, data}}``."""
    return _json_response(err.status, {"json": {"defined": False, "code": err.code, "status": err.status,
                                                "message": err.message, "data": {}}}, err.headers)


def _auth_error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> BridgeResponse:
    return _json_response(status, {"message": message, "code": code}, headers)


@dataclass
class _Ctx:
    account: Account
    tenant: str            # the tenant of the ACTIVE space: the account's own for the default space
    user: str
    token: str
    accepts_stream: bool
    space: str = sp.DEFAULT_SPACE_ID
    base_tenant: str = ""


# --------------------------------------------------------------------------------------------- the bridge
class RakazoBridge:
    """Dispatches ``/rpc/*`` and ``/api/auth/*``. ``handle`` returns a :class:`BridgeResponse`, or None for a path that is
    not the bridge's (the caller then answers exactly as it did before the bridge existed)."""

    def __init__(self, config: dict[str, Any], assistant, *, version: str = "0", persistence=None, space_persistence=None,
                 prefs_persistence=None, collab=None, clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic):
        if not config.get("enabled"):
            raise ValueError("RakazoBridge needs an enabled configuration")
        self.svc = assistant
        self.version = version
        self.accounts: list[Account] = list(config["accounts"])
        self._by_principal = {a.principal_id: a for a in self.accounts}
        self._by_id = {a.account_id: a for a in self.accounts}
        self.tokens = TokenStore(config["token_ttl_days"] * 86400, config["max_tokens"], persistence, clock,
                                 cutoffs={a.account_id: a.tokens_valid_after for a in self.accounts})
        self.trusted_proxies = tuple(config.get("trusted_proxies", ()))
        self.limiter = SignInLimiter(monotonic)
        self.journals = ev.JournalRegistry(clock)
        self.spaces = sp.SpaceRegistry(space_persistence, clock)
        self.prefs = pf.PrefsStore(prefs_persistence)
        self.collab = collab
        self._kdf_slots = threading.BoundedSemaphore(2)
        self._streams = threading.BoundedSemaphore(MAX_STREAMS)
        self._sends = threading.BoundedSemaphore(MAX_SENDS)
        self._thread_lock = threading.Lock()
        self._nonce_lock = threading.Lock()
        self._nonces: OrderedDict[tuple, dict[str, Any]] = OrderedDict()      # recent sends, by clientNonce
        self._create_lock = threading.Lock()
        self._closed = threading.Event()
        self._monotonic = monotonic
        self.thread_factory = threading.Thread      # tests swap it to make a thread start fail
        self.send_wait = 5.0                  # seconds threads/send waits for the run to exist; tests shorten it
        self.stream_options: dict[str, Any] = {}      # tests shorten the poll/heartbeat/lifetime
        self._procs: dict[str, Callable[[_Ctx, dict[str, Any]], Any]] = {
            "me": self._me, "spaces/list": self._spaces_list, "bots/list": self._bots_list, "bots/get": self._bots_get,
            "bots/create": self._bots_create, "threads/head": self._threads_head, "threads/get": self._threads_get,
            "threads/messages": self._threads_messages, "threads/send": self._threads_send,
            "threads/answer": self._threads_answer, "threads/stop": self._threads_stop,
            "threads/markRead": self._threads_mark_read, "threads/subscribe": self._threads_subscribe,
            "runs/list": self._runs_list, "aiConsent/status": self._consent, "aiConsent/allow": self._consent,
            "aiConsent/revoke": self._consent, "spaces/create": self._spaces_create, "spaces/rename": self._spaces_rename,
            "spaces/remove": self._spaces_remove, "spaceSettings/overrides": self._space_overrides,
            "spaceSettings/setOverride": self._space_set_override, "notifications/registerPush": self._register_push,
            "notifications/unregisterPush": self._ok, "analytics/screen": self._ok,
        }
        self.rooms = rooms.Rooms(self, BridgeError)
        self._procs.update(self.rooms.procs())
        self.max_bots = MAX_BOTS
        self.bots = bots.Bots(self, BridgeError)
        for name, fn in self.bots.procs().items():
            self._procs[name] = fn
        for name, group_fn in (("threads/head", self.rooms.head), ("threads/get", self.rooms.thread),
                               ("threads/messages", self.rooms.messages), ("threads/send", self.rooms.send),
                               ("threads/stop", lambda c, i, **kw: {"ok": True}),
                               ("threads/markRead", self.rooms.mark_read)):
            self._procs[name] = self._grouped(self._procs[name], group_fn)
        # a dummy for requests whose email matches no account, so a wrong email costs the same KDF as a wrong password
        self._dummy = self.accounts[0].params

    # routing ---------------------------------------------------------------------------------------------------
    def body_limit(self, path: str, headers: dict[str, str]) -> int:
        """How much body the server will read BEFORE it knows who is asking: only a request carrying a live bearer token to
        an ``/rpc/`` path may send the full ``MAX_BODY``; everything else (sign-in included) gets the small auth limit."""
        token = self._bearer(headers)
        if path.split("?", 1)[0].startswith("/rpc/") and token and self.tokens.valid(token):
            return MAX_BODY
        return MAX_AUTH_BODY

    @staticmethod
    def handles(path: str) -> bool:
        path = path.split("?", 1)[0]
        return path.startswith("/rpc/") or path.startswith("/api/auth/")

    def close(self) -> None:
        self._closed.set()

    def client_address(self, peer: str, forwarded_for: str = "") -> str:
        """The address a request is attributed to (lockout key, token bookkeeping). The last X-Forwarded-For entry is
        believed only when the TCP peer is loopback or inside ``trusted_proxies``; anything else, or an entry that is not
        an IP address, leaves the peer itself, so a caller cannot choose its own key."""
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return peer[:64]
        if addr.is_loopback or any(addr in net for net in self.trusted_proxies):
            try:
                return str(ipaddress.ip_address(forwarded_for.split(",")[-1].strip()))
            except ValueError:
                pass
        return str(addr)

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes, peer: str) -> BridgeResponse | None:
        """``headers`` has lower-case names; ``peer`` is the TCP peer address. Never raises."""
        path = path.split("?", 1)[0]
        if not self.handles(path):
            return None
        ip = self.client_address(peer, headers.get("x-forwarded-for", ""))
        try:
            if path.startswith("/api/auth/"):
                return self._auth(method, path[len("/api/auth/"):], headers, body, ip)
            return self._rpc(method, path[len("/rpc/"):], headers, body, ip)
        except BridgeError as err:
            return _rpc_error(err) if path.startswith("/rpc/") else _auth_error(err.status, err.code, err.message,
                                                                                 err.headers)
        except Exception as exc:  # noqa: BLE001 - never echo internals, never log the request
            log.error("rakazo bridge: %s failed (%s)", path.split("/", 3)[-1][:40], type(exc).__name__)
            fail = BridgeError(500, "INTERNAL_SERVER_ERROR", "The server could not complete that request.")
            return _rpc_error(fail) if path.startswith("/rpc/") else _auth_error(500, fail.code, fail.message)

    # auth ------------------------------------------------------------------------------------------------------
    def _auth(self, method: str, name: str, headers, body: bytes, ip: str) -> BridgeResponse:
        if method == "GET" and name == "capabilities":
            # email + password only: no sign-up, one-time codes, passkeys, SSO or reset mail
            return _json_response(200, {"sso": None, "passwordAuth": True, "passwordReset": False, "resetUrl": None,
                                        "emailCode": False, "billing": False, "passkeyRpId": None,
                                        "nativePasskeys": {"ios": False, "android": False}, "crispWebsiteId": None})
        if method == "POST" and name == "sign-in/email":
            return self._sign_in(body, ip)
        if method == "POST" and name == "sign-out":
            token = self._bearer(headers)
            account = self._by_id.get(self.tokens.lookup(token, ip) or "") if token else None
            known, durable = self.tokens.revoke(token) if token else (False, True)
            if known and account is not None:
                self._audit(account, "rakazo.bridge.sign_out")
            if not durable:
                # dead in memory, but its database row is still owed a delete (retried; see tokens_valid_after)
                log.warning("rakazo bridge: a sign-out could not delete its token row yet")
                raise BridgeError(500, "INTERNAL_SERVER_ERROR",
                                  "Signed out, but the server could not remove the session record yet.")
            return _json_response(200, {"success": True})        # the same answer for any token: no oracle
        raise BridgeError(404, "NOT_FOUND", "Not found")

    @staticmethod
    def _bearer(headers) -> str:
        value = headers.get("authorization", "")
        return value[7:].strip() if value[:7].lower() == "bearer " else ""

    def _sign_in(self, body: bytes, ip: str) -> BridgeResponse:
        wait = self.limiter.retry_after(ip)
        if wait:
            raise BridgeError(429, "TOO_MANY_REQUESTS", "Too many attempts. Try again in a few minutes.",
                              {"Retry-After": str(wait)})
        email = password = ""
        try:
            data = json.loads(body) if len(body) <= MAX_AUTH_BODY else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):               # not a sign-in attempt at all: no KDF, no failure counted
            raise BridgeError(400, "BAD_REQUEST", "That request could not be read.")
        if isinstance(data.get("email"), str) and len(data["email"]) <= 254:
            email = normalize_email(data["email"])
        if isinstance(data.get("password"), str) and len(data["password"]) <= 1024:
            password = data["password"]
        # Compare against EVERY account (no early exit) and ALWAYS run one KDF, against the matched account's salt or
        # a configured account's when nothing matched, so neither timing nor the response tells a wrong email from a
        # wrong password.
        wanted = hashlib.sha256(email.encode()).digest()
        matched: Account | None = None
        for account in self.accounts:
            if hmac.compare_digest(hashlib.sha256(account.email.encode()).digest(), wanted) and matched is None:
                matched = account
        params = matched.params if matched is not None else self._dummy
        if not self._kdf_slots.acquire(timeout=15):
            raise BridgeError(429, "TOO_MANY_REQUESTS", "Too many attempts. Try again in a few minutes.",
                              {"Retry-After": "5"})
        try:
            derived = _derive(password, params)
        finally:
            self._kdf_slots.release()
        good = hmac.compare_digest(derived, params.digest) & (matched is not None)
        if not good or matched is None:
            recent = self.limiter.failure(ip)
            log.warning("rakazo bridge: failed sign-in from %s (%d from all addresses in 10 minutes)", ip[:64], recent)
            raise BridgeError(401, "INVALID_EMAIL_OR_PASSWORD", "Invalid credentials")
        self.limiter.success(ip)
        try:
            token = self.tokens.mint(matched.account_id, ip)
        except TokenStoreError:
            raise BridgeError(500, "INTERNAL_SERVER_ERROR", "Could not start a session. Try again later.") from None
        self._audit(matched, "rakazo.bridge.sign_in")
        return _json_response(200, {"token": token, "redirect": False,
                                    "user": {"id": matched.principal_id, "email": matched.email,
                                             "name": matched.email.split("@", 1)[0]}},
                              {"set-auth-token": token})

    def _audit(self, account: Account, action: str) -> None:
        """Best effort: signing in must not depend on the audit store, but a refusal is logged (never any secret)."""
        try:
            self.svc.store.audit_record(tenant_id=self.svc.tenant_for(account.principal_id), user_id=account.principal_id,
                                        actor=account.principal_id, action=action, subject_id=account.account_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("rakazo bridge: could not write the audit entry %s (%s)", action, type(exc).__name__)

    # rpc -------------------------------------------------------------------------------------------------------
    def _rpc(self, method: str, proc: str, headers, body: bytes, ip: str) -> BridgeResponse:
        if method != "POST" or not _PROC.match(proc):
            raise BridgeError(404, "NOT_FOUND", "Not found")
        if proc == "health":
            return _json_response(200, {"json": {"ok": True, "version": self.version}})
        token = self._bearer(headers)
        account_id = self.tokens.lookup(token, ip) if token else None
        account = self._by_id.get(account_id or "")
        if account is None:
            raise BridgeError(401, "UNAUTHORIZED", "Sign in to continue.")
        handler = self._procs.get(proc)
        if handler is None:
            raise BridgeError(404, "NOT_FOUND", "This server does not support that feature.")
        try:
            data = json.loads(body) if body.strip() else {}
        except (ValueError, RecursionError):
            raise BridgeError(400, "BAD_REQUEST", "The request body is not valid JSON.") from None
        inp = data.get("json", {}) if isinstance(data, dict) else {}
        if inp is None:
            inp = {}
        if not isinstance(inp, dict):
            raise BridgeError(400, "BAD_REQUEST", "The request input must be an object.")
        base = self.svc.tenant_for(account.principal_id)
        space_id = headers.get("x-rakazo-space-id", "").strip() or sp.DEFAULT_SPACE_ID
        if space_id != sp.DEFAULT_SPACE_ID and self.spaces.get(account.account_id, space_id) is None:
            # another account's space, a deleted one and a made-up one all look the same: not found
            raise BridgeError(404, "NOT_FOUND", "Not found")
        tenant = sp.space_tenant(base, space_id)
        ctx = _Ctx(account, tenant, account.principal_id, token,
                   "text/event-stream" in headers.get("accept", ""), space_id, base)
        try:
            self.svc.check_rate_limit(key=f"{tenant}:{ctx.user}")
            result = handler(ctx, inp)
        except AssistantNotFound:
            raise BridgeError(404, "NOT_FOUND", "Not found.") from None
        except KeyError as exc:
            # the assistant store signals a missing record with KeyError, so this is usually a plain not-found; but a bug
            # in a view builder looks the same, so leave a trace (where, never the key or the request)
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            log.warning("rakazo bridge: %s hit a KeyError at %s:%s", proc[:40], frame.filename.rsplit("/", 1)[-1],
                        frame.lineno)
            raise BridgeError(404, "NOT_FOUND", "Not found.") from None
        except RateLimited as exc:
            raise BridgeError(429, "TOO_MANY_REQUESTS", str(exc), {"Retry-After": "30"}) from None
        except AssistantUnavailable:
            raise BridgeError(400, "BAD_REQUEST", "The Herdr assistant is not enabled on this server.") from None
        except AssistantForbidden as exc:
            raise BridgeError(403, "FORBIDDEN", str(exc)) from None
        except ApprovalRequired as exc:
            raise BridgeError(409, "CONFLICT", str(exc)) from None
        except AssistantError as exc:
            raise BridgeError(400, "BAD_REQUEST", redact_secrets(str(exc)) or "The request was refused.") from None
        if isinstance(result, BridgeResponse):
            return result
        return _json_response(200, {"json": result})

    # helpers ---------------------------------------------------------------------------------------------------
    def _assistant(self, ctx: _Ctx, bot_id: Any):
        if not isinstance(bot_id, str) or not bot_id:
            raise BridgeError(400, "BAD_REQUEST", "Provide exactly one of botId or groupId.")
        return self.svc.get_assistant(bot_id, tenant_id=ctx.tenant, user_id=ctx.user)

    def _target(self, ctx: _Ctx, inp: dict[str, Any]):
        if inp.get("groupId"):
            raise BridgeError(400, "BAD_REQUEST", "Groups are not supported by this server.")
        return self._assistant(ctx, inp.get("botId"))

    def _thread(self, ctx: _Ctx, assistant):
        """The bot's one thread: the oldest of the assistant's threads (created on first use)."""
        with self._thread_lock:
            threads = self.svc.list_threads(tenant_id=ctx.tenant, user_id=ctx.user, assistant_id=assistant.assistant_id)
            if threads:
                return min(threads, key=lambda t: (t.created_at, t.thread_id))
            return self.svc.create_thread(tenant_id=ctx.tenant, user_id=ctx.user, assistant_id=assistant.assistant_id,
                                          title=assistant.name)

    def _gather(self, ctx: _Ctx, assistant, thread):
        store = self.svc.store
        with store._lock:
            messages = store.list_messages(thread.thread_id, tenant_id=ctx.tenant, user_id=ctx.user)
            runs = [r for r in store.list_runs(tenant_id=ctx.tenant, user_id=ctx.user,
                                               assistant_id=assistant.assistant_id)
                    if r.thread_id == thread.thread_id]
            run_ids = {r.run_id for r in runs}
            approvals = [(a, store.actions[a.action_id]) for a in list(store.approvals.values())
                         if a.tenant_id == ctx.tenant and a.user_id == ctx.user and a.action_id in store.actions
                         and store.actions[a.action_id].run_id in run_ids]
        return messages, runs, approvals

    def _journal(self, ctx: _Ctx, assistant, thread):
        messages, runs, approvals = self._gather(ctx, assistant, thread)
        with self.journals.lock:
            journal = self.journals.get(thread.thread_id, assistant.assistant_id, ctx.space)
            journal.sync(messages, runs, approvals)
            return journal, runs

    def _bot(self, ctx: _Ctx, assistant) -> dict[str, Any]:
        thread = self._thread(ctx, assistant)
        messages, runs, _ = self._gather(ctx, assistant, thread)
        visible = [m for m in messages if m.visibility == "visible"]
        active = any(ev.run_status(r.state) in ev.ACTIVE for r in runs)
        view = ev.bot_view(assistant, thread.thread_id, ev.snippet(visible[-1].content, 120) if visible else "",
                           "running" if active else "idle", ctx.space)
        return self.bots.apply(ctx, view)

    # procedures ------------------------------------------------------------------------------------------------
    def _ok(self, ctx, inp):
        return {"ok": True}

    def _me(self, ctx, inp):
        return ev.me_view(ctx.account, ctx.space)

    def _in_space(self, ctx: _Ctx, space_id: str) -> _Ctx:
        return replace(ctx, space=space_id, tenant=sp.space_tenant(ctx.base_tenant, space_id))

    def _space_entry(self, ctx: _Ctx, space_id: str, name: str, owned: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        bots = self._bots(self._in_space(ctx, space_id))
        return ev.space_view(space_id, name, bots, owned=owned), bots

    def _spaces_list(self, ctx, inp):
        entries = [self._space_entry(ctx, sp.DEFAULT_SPACE_ID, sp.DEFAULT_SPACE_NAME, False)]
        entries += [self._space_entry(ctx, s.space_id, s.name, True) for s in self.spaces.list(ctx.account.account_id)]
        active = next(e for e in entries if e[0]["id"] == ctx.space)
        return ev.spaces_view(active[0], [e[0] for e in entries], active[1])

    def _space_call(self, fn, *args):
        try:
            return fn(*args)
        except sp.SpaceError as exc:
            code = {400: "BAD_REQUEST", 404: "NOT_FOUND", 503: "SERVICE_UNAVAILABLE"}.get(exc.status, "BAD_REQUEST")
            raise BridgeError(exc.status, code, exc.message) from None

    def _spaces_create(self, ctx, inp):
        space = self._space_call(self.spaces.create, ctx.account.account_id, inp.get("name"))
        return self._space_entry(ctx, space.space_id, space.name, True)[0]

    def _spaces_rename(self, ctx, inp):
        space = self._space_call(self.spaces.rename, ctx.account.account_id, inp.get("spaceId"), inp.get("name"))
        return {"id": space.space_id, "name": space.name}

    def _spaces_remove(self, ctx, inp):
        """Archive every bot in the space, then tombstone it. Nothing is deleted: the records stay in their tenant."""
        space_id = inp.get("spaceId")
        if not isinstance(space_id, str) or self.spaces.get(ctx.account.account_id, space_id) is None:
            if space_id == sp.DEFAULT_SPACE_ID:
                raise BridgeError(400, "BAD_REQUEST", "The default space cannot be changed or deleted.")
            raise BridgeError(404, "NOT_FOUND", "Not found.")
        inner = self._in_space(ctx, space_id)
        for assistant in self.svc.list_assistants(tenant_id=inner.tenant, user_id=ctx.user):
            if assistant.enabled:
                self.svc.archive_assistant(assistant.assistant_id, tenant_id=inner.tenant, user_id=ctx.user)
        self._space_call(self.spaces.remove, ctx.account.account_id, space_id)
        return {"ok": True, "activeSpaceId": sp.DEFAULT_SPACE_ID}

    def _space_overrides(self, ctx, inp):
        space = self.spaces.get(ctx.account.account_id, ctx.space)
        return sorted(space.overrides) if space else []

    def _space_set_override(self, ctx, inp):
        space = self._space_call(self.spaces.set_override, ctx.account.account_id, ctx.space, inp.get("setting"),
                                 inp.get("enabled"))
        return sorted(space.overrides)

    def _bots(self, ctx: _Ctx) -> list[dict[str, Any]]:
        return ([self._bot(ctx, a) for a in self.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user)
                 if a.enabled]
                + self.rooms.agent_bots(ctx))

    def prefs_put(self, account: str, space: str, kind: str, key: str, value: Any) -> None:
        try:
            self.prefs.put(account, space, kind, key, value)
        except pf.PrefsError as exc:
            raise BridgeError(exc.status, "SERVICE_UNAVAILABLE" if exc.status == 503 else "BAD_REQUEST",
                              exc.message) from None

    def _group_target(self, ctx: _Ctx, inp: dict[str, Any]):
        """(inp aimed at a room, agent) when the request names a group or one of the account's agent bots, else None."""
        if inp.get("groupId"):
            return inp, None
        bot_id = inp.get("botId")
        if not isinstance(bot_id, str) or not bot_id:
            return None
        try:
            self.svc.get_assistant(bot_id, tenant_id=ctx.tenant, user_id=ctx.user)
            return None
        except KeyError:
            pass
        agent = self.rooms.find_agent(ctx, bot_id)
        if agent is None:
            return None
        if not agent.get("rooms"):
            raise BridgeError(400, "BAD_REQUEST", "This agent is not in a group yet. Add it to a group to talk to it.")
        return {**inp, "groupId": agent["rooms"][0]["room_id"]}, agent

    def _grouped(self, plain, group_fn):
        def handler(ctx, inp):
            target = self._group_target(ctx, inp)
            if target is None:
                return plain(ctx, inp)
            routed, agent = target
            return group_fn(ctx, routed, agent=agent) if group_fn == self.rooms.send else group_fn(ctx, routed)
        return handler

    def _bots_list(self, ctx, inp):
        return self._bots(ctx)

    def _bots_get(self, ctx, inp):
        agent = self.rooms.find_agent(ctx, inp.get("botId"))
        if agent is not None and ctx.space == sp.DEFAULT_SPACE_ID:
            return self.rooms.agent_bot(ctx, agent)
        return self._bot(ctx, self._assistant(ctx, inp.get("botId")))

    def _bots_create(self, ctx, inp):
        name = inp.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError(400, "BAD_REQUEST", "A bot needs a name of up to 80 characters.")
        text = inp.get("instructions") or inp.get("description") or ""
        if not isinstance(text, str) or len(text) > 20_000:
            raise BridgeError(400, "BAD_REQUEST", "The instructions are too long.")
        with self._create_lock:
            if len(self.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user)) >= MAX_BOTS:
                raise BridgeError(400, "BAD_REQUEST", f"This server allows at most {MAX_BOTS} bots per account.")
            assistant = self.svc.create_assistant(tenant_id=ctx.tenant, user_id=ctx.user, name=name.strip(),
                                                  instructions=text, enabled=True)
        return self._bot(ctx, assistant)

    def _threads_head(self, ctx, inp):
        assistant = self._target(ctx, inp)
        thread = self._thread(ctx, assistant)
        journal, _ = self._journal(ctx, assistant, thread)
        return {"threadId": thread.thread_id, "cursor": journal.head}

    def _threads_get(self, ctx, inp):
        assistant = self._target(ctx, inp)
        thread = self._thread(ctx, assistant)
        journal, runs = self._journal(ctx, assistant, thread)
        views = [ev.run_view(r, assistant.assistant_id) for r in sorted(runs, key=lambda r: r.created_at)]
        active = [v for v in views if v["status"] in ev.ACTIVE]
        latest = views[-1] if views else None
        run = active[-1] if active else (latest if latest and latest["status"] == "failed" else None)
        messages, older = journal.page()
        return {"threadId": thread.thread_id, "cursor": journal.head, "messages": messages, "olderCursor": older,
                "botId": assistant.assistant_id, "run": run, "activeRuns": active}

    def _threads_messages(self, ctx, inp):
        assistant = self._target(ctx, inp)
        thread = self._thread(ctx, assistant)
        journal, _ = self._journal(ctx, assistant, thread)
        before = inp.get("before")
        if before is not None and (isinstance(before, bool) or not isinstance(before, int)):
            raise BridgeError(400, "BAD_REQUEST", "before must be an integer.")
        messages, older = journal.page(before)
        return {"threadId": thread.thread_id, "messages": messages, "olderCursor": older,
                "coveredThroughSeq": max(0, journal.head)}

    def _threads_send(self, ctx, inp):
        assistant = self._target(ctx, inp)
        text = inp.get("text")
        if inp.get("artifactIds"):
            raise BridgeError(400, "BAD_REQUEST", "Attachments are not supported by this server.")
        if not isinstance(text, str) or not text.strip():
            raise BridgeError(400, "BAD_REQUEST", "Provide text.")
        if len(text) > 50_000:
            raise BridgeError(400, "BAD_REQUEST", "The message is too long.")
        if not assistant.enabled:
            raise BridgeError(400, "BAD_REQUEST", "This bot is disabled.")
        thread = self._thread(ctx, assistant)
        journal, runs = self._journal(ctx, assistant, thread)
        nonce = inp.get("clientNonce")
        key = (ctx.tenant, ctx.user, thread.thread_id, nonce) if isinstance(nonce, str) and 0 < len(nonce) <= 200 else None
        entry: dict[str, Any] = {"known_runs": {r.run_id for r in runs}, "known_messages": set(journal.messages),
                                 "result": None, "worker": None}
        if not self._sends.acquire(blocking=False):
            raise BridgeError(429, "TOO_MANY_REQUESTS", "Too many messages are being answered. Try again shortly.",
                              {"Retry-After": "5"})
        if key is not None:
            # the app retries a send it never saw an answer to: the same clientNonce must not send the message twice
            with self._nonce_lock:
                prior = self._nonces.get(key)
                if prior is None:
                    self._nonces[key] = entry
                    while len(self._nonces) > MAX_NONCES:
                        self._nonces.popitem(last=False)
            if prior is not None:
                self._sends.release()
                return self._await_run(ctx, assistant, thread, prior, key)
        outcome: dict[str, BaseException] = {}

        def work() -> None:
            # The assistant answers synchronously and a model can take longer than the app's 8 s request timeout, so the
            # run goes on in this thread while the reply to the app returns as soon as the run exists.
            try:
                self.svc.send_message(tenant_id=ctx.tenant, user_id=ctx.user, assistant_id=assistant.assistant_id,
                                      thread_id=thread.thread_id, content=text)
            except BaseException as exc:  # noqa: BLE001 - the run records its own failure; report it to a waiting caller
                outcome["error"] = exc
                log.warning("rakazo bridge: a run ended with %s", type(exc).__name__)
            finally:
                self._sends.release()

        worker = self.thread_factory(target=work, name="rakazo-send", daemon=True)
        entry["worker"], entry["outcome"] = worker, outcome
        try:
            worker.start()
        except BaseException:                  # a thread cap: the slot and the nonce must not stay taken
            self._sends.release()
            self._forget_nonce(key)
            raise BridgeError(503, "SERVICE_UNAVAILABLE", "The server is too busy to start that. Try again shortly.",
                              {"Retry-After": "5"}) from None
        return self._await_run(ctx, assistant, thread, entry, key)

    def _forget_nonce(self, key) -> None:
        if key is not None:
            with self._nonce_lock:
                self._nonces.pop(key, None)

    def _await_run(self, ctx, assistant, thread, entry: dict[str, Any], key) -> dict[str, Any]:
        """The ``threads/send`` answer once the run exists. Gives up after 5 s with a retryable 503 (the same clientNonce
        then returns this same run instead of sending again), or with the run's own refusal if it never started."""
        if entry["result"] is not None:
            return entry["result"]
        deadline = self._monotonic() + self.send_wait
        run = None
        while run is None:
            _, current = self._journal(ctx, assistant, thread)
            run = next((r for r in current if r.run_id not in entry["known_runs"]), None)
            worker = entry["worker"]
            if run is not None or (worker is not None and not worker.is_alive()) or self._monotonic() > deadline:
                break
            time.sleep(0.01)
        if run is None:
            worker = entry["worker"]
            if worker is None or worker.is_alive():
                raise BridgeError(503, "SERVICE_UNAVAILABLE", "The message is still being started. Retry to see it.",
                                  {"Retry-After": "2"})
            self._forget_nonce(key)                 # it ended without a run: a retry may start afresh
            error = entry.get("outcome", {}).get("error")
            if isinstance(error, AssistantError):
                raise error
            raise BridgeError(500, "INTERNAL_SERVER_ERROR", "The message could not be started.")
        journal, _ = self._journal(ctx, assistant, thread)
        sent = journal.user_message_after(entry["known_messages"])
        entry["result"] = {"taskId": run.task_id, "runId": run.run_id, "seq": sent["seq"] if sent else journal.head}
        return entry["result"]

    def _threads_answer(self, ctx, inp):
        assistant = self._target(ctx, inp)
        message_id, run_id, answer = inp.get("messageId"), inp.get("runId"), inp.get("answer")
        if inp.get("dismiss") or inp.get("fields") or inp.get("username"):
            raise BridgeError(400, "BAD_REQUEST", "Only approval answers are supported by this server.")
        if not (isinstance(message_id, str) and message_id.startswith(ev.APPROVAL_PREFIX)):
            raise BridgeError(400, "BAD_REQUEST", "Only approval answers are supported by this server.")
        decision = {"allow": "approved", "deny": "denied"}.get(answer)
        if decision is None:
            raise BridgeError(400, "BAD_REQUEST", "Answer allow or deny.")
        approval_id = message_id[len(ev.APPROVAL_PREFIX):]
        store = self.svc.store
        approval = store.approvals.get(approval_id)
        action = store.actions.get(approval.action_id) if approval is not None else None
        thread = self._thread(ctx, assistant)
        run = store.runs.get(run_id) if isinstance(run_id, str) else None
        if (approval is None or action is None or approval.tenant_id != ctx.tenant or approval.user_id != ctx.user
                or action.run_id != run_id or run is None or run.thread_id != thread.thread_id):
            raise AssistantNotFound("approval not found")
        # the EXISTING rules decide: owner/approver, expiry, and approver != requester for a sensitive tool unless the
        # operator's YAML (allow_self_approval, or single_operator) says otherwise
        decided = self.svc.decide_approval(approval_id, tenant_id=ctx.tenant, user_id=ctx.user, decision=decision,
                                           actor=ctx.user)
        if decided.decision == "approved" and self.svc.config["actions"]["auto_execute"]:
            try:
                self.svc.execute_approved_action(decided.action_id, tenant_id=ctx.tenant, user_id=ctx.user,
                                                 actor=ctx.user)
            except AssistantError as exc:        # the approval stands; the card shows the action did not run
                log.info("rakazo bridge: approved action not executed: %s", redact_secrets(str(exc)))
        return {"ok": True}

    def _threads_stop(self, ctx, inp):
        assistant = self._target(ctx, inp)
        thread = self._thread(ctx, assistant)
        _, runs = self._journal(ctx, assistant, thread)
        for run in runs:
            if ev.run_status(run.state) in ev.ACTIVE:
                try:
                    self.svc.cancel_run(run.run_id, tenant_id=ctx.tenant, user_id=ctx.user)
                except AssistantError:
                    pass
        return {"ok": True}

    def _threads_mark_read(self, ctx, inp):
        assistant = self._target(ctx, inp)
        if self.bots.meta(ctx, assistant.assistant_id).get("unread"):
            self.bots._put_meta(ctx, assistant.assistant_id, {"unread": False})
        return {"ok": True, "unreadAfterSeq": None}

    def _runs_list(self, ctx, inp):
        mode = inp.get("filter")
        if mode not in ("active", "recent"):
            raise BridgeError(400, "BAD_REQUEST", "filter must be active or recent.")
        store = self.svc.store
        limit = MAX_RUN_ROWS if mode == "recent" else MAX_ACTIVE_ROWS
        with store._lock:                  # the shared dicts change under concurrent runs: read them only while held
            runs = [r for r in store.list_runs(tenant_id=ctx.tenant, user_id=ctx.user)
                    if mode == "recent" or ev.run_status(r.state) in ev.ACTIVE][:limit]
            threads = {r.thread_id for r in runs}
            prompts: dict[str, list] = {}
            for m in store.messages.values():            # one pass, only for the threads shown
                if m.role == "user" and m.thread_id in threads:
                    prompts.setdefault(m.thread_id, []).append(m)
            names = {r.assistant_id: a.name for r in runs if (a := store.assistants.get(r.assistant_id))}
        rows = []
        for run in runs:
            before = [m for m in prompts.get(run.thread_id, []) if m.created_at <= run.created_at]
            prompt = max(before, key=lambda m: m.created_at).content if before else ""
            rows.append({"runId": run.run_id, "botId": run.assistant_id, "botName": names.get(run.assistant_id, ""),
                         "groupId": None, "groupName": None, "threadId": run.thread_id,
                         "status": ev.run_status(run.state), "trigger": "user", "notificationsEnabled": False,
                         "promptSnippet": ev.snippet(prompt, 120), "agentMessage": None,
                         "updatedAt": ev.iso(run.updated_at)})
        return {"runs": rows}

    def _consent(self, ctx, inp):
        # No third-party AI recipients are named, so the app never asks for consent; this server's model is the operator's.
        return {"scope": ctx.space, "version": _CONSENT_VERSION, "recipients": []}

    def _register_push(self, ctx, inp):
        token = inp.get("token")
        if not isinstance(token, str) or not 8 <= len(token) <= 512 or re.search(r"\s", token):
            raise BridgeError(400, "BAD_REQUEST", "That push token is not valid.")
        return {"ok": True}           # accepted and dropped: this server sends no push notifications

    def _threads_subscribe(self, ctx, inp):
        cursor = inp.get("cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < -1:
            raise BridgeError(400, "BAD_REQUEST", "cursor must be an integer of at least -1.")
        if not ctx.accepts_stream:
            raise BridgeError(400, "BAD_REQUEST", "Subscribe with accept: text/event-stream.")
        target = self._group_target(ctx, inp)
        if target is not None:
            fetch, gone = self.rooms.subscribe(ctx, target[0], cursor)
        else:
            assistant = self._target(ctx, inp)
            thread = self._thread(ctx, assistant)

            def fetch(after: int) -> list[dict[str, Any]]:
                journal, _ = self._journal(ctx, assistant, thread)
                with self.journals.lock:
                    return journal.after(after)

            def gone() -> bool:
                return False
        if not self._streams.acquire(blocking=False):
            raise BridgeError(429, "TOO_MANY_REQUESTS", "Too many live connections.", {"Retry-After": "10"})

        frames = ev.event_stream(fetch, cursor, should_stop=lambda: self._closed.is_set() or not self.tokens.valid(
            ctx.token) or gone(), **self.stream_options)
        return BridgeResponse(200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                            "X-Accel-Buffering": "no", "Connection": "close"},
                              frames=frames, on_close=self._streams.release)
