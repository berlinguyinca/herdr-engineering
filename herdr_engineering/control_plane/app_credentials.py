"""Encrypted per-person credentials for the assistant's app connections (Gmail, Calendar).

Refresh tokens, OAuth client secrets and PKCE verifiers are sealed with Fernet; the 32-byte key is a file on the control
plane, never in the database or config. Without a usable key file nothing can be saved (``AppsError("not_set_up")``).
Memory is authoritative and the optional Postgres persistence is best effort, so a database problem never loses a
connection that was just made. Known limitation (accepted): a delete the database refused is retried from memory only,
so if the process restarts before a retry succeeds, the saved row returns on the next load. Tokens are never logged, never in a ``repr``, and never on ``Connection``.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

KEY_BYTES = 32
STATUSES = ("connected", "needs_attention")


class AppsError(Exception):
    """``reason`` is a fixed string; the message never carries a secret."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SecretBox:
    def __init__(self, key_file: str | None):
        self._fernet = None
        if not key_file:
            return
        try:
            with open(key_file, "rb") as fh:
                raw = fh.read(KEY_BYTES + 1)
            if len(raw) != KEY_BYTES:
                return
            from cryptography.fernet import Fernet
            self._fernet = Fernet(base64.urlsafe_b64encode(raw))
        except Exception:  # noqa: BLE001 - unreadable key file == not set up; never say why with the path's contents
            self._fernet = None

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def seal(self, plain: str) -> str:
        if self._fernet is None:
            raise AppsError("not_set_up")
        return self._fernet.encrypt(plain.encode()).decode()

    def open(self, token: str) -> str:
        if self._fernet is None:
            raise AppsError("not_set_up")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except Exception as exc:  # noqa: BLE001
            raise AppsError("unreadable") from exc

    def __repr__(self) -> str:
        return f"SecretBox(available={self.available})"


@dataclass(frozen=True)
class Connection:
    provider: str
    account: str
    scopes: tuple[str, ...]
    status: str
    created_at: str


class PostgresCredentialPersistence:
    """``assistant_app_*`` tables (migration 0043). Holds only ciphertext. Short timeouts; the DSN is never logged."""

    def __init__(self, dsn: str, connect: Callable | None = None):
        self._dsn, self._connect = dsn, connect

    def _run(self, sql: str, args: tuple = (), fetch: bool = False):
        if self._connect is not None:
            conn = self._connect()
        else:
            import psycopg2
            conn = psycopg2.connect(self._dsn, connect_timeout=3, options="-c statement_timeout=3000 -c lock_timeout=3000")
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def load_clients(self) -> list[tuple[str, str, str]]:
        return [(r[0], r[1], r[2]) for r in self._run(
            "SELECT provider, client_id, secret_ct FROM assistant_app_clients", fetch=True)]

    def save_client(self, provider: str, client_id: str, secret_ct: str) -> None:
        self._run("INSERT INTO assistant_app_clients (provider, client_id, secret_ct) VALUES (%s, %s, %s) "
                  "ON CONFLICT (provider) DO UPDATE SET client_id = EXCLUDED.client_id, secret_ct = EXCLUDED.secret_ct",
                  (provider, client_id, secret_ct))

    def load_connections(self) -> list[tuple]:
        return [tuple(r) for r in self._run(
            "SELECT tenant_id, user_id, provider, account, scopes::text, status, token_ct, created_at "
            "FROM assistant_app_connections", fetch=True)]

    def save_connection(self, tenant: str, user: str, provider: str, account: str, scopes: list[str],
                        status: str, token_ct: str, created_at: str) -> None:
        self._run("INSERT INTO assistant_app_connections (tenant_id, user_id, provider, account, scopes, status, token_ct, "
                  "created_at) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s) "
                  "ON CONFLICT (tenant_id, user_id, provider) DO UPDATE SET account = EXCLUDED.account, "
                  "scopes = EXCLUDED.scopes, status = EXCLUDED.status, token_ct = EXCLUDED.token_ct",
                  (tenant, user, provider, account, json.dumps(scopes), status, token_ct, created_at))

    def set_status(self, tenant: str, user: str, provider: str, status: str) -> None:
        self._run("UPDATE assistant_app_connections SET status = %s WHERE tenant_id = %s AND user_id = %s AND provider = %s",
                  (status, tenant, user, provider))

    def delete_connection(self, tenant: str, user: str, provider: str) -> None:
        self._run("DELETE FROM assistant_app_connections WHERE tenant_id = %s AND user_id = %s AND provider = %s",
                  (tenant, user, provider))

    def load_blocked(self) -> list[tuple[str, str, str, str]]:
        return [tuple(r) for r in self._run(
            "SELECT tenant_id, user_id, provider, calendar_ids::text FROM assistant_app_blocked", fetch=True)]

    def save_blocked(self, tenant: str, user: str, provider: str, ids: list[str]) -> None:
        self._run("INSERT INTO assistant_app_blocked (tenant_id, user_id, provider, calendar_ids) "
                  "VALUES (%s, %s, %s, %s::jsonb) "
                  "ON CONFLICT (tenant_id, user_id, provider) DO UPDATE SET calendar_ids = EXCLUDED.calendar_ids",
                  (tenant, user, provider, json.dumps(ids)))


def _serialized(fn):
    def wrapper(self, *a, **k):
        with self._plock:
            return fn(self, *a, **k)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


class CredentialStore:
    def __init__(self, box: SecretBox, persistence=None):
        self._box, self._p, self._lock = box, persistence, threading.Lock()
        # Writers hold _plock across "change memory, then persist" so a tombstone retry and an upsert for the same key
        # cannot interleave. Lock order is always _plock then _lock; readers take only _lock, so there is no cycle.
        self._plock = threading.RLock()
        self._clients: dict[str, tuple[str, str]] = {}      # provider -> (client_id, sealed secret)
        self._conns: dict[tuple[str, str, str], dict] = {}  # (tenant, user, provider) -> row with sealed token
        self._tombstones: set[tuple[str, str, str]] = set()  # deletes the database has not confirmed yet
        self._blocked: dict[tuple[str, str, str], list[str]] = {}
        if persistence is not None:
            try:
                for provider, client_id, ct in persistence.load_clients():
                    self._clients[provider] = (client_id, ct)
                for row in persistence.load_connections():
                    try:
                        t, u, p, account, scopes, status, ct, created = row
                        self._conns[(t, u, p)] = {
                            "account": account, "scopes": tuple(json.loads(scopes)), "status": status, "token_ct": ct,
                            "created_at": created.astimezone(UTC).isoformat() if hasattr(created, "astimezone") else str(created)}
                    except Exception:  # noqa: BLE001 - one bad row must not hide the others
                        log.warning("app credentials: a saved connection row was skipped")
                for t, u, p, ids in persistence.load_blocked():
                    self._blocked[(t, u, p)] = list(json.loads(ids))
            except Exception:  # noqa: BLE001 - start empty; people reconnect
                log.warning("app credentials: saved rows could not be loaded")

    def _persist(self, fn: Callable) -> None:
        # Best effort, serialized by _plock (held by every writer): memory already changed, so a failed write is only logged.
        if self._p is None:
            return
        self._retry_tombstones()
        try:
            fn(self._p)
        except Exception:  # noqa: BLE001
            log.warning("app credentials: the database could not be updated")

    def _retry_tombstones(self) -> None:
        with self._plock:
            for key in list(self._tombstones):
                try:
                    self._p.delete_connection(*key)
                except Exception:  # noqa: BLE001
                    log.warning("app credentials: delete not persisted")
                    return
                self._tombstones.discard(key)

    # --- OAuth client (one per provider, shared by the whole deployment)
    @_serialized
    def put_client(self, provider: str, client_id: str, client_secret: str) -> None:
        ct = self._box.seal(client_secret)
        with self._lock:
            self._clients[provider] = (client_id, ct)
        self._persist(lambda p: p.save_client(provider, client_id, ct))

    def get_client(self, provider: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._clients.get(provider)
        return None if row is None else (row[0], self._box.open(row[1]))

    # --- per-person connections
    @_serialized
    def put_connection(self, tenant: str, user: str, provider: str, *, account: str, scopes: list[str],
                       refresh_token: str) -> None:
        ct = self._box.seal(refresh_token)
        key = (tenant, user, provider)
        with self._lock:
            old = self._conns.get(key)   # reconnecting keeps the first created_at, as the database upsert does
            created = old["created_at"] if old else datetime.now(UTC).isoformat()
            self._conns[key] = {"account": account, "scopes": tuple(scopes), "status": "connected",
                                "token_ct": ct, "created_at": created}
            self._tombstones.discard(key)
        self._persist(lambda p: p.save_connection(tenant, user, provider, account, list(scopes), "connected", ct, created))

    @staticmethod
    def _view(provider: str, row: dict) -> Connection:
        return Connection(provider, row["account"], row["scopes"], row["status"], row["created_at"])

    def get_connection(self, tenant: str, user: str, provider: str) -> Connection | None:
        with self._lock:
            row = self._conns.get((tenant, user, provider))
            return None if row is None else self._view(provider, row)

    def list_connections(self, tenant: str, user: str) -> list[Connection]:
        with self._lock:
            return [self._view(k[2], v) for k, v in sorted(self._conns.items()) if k[:2] == (tenant, user)]

    @_serialized
    def set_status(self, tenant: str, user: str, provider: str, status: str) -> None:
        if status not in STATUSES:
            raise AppsError("bad_status")
        with self._lock:
            row = self._conns.get((tenant, user, provider))
            if row is None:
                return
            row["status"] = status
        self._persist(lambda p: p.set_status(tenant, user, provider, status))

    @_serialized
    def delete_connection(self, tenant: str, user: str, provider: str) -> None:
        key = (tenant, user, provider)
        with self._lock:
            self._conns.pop(key, None)
            if self._p is not None:
                self._tombstones.add(key)
        if self._p is not None:
            self._retry_tombstones()

    def refresh_token(self, tenant: str, user: str, provider: str) -> str | None:
        with self._lock:
            row = self._conns.get((tenant, user, provider))
        return None if row is None else self._box.open(row["token_ct"])

    # --- blocked calendars (not secrets; kept apart from the connection so they survive reconnecting)
    def get_blocked(self, tenant: str, user: str, provider: str) -> list[str]:
        with self._lock:
            return list(self._blocked.get((tenant, user, provider), []))

    @_serialized
    def set_blocked(self, tenant: str, user: str, provider: str, ids: list[str]) -> None:
        clean = list(dict.fromkeys(str(i) for i in ids))
        with self._lock:
            self._blocked[(tenant, user, provider)] = clean
        self._persist(lambda p: p.save_blocked(tenant, user, provider, clean))
