"""Release evidence from GitHub for the deployment coordinator (deploy-4 follow-up A).

``GitHubReleaseEvidence(definitions)`` is the coordinator's ``evidence`` callable,
``fn(track, target, plan_commit) -> ReleaseEvidence``:

* ``published_commit``: the content of the ``<follows.asset>.commit`` asset of the
  ``follows.tag`` release (``node-ci-latest``) of the track's repository, for every
  (tag, asset) the track's definitions name. They must all carry the same 40-hex commit,
  else None.
* ``on_main``: ``compare/{branch}...{target}`` is ``identical`` or ``behind`` with
  ``ahead_by == 0`` (the target is the branch tip or an ancestor of it); ``ahead`` or
  ``diverged`` is False. When ``plan_commit`` is another commit, ``compare/{target}...
  {plan}`` must not be ``ahead`` (the plan ahead of the target = the target is older:
  False). A ``diverged`` plan was force-pushed away; the target being on main decides.
  The expected ancestor is the compare's head, so a good answer lists no commits and
  stays small; an oversized answer fails closed.
* CI green is not fetched here: the coordinator's target is only ever a commit whose
  whole CI pipeline herdr saw succeed on a main push (``DeploymentDefinitions``), which
  is stricter than the ``follows.ci_context`` status alone.

Safety rules:

* The call only reads a cache and never waits on GitHub. A missing or expired entry
  queues one single-flight refresh on a daemon worker; the coordinator sees it on a later
  tick (ticks are 30 s apart).
* A success is fresh for ``SUCCESS_TTL_S`` and served at most ``MAX_AGE_S`` after it was
  fetched; then unknown. A failed refresh sets only a retry backoff (``FAILURE_RETRY_S``)
  and never refreshes, extends or replaces a success. A definitive answer (another
  commit, not on main) is a success and replaces the entry.
* The key is (track, (tag, asset)s, target, plan); a plan equal to the target counts as
  none, and a refresh with a plan also stores the plan-free answer, so the tick after a
  Replan (whose plan is then the target) hits the cache instead of reading unconfirmed.
* Every HTTP call has a hard wall-clock bound (``CALL_TIMEOUT_S``): it runs in a daemon
  thread and is abandoned at the deadline. Responses are size-capped, JSON is validated,
  commits must be 40 hex. Redirects are never followed by urllib; the asset's one
  redirect is followed by hand, only to an allowed download host, without credentials.
* A 403/429 rate limit pauses every refresh for its Retry-After / X-RateLimit-Reset,
  clamped to [FAILURE_RETRY_S, RATE_LIMIT_MAX_S]. API reads send If-None-Match with the
  ETag of the last body that parsed; a 304 re-parses and re-validates that body, and a
  304 without one is a failure, so a 304 can never confirm anything a 200 did not.
* The token is the server-held ``github.token`` (read on every refresh, so a change
  applies live). It goes only to the API base, never into an error, a status or a log:
  failures are recorded as fixed kinds.
* Any error is unknown (``ReleaseEvidence()``), never confirmed.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from .deployment_coordinator import ReleaseEvidence, same_commit

log = logging.getLogger(__name__)

API = "https://api.github.com"
#: Release-asset downloads redirect here (objects./release-assets.githubusercontent.com).
DOWNLOAD_SUFFIX = ".githubusercontent.com"
SUCCESS_TTL_S = 60.0
FAILURE_RETRY_S = 15.0
MAX_AGE_S = 180.0
CALL_TIMEOUT_S = 10.0
MAX_JSON = 1024 * 1024
MAX_COMMIT_FILE = 256
MAX_ASSETS = 1000
CACHE_ENTRIES = 64
QUEUE_LIMIT = 32
#: A 403/429 rate limit pauses every refresh (one token) for Retry-After / until
#: X-RateLimit-Reset, at least FAILURE_RETRY_S and at most this long.
RATE_LIMIT_MAX_S = 900.0
RATE_LIMIT_DEFAULT_S = 60.0
MAX_ETAG = 200

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_NAME = re.compile(r"[A-Za-z0-9_.+-]{1,128}")
_STATUSES = {"identical", "behind", "ahead", "diverged"}


class _Fail(Exception):
    """A refresh failure; ``kind`` is a fixed, token-free description; ``pause_s`` a
    rate-limit backoff for every refresh."""

    def __init__(self, kind, pause_s=None):
        super().__init__(kind)
        self.kind = kind
        self.pause_s = pause_s


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _loopback(host) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def download_allowed(location, download_hosts=()) -> bool:
    """The asset redirect may go to https ``*.githubusercontent.com`` only, or to a host
    listed in ``download_hosts`` (tests: loopback, where plain http is accepted)."""
    if not isinstance(location, str):
        return False
    try:
        parts = urllib.parse.urlsplit(location)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme == "https" and host.endswith(DOWNLOAD_SUFFIX) and len(host) > len(DOWNLOAD_SUFFIX):
        return True
    return host in set(download_hosts or ()) and (parts.scheme == "https" or _loopback(host))


def _rate_limit_pause(code, headers, wall):
    """Seconds to pause for a rate-limited answer, or None when it is not one."""
    get = (headers.get if headers is not None else (lambda _k: None))
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
        if pause is None or pause != pause:          # NaN
            pause = RATE_LIMIT_DEFAULT_S
        return max(FAILURE_RETRY_S, min(pause, RATE_LIMIT_MAX_S))
    return None


def _token_ok(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 16384
            and all(32 < ord(c) < 127 for c in value))


def _default_token():
    from ..host_config import host_value
    return host_value("github.token")


class GitHubReleaseEvidence:
    """See the module docstring. ``definitions`` returns {component: Definition};
    ``executor(fn)`` runs a refresh (default: the daemon worker)."""

    def __init__(self, definitions, *, token=None, api=API, opener=None, download_hosts=None,
                 clock=time.monotonic, executor=None, timeout=CALL_TIMEOUT_S, wall=time.time):
        parts = urllib.parse.urlsplit(api)
        if parts.scheme != "https" and not (parts.scheme == "http" and _loopback(parts.hostname)):
            raise ValueError("the GitHub API base must be https (plain http only to loopback)")
        self._definitions = definitions
        self._token = token or _default_token
        self._api = api.rstrip("/")
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self._download_hosts = set(download_hosts or ())
        self._clock = clock
        self._executor = executor or self._enqueue
        self._timeout = min(float(timeout), CALL_TIMEOUT_S)
        self._lock = threading.Lock()
        self._cache: OrderedDict = OrderedDict()      # key -> (ReleaseEvidence, fetched_at)
        self._retry_at: OrderedDict = OrderedDict()   # key -> earliest next refresh
        self._pending: set = set()
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._worker = None
        self._state = {"state": "unknown", "error": None}
        self._tracks: OrderedDict = OrderedDict()     # track -> {"state", "error"}
        self._paused_until = float("-inf")             # rate limit: no refresh before
        self._etags: OrderedDict = OrderedDict()      # url -> (ETag, raw body that parsed)
        self._wall = wall
        self._logged = None

    # ------------------------------------------------------------ the call
    def __call__(self, track, target, plan_commit) -> ReleaseEvidence:
        key = self._key(track, target, plan_commit)
        if key is None:
            return ReleaseEvidence()
        if self._due(key):
            try:
                self._executor(lambda: self._refresh(key))
            except Exception:  # noqa: BLE001 - a full queue retries on a later tick
                with self._lock:
                    self._pending.discard(key)
        return self._cached(key)

    def status(self) -> dict:
        """{state, error} over every track: "configured" only when each track's last refresh
        succeeded; ``error`` is a failing track's fixed kind (one track's success never
        hides another's error)."""
        with self._lock:
            if not self._tracks:
                return dict(self._state)
            errors = [st["error"] for _t, st in sorted(self._tracks.items()) if st["error"]]
            ok = all(st["state"] == "configured" for st in self._tracks.values())
            return {"state": "configured" if ok else "unknown",
                    "error": errors[0] if errors else None}

    def status_for(self, track) -> dict:
        """{state, error} of ``track``'s last refresh ("unknown", None before any)."""
        with self._lock:
            return dict(self._tracks.get(tuple(track) if isinstance(track, (list, tuple)) else track,
                                         {"state": "unknown", "error": None}))

    def _set_track(self, track, state, error):
        self._state = {"state": state, "error": error}
        self._tracks[track] = {"state": state, "error": error}
        self._tracks.move_to_end(track)
        while len(self._tracks) > CACHE_ENTRIES:
            self._tracks.popitem(last=False)

    def _key(self, track, target, plan_commit):
        if not (isinstance(track, tuple) and len(track) == 3
                and all(isinstance(x, str) for x in track)):
            return None
        if not isinstance(target, str) or not _SHA.fullmatch(target):
            return None
        if not _REPO.fullmatch(track[0]) or not _NAME.fullmatch(track[1]):
            return None
        try:
            defs = self._definitions() or {}
        except Exception:  # noqa: BLE001 - no definitions view: unknown
            return None
        specs = tuple(sorted({(d.follows.get("tag"), d.follows.get("asset"))
                              for d in defs.values() if getattr(d, "track", None) == track}))
        if not specs or not all(isinstance(t, str) and _NAME.fullmatch(t)
                                and isinstance(a, str) and _NAME.fullmatch(a) for t, a in specs):
            return None
        plan = plan_commit if isinstance(plan_commit, str) and _SHA.fullmatch(plan_commit) else None
        if plan is not None and same_commit(plan, target):
            plan = None              # nothing to compare: shares the entry the Replan tick used
        return (track, specs, target, plan)

    def _due(self, key) -> bool:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and now - entry[1] < SUCCESS_TTL_S:
                return False
            if key in self._pending or now < self._retry_at.get(key, float("-inf")):
                return False
            if now < self._paused_until:
                return False
            self._pending.add(key)
            return True

    def _cached(self, key) -> ReleaseEvidence:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or not (0 <= now - entry[1] <= MAX_AGE_S):
                return ReleaseEvidence()
            self._cache.move_to_end(key)
            return entry[0]

    # -------------------------------------------------------------- worker
    def _enqueue(self, fn):
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="release-evidence",
                                                daemon=True)
                self._worker.start()
        self._queue.put_nowait(fn)

    def _run(self):
        while True:
            fn = self._queue.get()
            try:
                fn()
            except Exception:  # noqa: BLE001 - _refresh records its own failures
                pass

    def _refresh(self, key):
        try:
            evidence, plain = self._fetch(*key)
        except _Fail as exc:
            self._failed(key, exc.kind, exc.pause_s)
        except Exception as exc:  # noqa: BLE001 - never a success
            self._failed(key, f"internal error: {type(exc).__name__}")
        else:
            with self._lock:
                at = self._clock()
                if key[3] is not None:
                    # The same answer without the plan check is the entry the tick after the
                    # Replan asks for (its plan is then the target): no unconfirmed tick between.
                    plain_key = key[:3] + (None,)
                    self._cache[plain_key] = (plain, at)
                    self._cache.move_to_end(plain_key)
                    self._retry_at.pop(plain_key, None)
                self._cache[key] = (evidence, at)
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_ENTRIES:
                    self._cache.popitem(last=False)
                self._retry_at.pop(key, None)
                self._set_track(key[0], "configured", None)
                self._pending.discard(key)
            self._log(None)

    def _failed(self, key, kind, pause_s=None):
        with self._lock:
            if pause_s is not None:
                self._paused_until = max(self._paused_until, self._clock() + pause_s)
            self._retry_at[key] = self._clock() + max(FAILURE_RETRY_S, pause_s or 0)
            self._retry_at.move_to_end(key)
            while len(self._retry_at) > CACHE_ENTRIES:
                self._retry_at.popitem(last=False)
            self._set_track(key[0], "unknown", kind)
            self._pending.discard(key)
        self._log(kind)

    def _log(self, kind):
        if kind == self._logged:
            return
        self._logged = kind
        if kind is None:
            log.info("release evidence: GitHub answering again")
        else:
            log.warning("release evidence unavailable: %s", kind)

    # ------------------------------------------------------------ fetching
    def _fetch(self, track, specs, target, plan):
        token = self._token()
        if not _token_ok(token):
            raise _Fail("no GitHub token configured")
        repo, branch, _ctx = track
        commits = set()
        releases: dict = {}
        for tag, asset in specs:
            if tag not in releases:
                releases[tag] = self._release(token, repo, tag)
            commits.add(self._commit_asset(token, repo, releases[tag], asset))
        published = commits.pop() if len(commits) == 1 else None
        on_branch = self._on_main(token, repo, branch, target)
        on_main = on_branch
        if on_main and plan is not None:
            status = self._compare(token, repo, target, plan)
            if status == "ahead":            # the plan is ahead: the target is older
                on_main = False
        return (ReleaseEvidence(published_commit=published, on_main=on_main),
                ReleaseEvidence(published_commit=published, on_main=on_branch))

    def _release(self, token, repo, tag):
        data = self._json(token, f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        assets = data.get("assets") if isinstance(data, dict) else None
        if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
            raise _Fail("invalid release response")
        return assets

    def _commit_asset(self, token, repo, assets, asset):
        name = f"{asset}.commit"
        found = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
        if len(found) != 1:
            raise _Fail("release has no single commit asset")
        a = found[0]
        aid = a.get("id")
        if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0:
            raise _Fail("invalid release response")
        if a.get("url") != f"{self._api}/repos/{repo}/releases/assets/{aid}":
            raise _Fail("invalid release response")
        raw = self._asset(token, a["url"])
        text = raw.decode("ascii", "replace").strip().lower()
        if not _SHA.fullmatch(text):
            raise _Fail("commit asset is not a 40-hex commit")
        return text

    def _on_main(self, token, repo, branch, target):
        status = self._compare(token, repo, branch, target)
        return status in ("identical", "behind")

    def _compare(self, token, repo, base, head):
        """The compare status; ``behind``/``identical`` must list no commits of the head."""
        data = self._json(token, f"/repos/{repo}/compare/{urllib.parse.quote(base, safe='')}..."
                                 f"{urllib.parse.quote(head, safe='')}?per_page=1")
        if not isinstance(data, dict):
            raise _Fail("invalid compare response")
        status, ahead, behind = data.get("status"), data.get("ahead_by"), data.get("behind_by")
        if status not in _STATUSES or not all(isinstance(n, int) and not isinstance(n, bool)
                                              and n >= 0 for n in (ahead, behind)):
            raise _Fail("invalid compare response")
        if status in ("identical", "behind") and ahead != 0:
            raise _Fail("invalid compare response")
        return status

    # ---------------------------------------------------------------- HTTP
    def _json(self, token, path):
        """GET + parse. With a stored ETag the request is conditional: a 304 re-parses (and
        the caller re-validates) the body stored with that ETag; nothing else is reused."""
        url = self._api + path
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        with self._lock:
            stored = self._etags.get(url)
        if stored is not None:
            headers["If-None-Match"] = stored[0]
        status, etag, raw = self._get(url, cap=MAX_JSON, headers=headers, conditional=True)
        if status == 304:
            if stored is None:
                raise _Fail("GitHub HTTP 304")
            raw = stored[1]
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _Fail("invalid JSON from GitHub") from None
        if status == 200 and isinstance(etag, str) and 0 < len(etag) <= MAX_ETAG \
                and all(32 <= ord(c) < 127 for c in etag):
            with self._lock:
                self._etags[url] = (etag, raw)
                self._etags.move_to_end(url)
                while len(self._etags) > CACHE_ENTRIES:
                    self._etags.popitem(last=False)
        return data

    def _asset(self, token, url):
        status, location, raw = self._get(url, cap=MAX_COMMIT_FILE, redirect=True, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/octet-stream"})
        if status == 200:
            return raw
        if not download_allowed(location, self._download_hosts):
            raise _Fail("asset redirect to a host that is not allowed")
        # no Authorization: the signed download URL carries its own credential
        return self._get(location, cap=MAX_COMMIT_FILE, headers={"Accept": "application/octet-stream"})

    def _get(self, url, *, cap, headers, redirect=False, conditional=False):
        """GET with a hard deadline. ``redirect``: a 3xx returns (status, Location, b"");
        ``conditional``: returns (status, ETag, body), a 304 as (304, None, b"")."""
        result: dict = {}
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "herdr-engineering"})
        deadline = time.monotonic() + self._timeout

        def call():
            try:
                with self._opener.open(req, timeout=self._timeout) as response:
                    raw = b""
                    while len(raw) <= cap:
                        if time.monotonic() > deadline:       # an abandoned call ends too
                            result["error"] = "timeout"
                            return
                        chunk = response.read1(min(65536, cap + 1 - len(raw))) \
                            if hasattr(response, "read1") else response.read(cap + 1 - len(raw))
                        if not chunk:
                            break
                        raw += chunk
                    result["ok"] = (response.status, raw, response.headers.get("ETag"))
            except urllib.error.HTTPError as exc:
                result["http"] = (exc.code, exc.headers.get("Location") if exc.headers else None,
                                  _rate_limit_pause(exc.code, exc.headers, self._wall))
                exc.close()
            except Exception as exc:  # noqa: BLE001 - only the type is kept
                result["error"] = type(exc).__name__

        worker = threading.Thread(target=call, name="release-evidence-call", daemon=True)
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            raise _Fail("timeout")
        if "ok" in result:
            status, raw, etag = result["ok"]
            if len(raw) > cap:
                raise _Fail("GitHub response too large")
            if status != 200:
                raise _Fail(f"GitHub HTTP {int(status)}")
            if conditional:
                return 200, etag, raw
            return (200, None, raw) if redirect else raw
        if "http" in result:
            code, location, pause = result["http"]
            if pause is not None:
                raise _Fail("GitHub rate limited", pause_s=pause)
            if redirect and code in (301, 302, 303, 307, 308) and isinstance(location, str):
                return code, location, b""
            if conditional and code == 304 and "If-None-Match" in headers:
                return 304, None, b""
            raise _Fail(f"GitHub HTTP {int(code)}")
        kind = result.get("error", "error")
        raise _Fail("timeout" if kind in ("TimeoutError", "timeout") else f"GitHub unreachable: {kind}")
