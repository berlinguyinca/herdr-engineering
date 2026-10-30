"""Open pull requests for project intelligence's outbound poller, read through the shared GitHub credential.

``reconcile_open_prs`` only needs ``open_prs(repo)``. This provides it from ``github_auth.credentials()``, so the poller
uses the same GitHub App (or static token) as the GitHub cache and the Deployments evidence and needs no review gate.
Read only: a repo-scoped token for ``pull_requests: read``. A full page at the page cap is an error, never a silent cut.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .. import github_auth

log = logging.getLogger(__name__)

PER_PAGE = 100
MAX_PAGES = 5
MAX_RESPONSE = 4 * 1024 * 1024
TIMEOUT_S = 10.0


@dataclass(frozen=True)
class OpenPr:
    number: int
    head_sha: str
    base_ref: str
    author_login: str


class PollError(RuntimeError):
    """A fixed reason string only (``permission``, ``unknown_owner``, ``not_configured``, ...): never a credential."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SharedCredentialPrLister:
    def __init__(self, *, credentials=None, api=github_auth.API, opener=None, timeout=TIMEOUT_S):
        github_auth._check_api(api)
        self._credentials = credentials or github_auth.credentials
        self._api = api.rstrip("/")
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), github_auth._NoRedirect())
        self._timeout = timeout
        self._lock = threading.Lock()
        self.last_error: str | None = None          # fixed reason of the latest failed repo, None after a clean pass

    def open_prs(self, repo: str) -> list[OpenPr]:
        owner = github_auth.owner_of(repo)
        name = repo.split("/", 1)[1] if isinstance(repo, str) and "/" in repo else None
        if owner is None or not name or not github_auth._REPO_NAME.fullmatch(name):
            raise self._fail("invalid_repository")
        provider = self._credentials()
        try:
            token = provider.token_for(owner, github_auth.PULLS_PERMISSIONS, [name])
        except github_auth.GitHubAuthError as exc:
            raise self._fail(exc.reason) from None
        if not token:
            raise self._fail(getattr(provider, "reason", None) or "not_configured")
        out: list[OpenPr] = []
        slug = f"{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
        for page in range(1, MAX_PAGES + 1):
            data = self._get(f"/repos/{slug}/pulls?state=open&per_page={PER_PAGE}&page={page}", token)
            if not isinstance(data, list):
                raise self._fail("invalid_response")
            try:
                out.extend(OpenPr(int(i["number"]), str(i["head"]["sha"]), str(i["base"]["ref"]),
                                  str((i.get("user") or {}).get("login", ""))) for i in data)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise self._fail("invalid_response") from None
            if len(data) < PER_PAGE:
                with self._lock:
                    self.last_error = None
                return out
        raise self._fail("too_many_pull_requests")      # a full last page: never truncate silently

    def _fail(self, reason) -> PollError:
        with self._lock:
            self.last_error = reason
        return PollError(reason)

    def _get(self, path, token):
        request = urllib.request.Request(
            self._api + path, method="GET",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "herdr-engineering"})
        token = None  # noqa: F841
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise self._fail({401: "authentication", 403: "permission", 404: "not_found"}.get(code, "unavailable")) from None
        except Exception:  # noqa: BLE001 - only a fixed kind is kept
            raise self._fail("unavailable") from None
        finally:
            request = None  # noqa: F841
        if len(raw) > MAX_RESPONSE:
            raise self._fail("invalid_response")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise self._fail("invalid_response") from None
