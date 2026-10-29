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
* Every HTTP call has a hard wall-clock bound (``CALL_TIMEOUT_S``): it runs in a daemon
  thread and is abandoned at the deadline. Responses are size-capped, JSON is validated,
  commits must be 40 hex. Redirects are never followed by urllib; the asset's one
  redirect is followed by hand, only to an allowed download host, without credentials.
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

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_NAME = re.compile(r"[A-Za-z0-9_.+-]{1,128}")
_STATUSES = {"identical", "behind", "ahead", "diverged"}


class _Fail(Exception):
    """A refresh failure; ``kind`` is a fixed, token-free description."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


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
                 clock=time.monotonic, executor=None, timeout=CALL_TIMEOUT_S):
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
        """{state: "configured" | "unknown", error: fixed kind | None} of the last refresh."""
        with self._lock:
            return dict(self._state)

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
        return (track, specs, target, plan)

    def _due(self, key) -> bool:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and now - entry[1] < SUCCESS_TTL_S:
                return False
            if key in self._pending or now < self._retry_at.get(key, float("-inf")):
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
            evidence = self._fetch(*key)
        except _Fail as exc:
            self._failed(key, exc.kind)
        except Exception as exc:  # noqa: BLE001 - never a success
            self._failed(key, f"internal error: {type(exc).__name__}")
        else:
            with self._lock:
                self._cache[key] = (evidence, self._clock())
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_ENTRIES:
                    self._cache.popitem(last=False)
                self._retry_at.pop(key, None)
                self._state = {"state": "configured", "error": None}
                self._pending.discard(key)
            self._log(None)

    def _failed(self, key, kind):
        with self._lock:
            self._retry_at[key] = self._clock() + FAILURE_RETRY_S
            self._retry_at.move_to_end(key)
            while len(self._retry_at) > CACHE_ENTRIES:
                self._retry_at.popitem(last=False)
            self._state = {"state": "unknown", "error": kind}
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
        on_main = self._on_main(token, repo, branch, target)
        if on_main and plan is not None and not same_commit(plan, target):
            status = self._compare(token, repo, target, plan)
            if status == "ahead":            # the plan is ahead: the target is older
                on_main = False
        return ReleaseEvidence(published_commit=published, on_main=on_main)

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
        raw = self._get(self._api + path, cap=MAX_JSON, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _Fail("invalid JSON from GitHub") from None

    def _asset(self, token, url):
        status, location, raw = self._get(url, cap=MAX_COMMIT_FILE, redirect=True, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/octet-stream"})
        if status == 200:
            return raw
        parts = urllib.parse.urlsplit(location or "")
        host = parts.hostname or ""
        allowed = (parts.scheme == "https" and host.endswith(DOWNLOAD_SUFFIX)) or (
            host in self._download_hosts and (parts.scheme == "https" or _loopback(host)))
        if not allowed:
            raise _Fail("asset redirect to a host that is not allowed")
        # no Authorization: the signed download URL carries its own credential
        return self._get(location, cap=MAX_COMMIT_FILE, headers={"Accept": "application/octet-stream"})

    def _get(self, url, *, cap, headers, redirect=False):
        """GET with a hard deadline. ``redirect``: a 3xx returns (status, Location, b"")."""
        result: dict = {}
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "herdr-engineering"})

        def call():
            try:
                with self._opener.open(req, timeout=self._timeout) as response:
                    raw = b""
                    while len(raw) <= cap:
                        chunk = response.read1(min(65536, cap + 1 - len(raw))) \
                            if hasattr(response, "read1") else response.read(cap + 1 - len(raw))
                        if not chunk:
                            break
                        raw += chunk
                    result["ok"] = (response.status, raw)
            except urllib.error.HTTPError as exc:
                result["http"] = (exc.code, exc.headers.get("Location") if exc.headers else None)
                exc.close()
            except Exception as exc:  # noqa: BLE001 - only the type is kept
                result["error"] = type(exc).__name__

        worker = threading.Thread(target=call, name="release-evidence-call", daemon=True)
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            raise _Fail("timeout")
        if "ok" in result:
            status, raw = result["ok"]
            if len(raw) > cap:
                raise _Fail("GitHub response too large")
            if status != 200:
                raise _Fail(f"GitHub HTTP {int(status)}")
            return (200, None, raw) if redirect else raw
        if "http" in result:
            code, location = result["http"]
            if redirect and code in (301, 302, 303, 307, 308) and isinstance(location, str):
                return code, location, b""
            raise _Fail(f"GitHub HTTP {int(code)}")
        kind = result.get("error", "error")
        raise _Fail("timeout" if kind in ("TimeoutError", "timeout") else f"GitHub unreachable: {kind}")
