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
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import rakazo_events as ev
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
_PROC = re.compile(r"[A-Za-z][A-Za-z0-9/]{0,63}\Z")
_ROLES = ("operator",)
_CONSENT_VERSION = "herdr-1"
MAX_BODY = 1_000_000
MAX_AUTH_BODY = 8_192
MAX_STREAMS = 8
MAX_SENDS = 4


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


def normalize_email(email: str) -> str:
    return email.strip().lower()


def rakazo_bridge_config(raw: Any) -> dict[str, Any]:
    """Validate ``control_plane.rakazo_bridge``. Disabled (and unchecked beyond its type) unless ``enabled`` is true;
    when enabled, anything missing or malformed raises ValueError so the control plane refuses to start."""
    if raw is None:
        return {"enabled": False}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.rakazo_bridge must be a mapping")
    if raw.get("enabled") is not True:
        return {"enabled": False}
    unknown = sorted(set(raw) - {"enabled", "accounts", "token_ttl_days", "max_tokens"})
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
    rows = raw.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("control_plane.rakazo_bridge.accounts must list at least one {email, password_scrypt, role}")
    accounts: list[Account] = []
    for i, row in enumerate(rows):
        where = f"control_plane.rakazo_bridge.accounts[{i}]"
        if not isinstance(row, dict) or set(row) - {"email", "password_scrypt", "role"}:
            raise ValueError(f"{where} must be a mapping with only email, password_scrypt and role")
        email = row.get("email")
        if not isinstance(email, str) or not _EMAIL.match(normalize_email(email)) or len(email) > 254:
            raise ValueError(f"{where}.email must be an email address")
        role = row.get("role", "operator")
        if role != "operator":
            raise ValueError(f"{where}.role {role!r} is not supported: only 'operator' is implemented "
                             "('family' is reserved for a later multi-user mode)")
        params = parse_scrypt(row.get("password_scrypt"), f"{where}.password_scrypt")
        email_n = normalize_email(email)
        account_id = hashlib.sha256(b"rakazo-bridge-account:" + email_n.encode()).hexdigest()[:16]
        if any(a.account_id == account_id for a in accounts):
            raise ValueError(f"{where}.email repeats an earlier account")
        accounts.append(Account(account_id, email_n, f"rakazo:{account_id}", role, params))
    return {"enabled": True, "token_ttl_days": ttl_days, "max_tokens": max_tokens, "accounts": accounts}


# --------------------------------------------------------------------------------------------- tokens
class TokenStoreError(Exception):
    """The persistent token table could not be written. Sign-in fails closed rather than issue an unrecorded token."""


class PostgresTokenPersistence:
    """``rakazo_bridge_tokens`` (migration 0036): the SHA-256 of each live token, never the token itself.

    Sync psycopg2 like the assistant's store; a connection factory is injectable for tests. Credentials come from the DSN
    and are never logged."""

    def __init__(self, dsn: str, connect: Callable | None = None):
        self._dsn = dsn
        self._connect = connect

    def _run(self, sql: str, args: tuple = (), fetch: bool = False):
        if self._connect is not None:
            conn = self._connect()
        else:
            import psycopg2
            conn = psycopg2.connect(self._dsn)
        try:
            with conn.cursor() as cur:
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


class TokenStore:
    """Bearer tokens: 32 random bytes handed out once; only ``sha256(token)`` is kept, with expiry, creation, last use and
    last address. At most ``max_tokens`` live per account (the oldest is evicted). Memory is authoritative; the optional
    persistence lets tokens survive a restart."""

    TOUCH_EVERY = 300.0

    def __init__(self, ttl_seconds: float, max_tokens: int, persistence=None, clock: Callable[[], float] = time.time):
        self.ttl, self.max_tokens, self._p, self._clock = ttl_seconds, max_tokens, persistence, clock
        self._tokens: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._serial = 0                      # breaks ties between tokens minted in the same instant
        if persistence is not None:
            try:
                for rec in persistence.load():
                    self._tokens[rec.pop("hash")] = {**rec, "persisted": rec["last_used"]}
            except Exception as exc:  # noqa: BLE001 - fail closed: nobody is signed in until they sign in again
                log.error("rakazo bridge: could not load persisted tokens (%s); all sessions are signed out",
                          type(exc).__name__)

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _drop(self, digest: str) -> None:
        self._tokens.pop(digest, None)
        if self._p is not None:
            try:
                self._p.delete(digest)
            except Exception as exc:  # noqa: BLE001 - the in-memory copy is gone; a stale row expires by itself
                log.warning("rakazo bridge: could not delete a token row (%s)", type(exc).__name__)

    def mint(self, account_id: str, ip: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        rec = {"account_id": account_id, "created": now, "expires": now + self.ttl, "last_used": now,
               "last_ip": ip[:64], "persisted": now}
        with self._lock:
            self._serial += 1
            rec["serial"] = self._serial
            for d in [d for d, r in self._tokens.items() if r["expires"] <= now]:
                self._drop(d)
            digest = self.digest(token)
            if self._p is not None:
                try:
                    self._p.upsert(digest, rec)
                except Exception as exc:  # noqa: BLE001
                    log.error("rakazo bridge: token table write failed (%s)", type(exc).__name__)
                    raise TokenStoreError from None
            self._tokens[digest] = rec
            # only after the new one is recorded: a failed write must not cost the account a working session
            mine = sorted((r["created"], r.get("serial", 0), d) for d, r in self._tokens.items()
                          if r["account_id"] == account_id)
            for *_, d in mine[:max(0, len(mine) - self.max_tokens)]:
                self._drop(d)
        return token

    def lookup(self, token: str, ip: str = "") -> str | None:
        """The account a live token belongs to, or None. Records last use and address."""
        digest = self.digest(token)
        now = self._clock()
        with self._lock:
            rec = self._tokens.get(digest)
            if rec is None:
                return None
            if rec["expires"] <= now:
                self._drop(digest)
                return None
            rec["last_used"], rec["last_ip"] = now, ip[:64]
            if self._p is not None and now - rec["persisted"] >= self.TOUCH_EVERY:
                rec["persisted"] = now
                try:
                    self._p.touch(digest, now, rec["last_ip"])
                except Exception as exc:  # noqa: BLE001 - bookkeeping only
                    log.warning("rakazo bridge: could not record token use (%s)", type(exc).__name__)
            return rec["account_id"]

    def valid(self, token: str) -> bool:
        with self._lock:
            rec = self._tokens.get(self.digest(token))
            return rec is not None and rec["expires"] > self._clock()

    def revoke(self, token: str) -> bool:
        digest = self.digest(token)
        with self._lock:
            known = digest in self._tokens
            if known:
                self._drop(digest)
            return known

    def count(self, account_id: str | None = None) -> int:
        with self._lock:
            return sum(1 for r in self._tokens.values() if account_id in (None, r["account_id"]))


# --------------------------------------------------------------------------------------------- sign-in limiter
class SignInLimiter:
    """Failed sign-ins lock a source address, and the whole bridge, out for exponentially longer: from the ``free``-th failure
    on the lock is ``base * 2**(failures beyond that)`` seconds, up to ``cap``. A success clears that address; quiet time clears
    both. The lock is checked BEFORE any password work, so a locked-out caller costs nothing."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, *, free_ip: int = 5, free_global: int = 40,
                 base_ip: float = 30.0, base_global: float = 60.0, cap: float = 3600.0, forget_after: float = 3600.0):
        self._clock, self._cap, self._forget = clock, cap, forget_after
        self._rules = {"ip": (free_ip, base_ip), "*": (free_global, base_global)}
        self._state: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def _entry(self, key: str, now: float) -> dict[str, float]:
        entry = self._state.get(key)
        if entry is not None and now - entry["last"] > self._forget:
            entry = None
        if entry is None:
            entry = self._state[key] = {"fails": 0, "until": 0.0, "last": now}
            if len(self._state) > 2048:
                for stale in sorted(self._state, key=lambda k: self._state[k]["last"])[:512]:
                    if stale != "*":
                        del self._state[stale]
        return entry

    def retry_after(self, ip: str) -> int:
        now = self._clock()
        with self._lock:
            wait = max(self._entry(k, now)["until"] - now for k in ("*", "ip:" + ip))
        return int(wait) + 1 if wait > 0 else 0

    def failure(self, ip: str) -> None:
        now = self._clock()
        with self._lock:
            for key, rule in (("*", "*"), ("ip:" + ip, "ip")):
                entry = self._entry(key, now)
                free, base = self._rules[rule]
                entry["fails"] += 1
                entry["last"] = now
                if entry["fails"] >= free:
                    entry["until"] = now + min(self._cap, base * 2 ** (entry["fails"] - free))

    def success(self, ip: str) -> None:
        with self._lock:
            self._state.pop("ip:" + ip, None)


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
    tenant: str
    user: str
    token: str
    accepts_stream: bool


# --------------------------------------------------------------------------------------------- the bridge
class RakazoBridge:
    """Dispatches ``/rpc/*`` and ``/api/auth/*``. ``handle`` returns a :class:`BridgeResponse`, or None for a path that is
    not the bridge's (the caller then answers exactly as it did before the bridge existed)."""

    def __init__(self, config: dict[str, Any], assistant, *, version: str = "0", persistence=None,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic):
        if not config.get("enabled"):
            raise ValueError("RakazoBridge needs an enabled configuration")
        self.svc = assistant
        self.version = version
        self.accounts: list[Account] = list(config["accounts"])
        self._by_principal = {a.principal_id: a for a in self.accounts}
        self._by_id = {a.account_id: a for a in self.accounts}
        self.tokens = TokenStore(config["token_ttl_days"] * 86400, config["max_tokens"], persistence, clock)
        self.limiter = SignInLimiter(monotonic)
        self.journals = ev.JournalRegistry(clock)
        self._kdf_slots = threading.BoundedSemaphore(2)
        self._streams = threading.BoundedSemaphore(MAX_STREAMS)
        self._sends = threading.BoundedSemaphore(MAX_SENDS)
        self._thread_lock = threading.Lock()
        self._closed = threading.Event()
        self._monotonic = monotonic
        self.stream_options: dict[str, Any] = {}      # tests shorten the poll/heartbeat/lifetime
        self._procs: dict[str, Callable[[_Ctx, dict[str, Any]], Any]] = {
            "me": self._me, "spaces/list": self._spaces_list, "bots/list": self._bots_list, "bots/get": self._bots_get,
            "bots/create": self._bots_create, "threads/head": self._threads_head, "threads/get": self._threads_get,
            "threads/messages": self._threads_messages, "threads/send": self._threads_send,
            "threads/answer": self._threads_answer, "threads/stop": self._threads_stop,
            "threads/markRead": self._threads_mark_read, "threads/subscribe": self._threads_subscribe,
            "runs/list": self._runs_list, "aiConsent/status": self._consent, "aiConsent/allow": self._consent,
            "aiConsent/revoke": self._consent, "notifications/registerPush": self._register_push,
            "notifications/unregisterPush": self._ok, "analytics/screen": self._ok,
        }
        # a dummy for requests whose email matches no account, so a wrong email costs the same KDF as a wrong password
        self._dummy = self.accounts[0].params

    # routing ---------------------------------------------------------------------------------------------------
    @staticmethod
    def handles(path: str) -> bool:
        path = path.split("?", 1)[0]
        return path.startswith("/rpc/") or path.startswith("/api/auth/")

    def close(self) -> None:
        self._closed.set()

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes, ip: str) -> BridgeResponse | None:
        """``headers`` has lower-case names. Never raises."""
        path = path.split("?", 1)[0]
        if not self.handles(path):
            return None
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
            if token and self.tokens.revoke(token) and account is not None:
                self._audit(account, "rakazo.bridge.sign_out")
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
            data = json.loads(body[:MAX_AUTH_BODY]) if len(body) <= MAX_AUTH_BODY else {}
            if isinstance(data, dict):
                if isinstance(data.get("email"), str) and len(data["email"]) <= 254:
                    email = normalize_email(data["email"])
                if isinstance(data.get("password"), str) and len(data["password"]) <= 1024:
                    password = data["password"]
        except ValueError:
            pass
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
            self.limiter.failure(ip)
            log.warning("rakazo bridge: failed sign-in from %s", ip[:64])
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
        except ValueError:
            raise BridgeError(400, "BAD_REQUEST", "The request body is not valid JSON.") from None
        inp = data.get("json", {}) if isinstance(data, dict) else {}
        if inp is None:
            inp = {}
        if not isinstance(inp, dict):
            raise BridgeError(400, "BAD_REQUEST", "The request input must be an object.")
        tenant = self.svc.tenant_for(account.principal_id)
        ctx = _Ctx(account, tenant, account.principal_id, token,
                   "text/event-stream" in headers.get("accept", ""))
        try:
            self.svc.check_rate_limit(key=f"{tenant}:{ctx.user}")
            result = handler(ctx, inp)
        except (AssistantNotFound, KeyError):
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
            journal = self.journals.get(thread.thread_id, assistant.assistant_id)
            journal.sync(messages, runs, approvals)
            return journal, runs

    def _bot(self, ctx: _Ctx, assistant) -> dict[str, Any]:
        thread = self._thread(ctx, assistant)
        messages, runs, _ = self._gather(ctx, assistant, thread)
        visible = [m for m in messages if m.visibility == "visible"]
        active = any(ev.run_status(r.state) in ev.ACTIVE for r in runs)
        return ev.bot_view(assistant, thread.thread_id, ev.snippet(visible[-1].content, 120) if visible else "",
                           "running" if active else "idle")

    # procedures ------------------------------------------------------------------------------------------------
    def _ok(self, ctx, inp):
        return {"ok": True}

    def _me(self, ctx, inp):
        return ev.me_view(ctx.account)

    def _spaces_list(self, ctx, inp):
        return ev.spaces_view(self._bots(ctx))

    def _bots(self, ctx: _Ctx) -> list[dict[str, Any]]:
        return [self._bot(ctx, a) for a in self.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user)]

    def _bots_list(self, ctx, inp):
        return self._bots(ctx)

    def _bots_get(self, ctx, inp):
        return self._bot(ctx, self._assistant(ctx, inp.get("botId")))

    def _bots_create(self, ctx, inp):
        name = inp.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError(400, "BAD_REQUEST", "A bot needs a name of up to 80 characters.")
        text = inp.get("instructions") or inp.get("description") or ""
        if not isinstance(text, str) or len(text) > 20_000:
            raise BridgeError(400, "BAD_REQUEST", "The instructions are too long.")
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
        known_runs, known_messages = {r.run_id for r in runs}, set(journal.messages)
        if not self._sends.acquire(blocking=False):
            raise BridgeError(429, "TOO_MANY_REQUESTS", "Too many messages are being answered. Try again shortly.",
                              {"Retry-After": "5"})
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

        worker = threading.Thread(target=work, name="rakazo-send", daemon=True)
        worker.start()
        deadline = self._monotonic() + 5.0
        run = None
        while run is None:
            _, current = self._journal(ctx, assistant, thread)
            run = next((r for r in current if r.run_id not in known_runs), None)
            if run is not None or not worker.is_alive() or self._monotonic() > deadline:
                break
            time.sleep(0.01)
        if run is None:
            error = outcome.get("error")
            if isinstance(error, AssistantError):
                raise error
            raise BridgeError(500, "INTERNAL_SERVER_ERROR", "The message could not be started.")
        journal, _ = self._journal(ctx, assistant, thread)
        sent = journal.user_message_after(known_messages)
        return {"taskId": run.task_id, "runId": run.run_id, "seq": sent["seq"] if sent else journal.head}

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
        self._target(ctx, inp)
        return {"ok": True, "unreadAfterSeq": None}

    def _runs_list(self, ctx, inp):
        mode = inp.get("filter")
        if mode not in ("active", "recent"):
            raise BridgeError(400, "BAD_REQUEST", "filter must be active or recent.")
        store = self.svc.store
        rows = []
        for run in store.list_runs(tenant_id=ctx.tenant, user_id=ctx.user):
            status = ev.run_status(run.state)
            if mode == "active" and status not in ev.ACTIVE:
                continue
            assistant = store.assistants.get(run.assistant_id)
            thread_messages = [m for m in store.messages.values() if m.thread_id == run.thread_id and m.role == "user"
                               and m.created_at <= run.created_at]
            prompt = max(thread_messages, key=lambda m: m.created_at).content if thread_messages else ""
            rows.append({"runId": run.run_id, "botId": run.assistant_id, "botName": assistant.name if assistant else "",
                         "groupId": None, "groupName": None, "threadId": run.thread_id, "status": status,
                         "trigger": "user", "notificationsEnabled": False, "promptSnippet": ev.snippet(prompt, 120),
                         "agentMessage": None, "updatedAt": ev.iso(run.updated_at)})
            if mode == "recent" and len(rows) >= 20:
                break
        return {"runs": rows}

    def _consent(self, ctx, inp):
        # No third-party AI recipients are named, so the app never asks for consent; this server's model is the operator's.
        return {"scope": ev.SPACE_ID, "version": _CONSENT_VERSION, "recipients": []}

    def _register_push(self, ctx, inp):
        token = inp.get("token")
        if not isinstance(token, str) or not 8 <= len(token) <= 512 or re.search(r"\s", token):
            raise BridgeError(400, "BAD_REQUEST", "That push token is not valid.")
        return {"ok": True}           # accepted and dropped: this server sends no push notifications

    def _threads_subscribe(self, ctx, inp):
        assistant = self._target(ctx, inp)
        cursor = inp.get("cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < -1:
            raise BridgeError(400, "BAD_REQUEST", "cursor must be an integer of at least -1.")
        if not ctx.accepts_stream:
            raise BridgeError(400, "BAD_REQUEST", "Subscribe with accept: text/event-stream.")
        thread = self._thread(ctx, assistant)
        if not self._streams.acquire(blocking=False):
            raise BridgeError(429, "TOO_MANY_REQUESTS", "Too many live connections.", {"Retry-After": "10"})

        def fetch(after: int) -> list[dict[str, Any]]:
            journal, _ = self._journal(ctx, assistant, thread)
            with self.journals.lock:
                return journal.after(after)

        frames = ev.event_stream(fetch, cursor, should_stop=lambda: self._closed.is_set() or not self.tokens.valid(
            ctx.token), **self.stream_options)
        return BridgeResponse(200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                            "X-Accel-Buffering": "no", "Connection": "close"},
                              frames=frames, on_close=self._streams.release)
