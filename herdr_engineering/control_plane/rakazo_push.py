"""Push notifications for the Rakazo phone app, through Expo's push service.

The app mints an Expo push token for its own project and hands it to whichever server it is signed in to
(``notifications/registerPush``). Expo then delivers a message sent to that token, whoever sends it, so this server can
notify the phone without owning the app. A token is not a sign-in credential: it only lets us ask Expo to show a
notification, and it is never logged. Sending happens on one background thread; a slow or failing Expo costs a
notification, never a request. Off unless ``control_plane.rakazo_bridge.push.enabled`` is true.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

log = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
MAX_TOKENS_PER_ACCOUNT = 3
MAX_TITLE, MAX_BODY = 100, 180
MAX_QUEUE = 64
SEEN_LIMIT = 2000
TIMEOUT_SECONDS = 10
MAX_RESPONSE = 64 * 1024


class PostgresPushPersistence:
    """``rakazo_push_tokens`` (migration 0042). Same connection discipline as the token table: short timeouts, the DSN's
    credentials are never logged."""

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

    def load(self) -> list[tuple[str, str, str]]:
        return [(r[0], r[1], r[2]) for r in self._run(
            "SELECT account_id, token, session_sha256 FROM rakazo_push_tokens ORDER BY updated_at", fetch=True)]

    def save(self, account_id: str, token: str, session: str) -> None:
        self._run("INSERT INTO rakazo_push_tokens (account_id, token, session_sha256) VALUES (%s, %s, %s) "
                  "ON CONFLICT (account_id, token) DO UPDATE SET session_sha256 = EXCLUDED.session_sha256, updated_at = now()",
                  (account_id, token, session))

    def remove(self, account_id: str, token: str) -> None:
        self._run("DELETE FROM rakazo_push_tokens WHERE account_id = %s AND token = %s", (account_id, token))


class PushRegistry:
    """Tokens per account (newest last, at most ``MAX_TOKENS_PER_ACCOUNT``). Memory is authoritative; the optional
    persistence is best effort, so a database problem never blocks registering or sending."""

    def __init__(self, persistence=None):
        self._p, self._lock = persistence, threading.Lock()
        self._tokens: dict[str, list[tuple[str, str]]] = {}
        if persistence is not None:
            try:
                for account_id, token, session in persistence.load():
                    self._tokens.setdefault(account_id, []).append((token, session))
            except Exception:  # noqa: BLE001 - start without saved tokens; the app registers again on its next launch
                log.warning("rakazo push: saved tokens could not be loaded")

    def add(self, account_id: str, token: str, session: str) -> None:
        with self._lock:
            rows = [r for r in self._tokens.get(account_id, []) if r[0] != token] + [(token, session)]
            dropped = rows[:-MAX_TOKENS_PER_ACCOUNT]
            self._tokens[account_id] = rows[-MAX_TOKENS_PER_ACCOUNT:]
        self._persist(lambda p: [p.save(account_id, token, session), *[p.remove(account_id, d[0]) for d in dropped]])

    def remove_session(self, account_id: str, session: str) -> int:
        """Forget the tokens this sign-in registered (the app calls unregister on sign-out)."""
        with self._lock:
            gone = [r for r in self._tokens.get(account_id, []) if r[1] == session]
            self._tokens[account_id] = [r for r in self._tokens.get(account_id, []) if r[1] != session]
        self._persist(lambda p: [p.remove(account_id, g[0]) for g in gone])
        return len(gone)

    def remove_token(self, account_id: str, token: str) -> None:
        with self._lock:
            self._tokens[account_id] = [r for r in self._tokens.get(account_id, []) if r[0] != token]
        self._persist(lambda p: p.remove(account_id, token))

    def tokens(self, account_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._tokens.get(account_id, [])]

    def _persist(self, fn: Callable) -> None:
        if self._p is None:
            return
        try:
            fn(self._p)
        except Exception:  # noqa: BLE001
            log.warning("rakazo push: the token table could not be updated")


class ExpoSender:
    """POSTs to Expo's send endpoint. ``access_token`` is only for projects with enhanced push security."""

    def __init__(self, access_token: str | None = None, opener: Callable | None = None, url: str = EXPO_PUSH_URL):
        self._token, self._url = access_token, url
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))   # noqa: S310 - fixed https URL

    def send(self, messages: list[dict]) -> list[dict]:
        """One ticket dict per message (``{"status": "ok"|"error", ...}``); raises OSError on a transport failure."""
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self._url, data=json.dumps(messages).encode(), headers=headers, method="POST")  # noqa: S310
        try:
            with self._open(req, TIMEOUT_SECONDS) as resp:
                raw = resp.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise OSError(f"expo push failed ({exc.code})") from None
        except urllib.error.URLError:
            raise OSError("expo push unreachable") from None
        if len(raw) > MAX_RESPONSE:
            raise OSError("expo push response too large")
        try:
            data = json.loads(raw).get("data")
        except (ValueError, AttributeError):
            raise OSError("expo push returned an invalid response") from None
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def _cap(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


class PushNotifier:
    """Queues notifications and sends them from one daemon thread. A key is notified once; quiet hours (UTC, from the
    operator's YAML) hold nothing back for approvals, which are urgent, but are honoured for ``normal`` priority."""

    def __init__(self, registry: PushRegistry, sender: ExpoSender, target: Callable[[str], dict | None], *,
                 enabled: bool = False, quiet_hours: tuple[int, int] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.registry, self._sender, self._target, self.enabled = registry, sender, target, enabled
        self._quiet, self._clock = quiet_hours, clock
        self._q: queue.Queue = queue.Queue(MAX_QUEUE)
        self._seen: dict[str, None] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.counters = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0, "unregistered": 0}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rakazo-push", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._q.put(None)

    def notify(self, account_id: str, key: str, title: str, body: str, *, kind: str = "help",
               priority: str = "urgent", collapse_id: str | None = None) -> bool:
        """True when queued. False when off, no device is registered, already notified, held for quiet hours, or full."""
        if not self.enabled or not self.registry.tokens(account_id):
            return False
        if priority != "urgent" and self._is_quiet():
            return False
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > SEEN_LIMIT:
                self._seen.pop(next(iter(self._seen)))
        try:
            self._q.put_nowait((account_id, title, body, kind, collapse_id))
        except queue.Full:
            self.counters["dropped"] += 1
            return False
        self.counters["queued"] += 1
        return True

    def _is_quiet(self) -> bool:
        if not self._quiet:
            return False
        start, end = self._quiet
        h = self._clock().hour
        return (h >= start or h < end) if start > end else start <= h < end

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                return
            try:
                self.deliver(*item)
            except Exception:  # noqa: BLE001 - one bad notification must not end the thread
                self.counters["failed"] += 1
                log.warning("rakazo push: a notification could not be sent")

    def deliver(self, account_id: str, title: str, body: str, kind: str, collapse_id: str | None = None) -> None:
        tokens = self.registry.tokens(account_id)
        target = self._target(account_id) or {}
        if not tokens or not target:
            return
        data = {"kind": kind, "deliveryId": uuid.uuid4().hex, **target}
        messages = [{"to": t, "title": _cap(title, MAX_TITLE), "body": _cap(body, MAX_BODY), "data": data,
                     "collapseId": collapse_id or target.get("threadId"), "sound": "default"} for t in tokens]
        try:
            tickets = self._sender.send(messages)
        except OSError as exc:
            self.counters["failed"] += 1
            log.warning("rakazo push: %s", exc)
            return
        for token, ticket in zip(tokens, tickets, strict=False):
            if ticket.get("status") == "ok":
                self.counters["sent"] += 1
            elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                self.registry.remove_token(account_id, token)      # Expo says the device is gone
                self.counters["unregistered"] += 1
            else:
                self.counters["failed"] += 1
                log.warning("rakazo push: Expo refused a notification (%s)", (ticket.get("details") or {}).get("error") or "error")
