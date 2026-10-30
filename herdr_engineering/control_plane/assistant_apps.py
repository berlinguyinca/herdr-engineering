"""The assistant's app connections: provider listing and the Google OAuth web flow.

Pending sign-ins live in memory only (state hash -> person, provider, sealed PKCE verifier, expiry); a restart simply
invalidates them. A state is single use: it is removed on first lookup even if a later step fails. Secrets, codes and
tokens never appear in a listing, an exception or a log line.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable

from herdr_engineering.control_plane import google_oauth
from herdr_engineering.control_plane.app_credentials import AppsError, CredentialStore
from herdr_engineering.control_plane.google_workspace import GoogleError

STATE_TTL = 600
GOOGLE = "google"
CALLBACK_PATH = "/api/v1/assistant-apps/google/callback"
CARDS = (("gmail", "Gmail"), ("calendar", "Google Calendar"))


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


class AppsService:
    def __init__(self, store: CredentialStore, *, redirect_base: str, opener: Callable | None = None,
                 clock: Callable[[], float] = time.time, github_status_fn: Callable[[], dict] | None = None):
        self._store, self._clock = store, clock
        self._open = opener or google_oauth._urlopen
        self._redirect = redirect_base.rstrip("/") + CALLBACK_PATH
        self._github = github_status_fn
        self._pending: dict[str, tuple[str, str, str, str, float]] = {}
        self._lock = threading.Lock()

    # --- listing
    def client_configured(self, provider: str) -> bool:
        try:
            return self._store.get_client(provider) is not None
        except AppsError:
            return False

    def providers(self, tenant: str, user: str) -> list[dict]:
        conn = self._store.get_connection(tenant, user, GOOGLE)
        configured = self.client_configured(GOOGLE)
        out = []
        for card_id, name in CARDS:
            out.append({"id": card_id, "name": name, "kind": "oauth", "provider": GOOGLE, "connected": conn is not None,
                        "account": conn.account if conn else "", "status": conn.status if conn else "not_connected",
                        "scopes": list(conn.scopes) if conn else [], "client_configured": configured})
        gh: dict = {}
        if self._github is not None:
            try:
                gh = self._github() or {}
            except Exception:  # noqa: BLE001 - a broken status probe must not hide the other cards
                gh = {}
        out.append({"id": "github", "name": "GitHub", "kind": "status", "provider": "github",
                    "connected": gh.get("connected") is True, "account": str(gh.get("account", "") or ""),
                    "status": str(gh.get("status", "") or ("connected" if gh.get("connected") is True else "not_connected")),
                    "scopes": [], "client_configured": False})
        return out

    def save_client(self, provider: str, client_id: str, client_secret: str) -> None:
        if provider != GOOGLE:
            raise AppsError("unknown_provider")
        if not isinstance(client_id, str) or not isinstance(client_secret, str) or not client_id.strip() or not client_secret.strip():
            raise AppsError("bad_client")
        self._store.put_client(provider, client_id.strip(), client_secret.strip())

    def blocked_calendars(self, tenant: str, user: str) -> list[str]:
        return self._store.get_blocked(tenant, user, GOOGLE)

    def set_blocked_calendars(self, tenant: str, user: str, ids: list[str]) -> None:
        self._store.set_blocked(tenant, user, GOOGLE, ids)

    # --- sign-in
    def _purge(self, now: float) -> None:
        for key in [k for k, v in self._pending.items() if v[4] <= now]:
            del self._pending[key]

    def connect_start(self, tenant: str, user: str, provider: str) -> str:
        if provider != GOOGLE:
            raise AppsError("unknown_provider")
        client = self._store.get_client(GOOGLE)
        if client is None:
            raise AppsError("no_client")
        state, verifier, challenge = google_oauth.new_pkce()
        sealed = self._store._box.seal(verifier)
        now = self._clock()
        with self._lock:
            self._purge(now)
            self._pending[_digest(state)] = (tenant, user, provider, sealed, now + STATE_TTL)
        return google_oauth.authorization_url(client[0], self._redirect, state, challenge)

    def connect_callback(self, provider: str, params: dict) -> tuple[str, str]:
        state = params.get("state")
        if not isinstance(state, str) or not state:
            raise AppsError("bad_state")
        now = self._clock()
        with self._lock:
            self._purge(now - STATE_TTL)     # keeps a just-expired entry findable so it reports "expired"
            entry = self._pending.pop(_digest(state), None)
        if entry is None or entry[2] != provider or provider != GOOGLE:
            raise AppsError("bad_state")
        tenant, user, _, sealed, expires = entry
        if expires <= now:
            raise AppsError("expired")
        if params.get("error"):
            raise AppsError("denied")
        code = params.get("code")
        if not isinstance(code, str) or not code:
            raise AppsError("denied")
        client = self._store.get_client(GOOGLE)
        if client is None:
            raise AppsError("no_client")
        verifier = self._store._box.open(sealed)
        try:
            data = google_oauth.exchange_code(self._open, client, code, verifier, self._redirect)
        except GoogleError:
            raise AppsError("exchange_failed") from None
        granted = set(str(data.get("scope", "")).split())
        if not granted or granted - set(google_oauth.SCOPES) or set(google_oauth.SCOPES) - granted:
            raise AppsError("unexpected_scope")
        refresh, access = data.get("refresh_token"), data.get("access_token")
        if not isinstance(refresh, str) or not refresh or not isinstance(access, str) or not access:
            raise AppsError("no_refresh_token")
        try:
            account = google_oauth.account_address(self._open, access)
        except GoogleError:
            raise AppsError("exchange_failed") from None
        self._store.put_connection(tenant, user, GOOGLE, account=account, scopes=sorted(granted), refresh_token=refresh)
        return tenant, user

    def disconnect(self, tenant: str, user: str, provider: str) -> None:
        if provider != GOOGLE:
            raise AppsError("unknown_provider")
        try:
            token = self._store.refresh_token(tenant, user, GOOGLE)
        except AppsError:
            token = None
        self._store.delete_connection(tenant, user, GOOGLE)
        if token:
            google_oauth.revoke(self._open, token)
