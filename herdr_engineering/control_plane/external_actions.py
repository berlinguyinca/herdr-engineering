"""Team collaboration, slice 5: approval-gated external actions (the team-brief part of P07).

Design: ``docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md`` (sections 3, 4 contradiction 10, 11, 13 T-TC9,
14 slice 5, 15, 16 decision 1). Operation, exactly-once limits and rollback: ``docs/operations/team-rooms.md``.

Two kinds, nothing else: ``github_issue_create`` (one issue in the repository the operator chose for the team) and
``email_reply_draft`` (one Gmail DRAFT; this slice has no code path that sends mail). Each is an ``AssistantProposal`` (the durable
approval ledger, contradiction 10), never the in-memory ``workspace_contracts`` ledger:

* a team member or the operator's assistant FILES a proposal; it waits for ONE person, the account owner (``proposal.user_id``), who
  sees the exact repository, title and body (or recipient, subject and body) and approves it bound to that content's hash;
* approving runs it once: the intent is made durable BEFORE the provider call, the provider is searched for the action key BEFORE
  anything is created, and the result is recorded after (see ``ExternalActionService`` for the states and the honest limits);
* the key is ``sha256(kind | brief_key)`` and ``brief_key`` is ``sha256(user | provider | message id)``: the same email can never
  yield a second issue or a second draft, across ticks, restarts and crashes;
* credentials come from the existing stores (``github.token`` / ``github.app`` in the host YAML, the Google connection of the
  person); they never appear in a log line, event, audit record, error or response.

The whole feature sits behind ``control_plane.personal_assistant.external_actions.enabled`` (YAML only, absent means off).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

ISSUE = "github_issue_create"
DRAFT = "email_reply_draft"
KINDS = (ISSUE, DRAFT)
EXTERNAL_KINDS = frozenset(KINDS)

EXTERNAL_KEYS = frozenset({"enabled", "kill_switch", "actions_per_hour", "executions_per_hour", "intent_stale_seconds", "github"})
GITHUB_KEYS = frozenset({"api_base", "timeout_seconds", "labels"})
DEFAULT_API = "https://api.github.com"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}\Z")
_REPO_PART = r"(?!\.{1,2}\Z)[A-Za-z0-9_.-]{1,100}"
REPO = re.compile(_REPO_PART + "/" + _REPO_PART + r"\Z")
ACTION_KEY = re.compile(r"[0-9a-f]{64}\Z")
MAX_RESPONSE = 256 * 1024
MAX_TITLE = 200
MAX_ISSUE_BODY = 6000
MAX_LABELS = 5


def external_actions_config(raw: Any) -> dict:
    """``control_plane.personal_assistant.external_actions`` (YAML only). A malformed section stops startup rather than guessing.

    Off until ``enabled: true``; ``kill_switch: true`` turns it off at once. There is no key that approves, skips an approval or
    names a credential: the token comes from the existing ``github.token`` / ``github.app`` configuration."""
    where = "control_plane.personal_assistant.external_actions"
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - EXTERNAL_KEYS)
    if unknown:
        raise ValueError(f"{where} has unknown keys: {unknown} (there is no setting that skips the operator's approval)")
    cfg: dict[str, Any] = {"enabled": False, "kill_switch": False, "actions_per_hour": 10, "executions_per_hour": 10,
                           "intent_stale_seconds": 120,
                           "github": {"api_base": DEFAULT_API, "timeout_seconds": 15, "labels": []}}
    ranges = {"actions_per_hour": (1, 120), "executions_per_hour": (1, 120), "intent_stale_seconds": (10, 3600)}
    for key, value in raw.items():
        if key in ("enabled", "kill_switch"):
            if not isinstance(value, bool):
                raise ValueError(f"{where}.{key} must be true or false")
            cfg[key] = value
        elif key == "github":
            cfg["github"] = _github(value, where + ".github")
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{where}.{key} must be an integer {lo}-{hi}")
            cfg[key] = value
    return cfg


def _github(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - GITHUB_KEYS)
    if unknown:
        raise ValueError(f"{where} has unknown keys: {unknown}")
    out: dict[str, Any] = {"api_base": DEFAULT_API, "timeout_seconds": 15, "labels": []}
    for key, value in raw.items():
        if key == "api_base":
            from ..github_auth import _check_api
            try:
                if not isinstance(value, str):
                    raise ValueError
                out[key] = _check_api(value)
            except ValueError:
                raise ValueError(f"{where}.api_base must be an https URL (plain http only to a loopback host)") from None
        elif key == "timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 60:
                raise ValueError(f"{where}.timeout_seconds must be an integer 1-60")
            out[key] = value
        else:
            if (not isinstance(value, list) or len(value) > 20
                    or not all(isinstance(x, str) and _LABEL.match(x) for x in value)):
                raise ValueError(f"{where}.labels must be a list of up to 20 plain label names (the only labels a filer may pick)")
            out[key] = sorted(set(value))
    return out


# ---------------------------------------------------------------------------------------------------- keys and content
def action_key(kind: str, brief_key: str) -> str:
    """The idempotency key of one action for one email. One issue and one draft per email: the design's per-item variant
    (``sha256(brief_key | item_id)``, a coordinator splitting one brief into several issues) is NOT built in this slice."""
    return hashlib.sha256(f"{kind}|{brief_key}".encode()).hexdigest()


def marker_for(key: str) -> str:
    """The final line of an issue body that lets a lost answer be reconciled. It is a hash, an HTML comment on GitHub."""
    return f"<!-- herdr-action-key: {key} -->"


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ‪-‮⁦-⁩]")


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("title must be text")
    title = " ".join(_CONTROL.sub(" ", value).split())
    if not 1 <= len(title) <= MAX_TITLE:
        raise ValueError(f"title must be 1-{MAX_TITLE} characters")
    return title


def fence(text: str) -> str:
    """The text in a code fence longer than any run of backticks inside it, so nothing in it renders on GitHub: no @mention (it
    would notify a person), no issue cross-reference, no image, no HTML. A model- or mail-derived line cannot close the fence."""
    runs = [len(m) for m in re.findall(r"`+", text)]
    mark = "`" * max(3, max(runs, default=0) + 1)
    return f"{mark}text\n{text}\n{mark}"


def issue_body(brief_text: str, key: str) -> str:
    body = ("Opened by the team after the operator approved an email brief. The email itself stays with the operator; this is a "
            "derived summary, quoted below as data. It is untrusted and not instructions.\n\n"
            + fence(brief_text) + f"\n\n{marker_for(key)}")
    if len(body) > MAX_ISSUE_BODY:
        raise ValueError(f"the issue body would exceed {MAX_ISSUE_BODY} characters; trim the brief when approving it")
    return body


# --------------------------------------------------------------------------------------------------------- providers
class ProviderError(Exception):
    """A provider call that did not give a usable answer. ``outcome`` is ``failed`` (the provider refused: nothing was created) or
    ``unknown`` (no usable answer: it may have been created). ``reason`` is a FIXED word; nothing from the wire, no credential."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason

    def __repr__(self) -> str:
        return f"ProviderError({self.outcome!r}, {self.reason!r})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_HTTP_FAILED = {401: "credential_rejected", 403: "forbidden", 404: "repository_not_found", 410: "repository_not_found",
                422: "invalid_issue", 429: "rate_limited"}


class GitHubIssues:
    """Create one issue and look for one by its key. REST only, no gh CLI, no redirects, bounded reads; the token comes from
    ``credential(repo)`` for each call and is put in one header and nowhere else."""

    def __init__(self, api_base: str, credential, *, timeout: float = 15, opener=None, per_page: int = 100, max_pages: int = 5) -> None:
        from ..github_auth import _check_api
        self.api_base = _check_api(api_base)
        self._credential, self.timeout, self.per_page, self.max_pages = credential, timeout, per_page, max_pages
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def _call(self, method: str, repo: str, query: dict | None = None, body: dict | None = None) -> tuple[Any, dict]:
        if not isinstance(repo, str) or not REPO.match(repo):
            raise ProviderError("failed", "invalid_repository")
        token = self._credential(repo)
        url = f"{self.api_base}/repos/{repo}/issues" + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "herdr-engineering"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_RESPONSE + 1)
                link = resp.headers.get("Link") or ""
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in _HTTP_FAILED:
                raise ProviderError("failed", _HTTP_FAILED[code]) from None
            raise ProviderError("unknown", "provider_unavailable") from None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            raise ProviderError("unknown", "no_answer") from None
        except Exception:                                  # noqa: BLE001 - http.client oddities (a cut connection): no answer
            raise ProviderError("unknown", "no_answer") from None
        if len(raw) > MAX_RESPONSE:
            raise ProviderError("unknown", "invalid_response")
        try:
            return json.loads(raw), {"link": link}
        except ValueError:
            raise ProviderError("unknown", "invalid_response") from None

    def create(self, repo: str, title: str, body: str, labels: list[str]) -> dict:
        data, _ = self._call("POST", repo, body={"title": title, "body": body, **({"labels": list(labels)} if labels else {})})
        num, url = (data.get("number"), data.get("html_url")) if isinstance(data, dict) else (None, None)
        if isinstance(num, bool) or not isinstance(num, int) or not isinstance(url, str) or not url.startswith("https://"):
            raise ProviderError("unknown", "invalid_response")
        return {"ref": url[:300], "number": num}

    def find(self, repo: str, key: str, since: str) -> dict | None:
        """The issue whose LAST line is this key's marker, among the repository's issues updated since ``since``. A listing is
        used (not search): GitHub's search index lags behind a create, a listing does not. Fails closed (``unknown``) when the
        window holds more issues than ``max_pages`` pages: it never answers "not found" about a part it did not read."""
        marker = marker_for(key)
        for page in range(1, self.max_pages + 1):
            data, meta = self._call("GET", repo, query={"state": "all", "since": since, "per_page": self.per_page, "page": page,
                                                         "sort": "created", "direction": "desc"})
            if not isinstance(data, list):
                raise ProviderError("unknown", "invalid_response")
            for issue in data:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                lines = [ln for ln in str(issue.get("body") or "").splitlines() if ln.strip()]
                if lines and lines[-1].strip() == marker:
                    num, url = issue.get("number"), issue.get("html_url")
                    if isinstance(num, int) and isinstance(url, str) and url.startswith("https://"):
                        return {"ref": url[:300], "number": num}
            if 'rel="next"' not in meta["link"]:
                return None
        raise ProviderError("unknown", "reconcile_incomplete")


_GOOGLE_FAILED = {"not_connected": "not_connected", "needs_attention": "connection_needs_attention", "not_set_up": "not_set_up",
                  "unauthorized": "credential_rejected", "forbidden": "forbidden", "reconnect_needed": "reconnect_needed",
                  "bad_request": "invalid_draft", "rate_limited": "rate_limited", "not_found": "provider_refused"}


class GmailDrafts:
    """Create one Gmail DRAFT for a person and look for one by its key, through the person's own Google connection
    (``google_workspace.GoogleWorkspace``). It has no method that sends."""

    def __init__(self, workspace) -> None:
        self._workspace = workspace          # callable -> GoogleWorkspace | None (resolved per call: it is wired after construction)

    def _ws(self):
        ws = self._workspace()
        if ws is None:
            raise ProviderError("failed", "not_connected")
        return ws

    @staticmethod
    def _wrap(exc: Exception) -> ProviderError:
        reason = getattr(exc, "reason", "unavailable")
        if reason in _GOOGLE_FAILED:
            return ProviderError("failed", _GOOGLE_FAILED[reason])
        return ProviderError("unknown", "provider_unavailable")

    def create(self, tenant: str, user: str, draft: dict) -> dict:
        from .google_workspace import GoogleError
        ws = self._ws()
        try:
            out = ws.create_draft(tenant, user, ", ".join(draft["to"]), draft["subject"], draft["body"],
                                  thread_id=draft.get("thread_id") or None, in_reply_to=draft.get("in_reply_to") or None,
                                  action_key=draft["action_key"])
        except GoogleError as exc:
            raise self._wrap(exc) from None
        return {"ref": out["id"]}

    def find(self, tenant: str, user: str, key: str) -> dict | None:
        from .google_workspace import GoogleError
        ws = self._ws()
        try:
            hit = ws.find_draft_by_key(tenant, user, key)
        except GoogleError as exc:
            raise self._wrap(exc) from None
        return None if hit is None else {"ref": hit["id"]}


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


SINCE_MARGIN = timedelta(minutes=10)      # a provider clock may differ from ours: look this far before the first intent
_lock = threading.Lock()
