"""GitHub credentials for every herdr GitHub reader: a GitHub App (preferred) or a static token.

One interface, ``token_for(owner, permissions) -> str | None``, is what callers use (the
Deployments release evidence and the GitHub issue/project cache). ``credentials()`` resolves it
from the live configuration on every call:

* ``github.app`` configured -> installation tokens of a GitHub App (read only unless a write
  capability is enabled, see below). When
  ``github.token`` is also set the App wins and one warning is logged.
* otherwise ``github.token`` (a static personal token), for any owner, exactly as before.
* neither -> no token (callers treat GitHub as unknown / not configured).

``github.app`` is ``{app_id, private_key_file, installations: {<owner>: <installation_id> | auto}, write: [..]}``.
``write`` lists the write capabilities (``WRITE_CAPABILITIES``) this deployment allows; it is empty by default and a
capability's permission set is fixed here, never chosen by the caller: ``review_gate`` (commit statuses, merge,
update-branch) and ``escalations`` (issue writes). A capability that is not listed is refused with ``write_disabled``
before any call to GitHub.
Only owners listed under ``installations`` ever get a token; ``auto`` finds that owner's
installation id once via ``GET /app/installations`` (cached), it never adds an owner.

App token flow (fail closed: any failure is ``None``, never a partial credential):

* A RS256 JWT is minted from the private key file (``iat`` backdated 60 s, ``exp`` 9 min ahead:
  10 min span, GitHub's maximum). The key file is refused unless its path has no symlink
  (resolved path == path, final component opened O_NOFOLLOW), it is a regular file of mode
  exactly 0600 or 0400, and it is owned by the effective uid (root, e.g. the container under
  rootful docker, may read a key owned by the host user). A missing PyJWT[crypto] is
  ``jwt_library_missing``. The key bytes are dropped as soon as the JWT is signed.
* ``POST /app/installations/{id}/access_tokens`` asks for explicit read-only permissions (a
  consumer passes its own minimal set; anything but ``read`` is refused before the call). The
  answer is validated: a ``ghs_`` token shape, a parsable ``expires_at`` at most 2 h ahead, and
  every granted permission ``read``.
* The token is cached per (owner, permission set) until 5 min before it expires (validity is
  anchored to the monotonic clock and capped at 1 h, so a wall-clock jump cannot extend it).
  Concurrent callers for one key share one refresh (single flight); a failure sets a backoff
  (rate-limit headers honoured, clamped) so waiters and later calls do not re-POST.
* Every HTTP call has a hard wall-clock bound (``CALL_TIMEOUT_S`` <= 10 s; the call runs in a
  daemon thread abandoned at the deadline), a capped body, and redirects are never followed.
* No key, JWT or token is ever logged, put in an exception, or serialised: errors carry fixed
  reasons only, raised ``from None``.
"""
from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import logging
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

API = "https://api.github.com"
CALL_TIMEOUT_S = 10.0
JWT_BACKDATE_S = 60
JWT_AHEAD_S = 540              # exp - iat = 600 s: GitHub's 10 minute maximum
REFRESH_MARGIN_S = 300.0       # a cached token is renewed this long before it expires
MAX_TOKEN_LIFETIME_S = 3600.0  # GitHub issues 1 h tokens; never trust more
MAX_EXPIRES_AHEAD_S = 7200.0   # an expires_at further out is a bad answer (or a bad clock)
FAILURE_BACKOFF_S = 30.0
RATE_LIMIT_DEFAULT_S = 60.0
RATE_LIMIT_MAX_S = 900.0
DISCOVERY_TTL_S = 3600.0
DISCOVERY_PAGES = 10
MAX_RESPONSE = 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
CACHE_ENTRIES = 256

#: Deployments release evidence: releases, release assets and compare.
EVIDENCE_PERMISSIONS = {"contents": "read", "metadata": "read"}
#: The GitHub cache: repository listing and issues.
CATALOG_PERMISSIONS = {"issues": "read", "metadata": "read"}
#: The GitHub cache's Projects v2 sync (organization projects; items reference issues).
PROJECTS_PERMISSIONS = {"issues": "read", "metadata": "read", "organization_projects": "read"}
#: Project intelligence's PR poller: the open pull requests of a mapped repository.
PULLS_PERMISSIONS = {"pull_requests": "read", "metadata": "read"}
#: The review gate's reads: pull requests, their files and commits, compare.
GATE_READ_PERMISSIONS = {"pull_requests": "read", "contents": "read", "metadata": "read"}
#: The only permissions a read request may name, and only at ``read``.
ALLOWED_PERMISSIONS = frozenset({"contents", "metadata", "issues", "organization_projects", "pull_requests"})
#: Write capabilities: the exact, fixed permission set of each. Enabled per deployment in ``github.app.write``.
WRITE_CAPABILITIES = {
    "review_gate": {"contents": "write", "metadata": "read", "pull_requests": "write", "statuses": "write"},
    "escalations": {"issues": "write", "metadata": "read"},
}
_RANK = {"read": 1, "write": 2}

# GitHub's current installation tokens are ~390 chars and contain "-" and "." as well.
_TOKEN = re.compile(r"ghs_[A-Za-z0-9_.-]{16,2000}")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
MAX_REPOSITORIES = 100
_MESSAGES = {
    "invalid_config": "github.app is invalid; fix the YAML configuration.",
    "write_disabled": "This GitHub write capability is not enabled in github.app.write.",
    "key_permissions": "The GitHub App private key file must be a regular file with mode 0600.",
    "key_unreadable": "The GitHub App private key file could not be read.",
    "key_invalid": "The GitHub App private key could not sign a token.",
    "jwt_library_missing": "PyJWT[crypto] is not installed: GitHub App tokens cannot be minted.",
    "unknown_owner": "No GitHub App installation is configured for this owner.",
    "authentication": "GitHub rejected the App credential.",
    "permission": "GitHub denied the requested App permissions.",
    "not_found": "The GitHub App installation is unavailable.",
    "rate_limited": "GitHub App requests are paused until the rate limit resets.",
    "invalid_response": "GitHub returned an incomplete or invalid token response.",
    "timeout": "GitHub did not answer in time.",
    "unavailable": "GitHub is temporarily unavailable.",
    "backoff": "GitHub App token requests are paused after a failure.",
}


class GitHubAuthError(RuntimeError):
    """A fixed, secret-free reason (``reason``); ``retry_s`` a backoff hint."""

    def __init__(self, reason, *, retry_s=None):
        self.reason = reason if reason in _MESSAGES else "unavailable"
        self.retry_s = retry_s
        super().__init__(_MESSAGES[self.reason])
        self.__context__ = None
        self.__suppress_context__ = True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _loopback(host) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


def _check_api(api):
    parts = urllib.parse.urlsplit(api)
    if parts.scheme != "https" and not (parts.scheme == "http" and _loopback(parts.hostname)):
        raise ValueError("the GitHub API base must be https (plain http only to loopback)")
    return api.rstrip("/")


def owner_of(repo) -> str | None:
    """The lower-cased owner of ``owner/name`` (or ``github.com/owner/name``), else None."""
    if not isinstance(repo, str):
        return None
    parts = repo.strip().split("/")
    if len(parts) == 3 and parts[0].lower() == "github.com":
        parts = parts[1:]
    if len(parts) != 2 or not _OWNER.fullmatch(parts[0]):
        return None
    return parts[0].lower()


def _repositories(value) -> tuple:
    """Repository names (no owner) a token is limited to; () = the installation's own scope."""
    if value is None:
        return ()
    names = [value] if isinstance(value, str) else list(value)
    if not names or len(names) > MAX_REPOSITORIES or not all(
            isinstance(n, str) and _REPO_NAME.fullmatch(n) and n not in (".", "..") for n in names):
        raise ValueError("repositories must be 1-100 repository names")
    return tuple(sorted({n.lower() for n in names}))


def _permissions(value) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError("permissions must be a non-empty mapping")
    for name, level in value.items():
        if name not in ALLOWED_PERMISSIONS or level != "read":
            raise ValueError("only read access to the documented permissions may be requested")
    return {name: "read" for name in sorted(value)}


def _granted_within(granted, asked) -> bool:
    """GitHub's answer never exceeds the request: a permission that was asked for comes back at most at the asked
    level; one that was not asked for (GitHub adds ``metadata``) only at ``read``."""
    if not isinstance(granted, dict):
        return False
    for name, level in granted.items():
        limit = _RANK.get(asked.get(name, "read"))
        if _RANK.get(level) is None or limit is None or _RANK[level] > limit:
            return False
    return True


# ------------------------------------------------------------------ configuration
@dataclass(frozen=True)
class AppAuthConfig:
    app_id: int
    private_key_file: str
    installations: tuple  # ((owner_lower, installation_id | None for auto), ...) sorted
    write: tuple = ()     # enabled write capabilities (names from WRITE_CAPABILITIES), sorted

    @property
    def owners(self) -> tuple:
        return tuple(owner for owner, _ in self.installations)

    def __repr__(self) -> str:  # no path detail needed in logs
        return f"AppAuthConfig(app_id={self.app_id}, owners={list(self.owners)!r})"


def _positive_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and len(value) <= 18:
        return int(value) or None
    return None


def parse_app_config(raw) -> AppAuthConfig:
    """Validate ``github.app``. Raises ``GitHubAuthError("invalid_config")`` (no value echoed)."""
    if not isinstance(raw, dict) or set(raw) - {"app_id", "private_key_file", "installations", "write"}:
        raise GitHubAuthError("invalid_config")
    write = raw.get("write", [])
    if (not isinstance(write, list) or len(set(write)) != len(write)
            or any(not isinstance(w, str) or w not in WRITE_CAPABILITIES for w in write)):
        raise GitHubAuthError("invalid_config")
    app_id = _positive_int(raw.get("app_id"))
    path = raw.get("private_key_file")
    installs = raw.get("installations")
    if app_id is None or not isinstance(path, str) or not os.path.isabs(path) \
            or not isinstance(installs, dict) or not installs or len(installs) > 100:
        raise GitHubAuthError("invalid_config")
    table = {}
    for owner, value in installs.items():
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner) or owner.lower() in table:
            raise GitHubAuthError("invalid_config")
        if value == "auto":
            table[owner.lower()] = None
        elif (iid := _positive_int(value)) is not None:
            table[owner.lower()] = iid
        else:
            raise GitHubAuthError("invalid_config")
    return AppAuthConfig(app_id, os.path.normpath(path), tuple(sorted(table.items())), tuple(sorted(write)))


KEY_MODES = (0o600, 0o400)


def read_private_key(path) -> bytes:
    """The PEM bytes. Refused (``key_permissions``): a symlink anywhere in the path (the
    resolved path must equal it), a non-regular file, a mode other than exactly 0600/0400, and
    an owner other than the effective uid, except for root (rootful docker runs the container
    as root while the bind-mounted key keeps its host owner; root can read any file anyway)."""
    if not isinstance(path, str) or os.path.realpath(path) != os.path.normpath(path):
        raise GitHubAuthError("key_permissions")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        reason = "key_permissions" if exc.errno == errno.ELOOP else "key_unreadable"
        raise GitHubAuthError(reason) from None
    try:
        info = os.fstat(fd)
        euid = os.geteuid()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in KEY_MODES \
                or (euid != 0 and info.st_uid != euid):
            raise GitHubAuthError("key_permissions")
        data = b""
        while len(data) <= MAX_KEY_BYTES:
            chunk = os.read(fd, MAX_KEY_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except GitHubAuthError:
        raise
    except OSError:
        raise GitHubAuthError("key_unreadable") from None
    finally:
        os.close(fd)
    if not data or len(data) > MAX_KEY_BYTES:
        raise GitHubAuthError("key_unreadable")
    return data


def mint_jwt(app_id, key_file, now) -> str:
    """A RS256 App JWT: ``iat`` = now - 60 s, ``exp`` = now + 540 s, ``iss`` = the App id."""
    try:
        import jwt  # PyJWT[crypto] (the review-gate/collab extra), imported lazily
        from jwt.algorithms import has_crypto
    except ImportError:
        jwt = None
        has_crypto = False
    if jwt is None or not has_crypto:
        raise GitHubAuthError("jwt_library_missing")
    pem = read_private_key(key_file)
    encoded, failed = None, False
    try:
        issued = int(now)
        encoded = jwt.encode({"iat": issued - JWT_BACKDATE_S, "exp": issued + JWT_AHEAD_S,
                              "iss": str(app_id)}, pem, algorithm="RS256")
    except Exception:  # noqa: BLE001 - never chained: the cause could echo key material
        failed = True
    finally:
        pem = None  # noqa: F841 - drop the key before any exception can carry this frame
    if failed or not isinstance(encoded, str):
        raise GitHubAuthError("key_invalid")
    return encoded


def _parse_expiry(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _rate_pause(code, headers, wall):
    get = headers.get if headers is not None else (lambda _k: None)
    retry, remaining, reset = get("Retry-After"), get("X-RateLimit-Remaining"), get("X-RateLimit-Reset")
    if code == 429 or (code == 403 and (retry is not None or remaining == "0")):
        pause = None
        try:
            if retry is not None:
                pause = float(retry)
            elif reset is not None:
                pause = float(reset) - wall()
        except (TypeError, ValueError):
            pause = None
        if pause is None or pause != pause:
            pause = RATE_LIMIT_DEFAULT_S
        return max(FAILURE_BACKOFF_S, min(pause, RATE_LIMIT_MAX_S))
    return None


# ------------------------------------------------------------------ App tokens
class GitHubAppTokens:
    """Installation tokens for one ``AppAuthConfig`` (see the module docstring)."""

    mode = "app"

    def __init__(self, config: AppAuthConfig, *, api=API, opener=None, clock=time.monotonic,
                 wall=time.time, timeout=CALL_TIMEOUT_S):
        self.config = config
        self._api = _check_api(api)
        # no proxy (a proxy would see the JWT and the token) and no redirects
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                             _NoRedirect())
        self._clock = clock
        self._wall = wall
        self._timeout = min(float(timeout), CALL_TIMEOUT_S)
        self._lock = threading.Lock()
        self._tokens: dict = {}     # (owner, perms) -> (token, valid_until monotonic)
        self._retry: dict = {}      # key -> earliest next attempt (monotonic)
        self._flights: dict = {}    # key -> threading.Event of the running refresh
        self._discovered = None     # {owner_lower: installation_id} from GET /app/installations
        self._discovered_at = float("-inf")
        self._logged: dict = {}     # owner -> last logged failure reason
        self.last_error: dict = {}  # owner -> fixed reason of the last failure (None when fine)

    @property
    def identity(self) -> str:
        """Stable across token rotations: the App id and the configured installations."""
        material = json.dumps(["app", self.config.app_id, list(self.config.installations), list(self.config.write)])
        return hashlib.sha256(material.encode()).hexdigest()

    @property
    def owners(self) -> tuple:
        return self.config.owners

    def __repr__(self) -> str:
        return f"GitHubAppTokens(app_id={self.config.app_id})"

    # -- the interface
    def token_for(self, owner, permissions=None, repositories=None) -> str | None:
        """An installation token for ``owner`` with ``permissions`` (read only), limited to
        ``repositories`` (names under that owner) when the caller knows them; or None."""
        try:
            return self.token_or_raise(owner, permissions, repositories)
        except GitHubAuthError:
            return None

    def token_or_raise(self, owner, permissions=None, repositories=None) -> str:
        perms = _permissions(permissions if permissions is not None else EVIDENCE_PERMISSIONS)
        repos = _repositories(repositories)
        owner = owner.lower() if isinstance(owner, str) else None
        if owner not in dict(self.config.installations):
            raise GitHubAuthError("unknown_owner")
        key = (owner, tuple(sorted(perms.items())), repos)
        return self._single_flight(key, lambda: self._mint(owner, perms, repos))

    def capability_token_or_raise(self, owner, capability, repositories=None) -> str:
        """A repo-scoped installation token with the fixed permission set of a write ``capability``. Refused
        (``write_disabled``) unless ``github.app.write`` lists it; the caller never chooses the permissions."""
        perms = WRITE_CAPABILITIES.get(capability)
        if perms is None:
            raise GitHubAuthError("invalid_config")
        if capability not in self.config.write:
            raise GitHubAuthError("write_disabled")
        repos = _repositories(repositories)
        owner = owner.lower() if isinstance(owner, str) else None
        if owner not in dict(self.config.installations):
            raise GitHubAuthError("unknown_owner")
        key = (owner, tuple(sorted(perms.items())), repos)
        return self._single_flight(key, lambda: self._mint(owner, dict(perms), repos))

    def invalidate(self, owner, token=None) -> None:
        """Drop the cached tokens of ``owner`` (call on a 401 from GitHub). With ``token`` only the entries
        holding that rejected token go, so a fresher token another thread just minted survives."""
        owner = owner.lower() if isinstance(owner, str) else None
        with self._lock:
            for key in [k for k in self._tokens if k[0] == owner and (token is None or self._tokens[k][0] == token)]:
                del self._tokens[key]

    # -- single flight
    def _single_flight(self, key, refresh):
        with self._lock:
            hit = self._tokens.get(key)
            if hit is not None and self._clock() < hit[1]:
                return hit[0]
            if self._clock() < self._retry.get(key, float("-inf")):
                raise GitHubAuthError("backoff")
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = threading.Event()
        if not leader:
            flight.wait(self._timeout * 3 + 1)
            with self._lock:
                hit = self._tokens.get(key)
                if hit is not None and self._clock() < hit[1]:
                    return hit[0]
            raise GitHubAuthError("backoff")
        failure, retry_s, token = "unavailable", None, None
        try:
            token, valid_until = refresh()
            failure = None
        except GitHubAuthError as exc:
            failure, retry_s = exc.reason, exc.retry_s
        except Exception:  # noqa: BLE001 - fail closed, no detail (it could carry a secret)
            pass
        finally:
            # the result is stored before the waiters wake (and even on BaseException)
            with self._lock:
                if failure is None:
                    self._tokens[key] = (token, valid_until)
                    self._retry.pop(key, None)
                    self._trim(self._tokens)
                else:
                    self._retry[key] = self._clock() + max(FAILURE_BACKOFF_S, retry_s or 0)
                    self._trim(self._retry)
                self._flights.pop(key, None)
            flight.set()
        self._note(key[0], failure)
        if failure is not None:
            # raised outside the handler: no __context__ can carry the original error
            raise GitHubAuthError(failure)
        return token

    @staticmethod
    def _trim(table):
        while len(table) > CACHE_ENTRIES:
            table.pop(next(iter(table)))

    def _note(self, owner, reason):
        if not owner:
            return  # the discovery slot: its failure surfaces as the owner's own reason
        self.last_error[owner] = reason
        if self._logged.get(owner) == reason:
            return
        self._logged[owner] = reason
        if reason is None:
            log.info("GitHub App: installation token for %s issued", owner)
        else:
            log.warning("GitHub App: no installation token for %s: %s", owner, _MESSAGES[reason])

    # -- minting
    def _mint(self, owner, perms, repos=()):
        iid = self._installation_id(owner)
        jwt_value = mint_jwt(self.config.app_id, self.config.private_key_file, self._wall())
        body = {"permissions": perms}
        if repos:
            body["repositories"] = list(repos)
        status, _headers, raw = self._call("POST", f"/app/installations/{iid}/access_tokens",
                                           jwt_value, body)
        jwt_value = None  # noqa: F841
        if status not in (200, 201):
            raise GitHubAuthError("invalid_response")
        data = self._json(raw)
        raw = None  # noqa: F841
        token = data.get("token") if isinstance(data, dict) else None
        expires = _parse_expiry(data.get("expires_at")) if isinstance(data, dict) else None
        granted = data.get("permissions") if isinstance(data, dict) else None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token) or expires is None:
            raise GitHubAuthError("invalid_response")
        if granted is not None and not _granted_within(granted, perms):
            raise GitHubAuthError("invalid_response")  # a broader grant than asked: refuse it
        remaining = expires - self._wall()
        if remaining <= REFRESH_MARGIN_S or remaining > MAX_EXPIRES_AHEAD_S:
            raise GitHubAuthError("invalid_response")
        return token, self._clock() + min(remaining, MAX_TOKEN_LIFETIME_S) - REFRESH_MARGIN_S

    _DISCOVERY = ("", (("discovery", "read"),), ())

    def _installation_id(self, owner):
        configured = dict(self.config.installations)[owner]
        if configured is not None:
            return configured
        with self._lock:
            table, at = self._discovered, self._discovered_at
        fresh = table is not None and self._clock() - at < DISCOVERY_TTL_S
        if table is not None and (owner in table or fresh):
            if owner in table:
                return table[owner]
            raise GitHubAuthError("not_found")
        self._single_flight(self._DISCOVERY, lambda: (self._discover(), self._clock() + DISCOVERY_TTL_S))
        with self._lock:
            table = dict(self._discovered or {})
        if owner not in table:
            raise GitHubAuthError("not_found")
        return table[owner]

    def _forget_discovery(self):
        with self._lock:  # an `auto` id may be stale: re-discover on the next miss
            self._discovered, self._discovered_at = None, float("-inf")
            self._tokens.pop(self._DISCOVERY, None)

    def _discover(self):
        jwt_value = mint_jwt(self.config.app_id, self.config.private_key_file, self._wall())
        wanted = set(self.config.owners)
        found: dict = {}
        deadline = self._clock() + 2 * self._timeout   # the whole walk, not only each call
        for page in range(1, DISCOVERY_PAGES + 1):
            if page > 1 and self._clock() > deadline:
                raise GitHubAuthError("timeout")
            status, _h, raw = self._call("GET", f"/app/installations?per_page=100&page={page}",
                                         jwt_value, None)
            data = self._json(raw)
            if status != 200 or not isinstance(data, list):
                raise GitHubAuthError("invalid_response")
            for item in data:
                account = item.get("account") if isinstance(item, dict) else None
                login = account.get("login") if isinstance(account, dict) else None
                iid = item.get("id") if isinstance(item, dict) else None
                if isinstance(login, str) and login.lower() in wanted and type(iid) is int \
                        and iid > 0:
                    found[login.lower()] = iid
            if len(data) < 100:
                break
        with self._lock:
            self._discovered, self._discovered_at = found, self._clock()
        return "discovered"  # the single-flight slot holds a marker, never a credential

    # -- HTTP
    @staticmethod
    def _json(raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise GitHubAuthError("invalid_response") from None

    def _call(self, method, path, bearer, body):
        """(status, headers, body) of one bounded call; any failure is a GitHubAuthError."""
        request = urllib.request.Request(
            self._api + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "herdr-engineering",
                     "Content-Type": "application/json"})
        bearer = None  # noqa: F841
        result: dict = {}
        deadline = time.monotonic() + self._timeout

        def call():
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    raw = b""
                    while len(raw) <= MAX_RESPONSE:
                        if time.monotonic() > deadline:
                            result["error"] = "timeout"
                            return
                        chunk = response.read(min(65536, MAX_RESPONSE + 1 - len(raw)))
                        if not chunk:
                            break
                        raw += chunk
                    result["ok"] = (response.status, response.headers, raw)
            except urllib.error.HTTPError as exc:
                result["http"] = (exc.code, exc.headers)
                exc.close()
            except Exception as exc:  # noqa: BLE001 - only the kind is kept
                result["error"] = "timeout" if isinstance(exc, TimeoutError) else "unavailable"

        worker = threading.Thread(target=call, name="github-app-token", daemon=True)
        worker.start()
        worker.join(self._timeout)
        request = None  # noqa: F841
        if worker.is_alive():
            raise GitHubAuthError("timeout")
        if "ok" in result:
            status, headers, raw = result["ok"]
            if len(raw) > MAX_RESPONSE:
                raise GitHubAuthError("invalid_response")
            return status, headers, raw
        if "http" in result:
            code, headers = result["http"]
            pause = _rate_pause(code, headers, self._wall)
            if pause is not None:
                raise GitHubAuthError("rate_limited", retry_s=pause)
            reason = {401: "authentication", 403: "permission", 404: "not_found",
                      422: "permission"}.get(code, "unavailable")
            if reason == "not_found":
                self._forget_discovery()
            raise GitHubAuthError(reason)
        raise GitHubAuthError(result.get("error", "unavailable"))


# ------------------------------------------------------------------ static token
def static_token_ok(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 16384
            and all(32 < ord(c) < 127 for c in value))


class StaticToken:
    """``github.token``: the same token for every owner (permissions are the token's own)."""

    mode = "token"
    owners = ()

    def __init__(self, token):
        self._token = token if static_token_ok(token) else None

    @property
    def identity(self) -> str:
        return hashlib.sha256((self._token or "").encode()).hexdigest()

    def token_for(self, owner=None, permissions=None, repositories=None) -> str | None:
        return self._token

    def capability_token_or_raise(self, owner, capability, repositories=None) -> str:
        raise GitHubAuthError("write_disabled")     # write capabilities exist only for a GitHub App

    def invalidate(self, owner=None, token=None) -> None:
        return None

    def __repr__(self) -> str:
        return "StaticToken()"


class NoCredentials:
    """No usable credential (nothing configured, or an invalid ``github.app``: fail closed)."""

    mode = None
    owners = ()

    def __init__(self, reason="not_configured"):
        self.reason = reason

    @property
    def identity(self) -> str:
        return hashlib.sha256(b"").hexdigest()

    def token_for(self, owner=None, permissions=None, repositories=None):
        return None

    def capability_token_or_raise(self, owner, capability, repositories=None) -> str:
        raise GitHubAuthError("write_disabled")

    def invalidate(self, owner=None, token=None) -> None:
        return None


# ------------------------------------------------------------------ resolution
_resolve_lock = threading.Lock()
_app_instances: dict = {}  # AppAuthConfig -> GitHubAppTokens (shared: token caches survive)
_warned_both = False
_warned_invalid = False


def _host_read(key):
    from .host_config import host_value
    return host_value(key)


def credentials(*, read=None, api=API, opener=None):
    """The live credential provider: ``github.app`` (wins), else ``github.token``, else none."""
    global _warned_both, _warned_invalid
    read = read or _host_read
    app_raw, token = read("github.app"), read("github.token")
    if app_raw in (None, {}, ""):
        return StaticToken(token) if static_token_ok(token) else NoCredentials()
    try:
        config = parse_app_config(app_raw)
    except GitHubAuthError:
        if not _warned_invalid:
            _warned_invalid = True
            log.warning("github.app is invalid: no GitHub credential is used (github.token is "
                        "not a fallback while github.app is configured)")
        return NoCredentials("invalid_config")
    _warned_invalid = False
    if static_token_ok(token) and not _warned_both:
        _warned_both = True
        log.warning("both github.app and github.token are configured: the GitHub App is used "
                    "for reads (github.token only for deployment escalation issue writes)")
    key = (config, api)
    with _resolve_lock:
        tokens = _app_instances.get(key)
        if tokens is None:
            tokens = _app_instances[key] = GitHubAppTokens(config, api=api, opener=opener)
            while len(_app_instances) > 8:
                _app_instances.pop(next(iter(_app_instances)))
        return tokens


def _reset_for_tests():
    global _warned_both, _warned_invalid
    with _resolve_lock:
        _app_instances.clear()
    _warned_both = _warned_invalid = False
