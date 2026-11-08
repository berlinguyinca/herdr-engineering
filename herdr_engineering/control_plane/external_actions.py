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
import logging
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


def sink_credential(sink):
    """``credential(repo) -> token`` for ``GitHubIssues`` from a ``GitHubIssueSink`` (the App capability, else ``github.token``).
    A missing credential is a definite failure with a fixed reason; the secret is never in the exception."""
    def credential(repo: str) -> str:
        from .deployment_escalation import IssueRefused
        try:
            return sink.credential(repo)[0]
        except IssueRefused:
            raise ProviderError("failed", "no_credential") from None
    return credential


# ------------------------------------------------------------------------------------------------------------- service
_SAFE = re.compile(r"[a-z_]{1,40}\Z")


class Mismatch(Exception):
    """The caller did not confirm the content hash they were shown: HTTP 409."""

    status = 409


class ExternalActionService:
    """The approval ledger and executor for external actions. One per ``AssistantService`` (``assistant.external_actions``); the
    provider clients (``github``, ``gmail``) are wired by the control plane and absent means "not available".

    States of one action (an ``AssistantProposal``): ``status`` ``awaiting_approval`` -> ``approved`` | ``rejected`` | ``expired``;
    ``execution`` ``not_started`` -> ``executing`` -> ``executed`` | ``failed`` | ``unknown``. All of it lives in fields the
    proposal already had, so an older image that rewrites the snapshot loses nothing (and the marker on the provider side is the
    backstop after a rollback).

    Exactly once, and where that stops:

    * the key is ``sha256(kind | brief_key)``; filing the same email again returns the existing action unless it was rejected, or
      expired without ever starting (then a new decision is a new action);
    * the intent (``executing``, an attempt count, the time) is made durable with an acknowledged write BEFORE the provider is
      called; a failed write means no call;
    * every run after the first looks for the key at the provider BEFORE creating (GitHub: a listing of the issues updated since the
      first intent, the key being the last line of the body; Gmail: the newest drafts, the key being a header). Found means adopted,
      not created again;
    * a definite refusal (4xx, a revoked token) is ``failed``; anything without a usable answer (timeout, 5xx, a cut connection, a
      crash) is ``unknown``. Neither is retried by anything but the operator's own retry, which re-confirms the content hash;
    * what this CANNOT promise: a request that reached GitHub only AFTER the next run read the listing (a provider still processing a
      cut connection) can yield a second issue; a Gmail draft older than the newest 50 is not found, so a retry can add a second
      DRAFT (never a second send: nothing here sends). Both are rare and visible to the operator.
    """

    def __init__(self, assistant: Any) -> None:
        self.assistant = assistant
        self._lock = threading.RLock()
        self.github: GitHubIssues | None = None
        self.gmail: GmailDrafts | None = None

    # ------------------------------------------------------------------ gates
    @property
    def cfg(self) -> dict:
        return self.assistant.config["external_actions"]

    def enabled(self) -> bool:
        return bool(self.assistant.config.get("enabled") and self.cfg["enabled"] and not self.cfg["kill_switch"])

    def _require_enabled(self) -> None:
        from .assistant import AssistantNotFound
        if not self.enabled():
            raise AssistantNotFound("not found")                      # off means the route does not exist

    def _now(self) -> datetime:
        return self.assistant.now()

    # ------------------------------------------------------------------ lookups and views
    def _mine(self, proposal_id: Any, tenant_id: str, user_id: str):
        """Only the owner. ``actions.approvers`` is deliberately NOT honoured for these kinds: an approver could otherwise read a
        draft's recipient, or approve something that leaves the system for a person who never named them."""
        from .assistant import AssistantNotFound
        x = self.assistant.store.proposals.get(proposal_id) if isinstance(proposal_id, str) else None
        if x is None or x.kind not in EXTERNAL_KINDS or x.tenant_id != tenant_id or x.user_id != user_id:
            raise AssistantNotFound("not found")
        return x

    def _stale(self, x) -> bool:
        at = (x.correlation or {}).get("last_intent_at")
        try:
            return datetime.fromisoformat(at) + timedelta(seconds=self.cfg["intent_stale_seconds"]) <= self._now()
        except (TypeError, ValueError):
            return True

    def state_of(self, x) -> str:
        if x.status == "awaiting_approval":
            return "awaiting_approval" if x.expires_at and x.expires_at > self._now() else "expired"
        if x.status in ("rejected", "expired"):
            return x.status
        if x.execution == "executing":
            return "unknown" if self._stale(x) else "executing"
        return "approved" if x.execution == "not_started" else x.execution

    def view(self, x, *, detail: bool = True) -> dict:
        c = x.correlation or {}
        state = self.state_of(x)
        out = {"action_id": x.proposal_id, "kind": x.kind, "state": state, "status": x.status, "execution": x.execution,
               "title": x.title, "room_id": c.get("room_id"), "team_id": c.get("team_id"), "requested_by": x.requested_by,
               "decided_by": x.decided_by or None, "created_at": x.created_at.isoformat(),
               "expires_at": x.expires_at.isoformat() if x.expires_at else None, "attempts": int(c.get("attempts") or 0),
               "result": {"ref": x.result_ref} if x.result_ref else None, "sha256": x.payload_sha256,
               "failure": None}
        if state in ("failed", "unknown"):
            out["failure"] = {"outcome": state, "reason": x.execution_error or "interrupted"}
        if detail:
            out["card"] = self.card(x)
        return out

    @staticmethod
    def card(x) -> dict:
        p = x.payload
        if x.kind == ISSUE:
            return {"will": "create one GitHub issue", "repository": p["repo"], "title": p["title"], "labels": list(p["labels"]),
                    "body": p["body"]}
        return {"will": "save one Gmail DRAFT (nothing is sent; you send it yourself, later, from Gmail)", "to": list(p["to"]),
                "subject": p["subject"], "body": p["body"], "in_reply_to_thread": bool(p.get("thread_id"))}

    def _rows(self, tenant_id: str, user_id: str):
        rows = [x for x in self.assistant.store.proposals.values()
                if x.kind in EXTERNAL_KINDS and x.tenant_id == tenant_id and x.user_id == user_id]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    def list(self, *, tenant_id: str, user_id: str, state: str | None = None) -> list[dict]:
        self._require_enabled()
        out = [self.view(x) for x in self._rows(tenant_id, user_id)]
        return [v for v in out if state is None or v["state"] == state]

    def get(self, proposal_id: str, *, tenant_id: str, user_id: str) -> dict:
        self._require_enabled()
        return self.view(self._mine(proposal_id, tenant_id, user_id))

    def for_room(self, room_id: str, *, tenant_id: str, user_id: str, filed_by: str | None = None) -> list[dict]:
        """The room's actions. The owner gets the full card; a filer (``filed_by``) only their own, with no content."""
        self._require_enabled()
        rows = [x for x in self._rows(tenant_id, user_id)
                if (x.correlation or {}).get("room_id") == room_id and (filed_by is None or x.requested_by == filed_by)]
        if filed_by is None:
            return [self.view(x) for x in rows]
        keep = ("action_id", "kind", "state", "created_at", "team_id")
        return [{k: v for k, v in self.view(x, detail=False).items() if k in keep} for x in rows]

    # ------------------------------------------------------------------ filing
    def build_payload(self, kind: str, ctx: dict, *, tenant_id: str, user_id: str) -> tuple[dict, str]:
        """(payload, key) from the context the collaboration service vouched for. Everything the filer cannot choose (the
        repository, the recipient, the thread, the brief text) comes from there or from the operator's private record."""
        from .assistant import AssistantError, AssistantNotFound
        from .redact import redact_text
        key = action_key(kind, ctx["brief_key"])
        try:
            if kind == ISSUE:
                repo = ctx["repo"]
                if not isinstance(repo, str) or not REPO.match(repo):
                    raise ValueError("the team has no valid issue repository")
                title = clean_title(ctx.get("title") or ctx["default_title"])
                labels = ctx.get("labels") or []
                allowed = set(self.cfg["github"]["labels"])
                if (not isinstance(labels, list) or len(labels) > MAX_LABELS or len(set(map(str, labels))) != len(labels)
                        or not all(isinstance(x, str) and x in allowed for x in labels)):
                    raise ValueError(f"labels must come from external_actions.github.labels (up to {MAX_LABELS})")
                return {"repo": repo, "title": title, "labels": sorted(labels), "body": issue_body(ctx["brief_text"], key)}, key
            rec = self.assistant.store.team_briefs.get(ctx["reference"])
            if (rec is None or rec.tenant_id != tenant_id or rec.user_id != user_id or rec.brief_key != ctx["brief_key"]
                    or rec.suggestion_id != ctx["suggestion_id"]):
                raise AssistantNotFound("not found")
            from .google_workspace import _ADDRESS, MESSAGE_ID_HEADER, THREAD_ID
            sender = rec.private.get("sender", "")
            if not _ADDRESS.match(sender):
                raise ValueError("the original email has no usable reply address")
            subject = " ".join(_CONTROL.sub(" ", rec.private.get("subject", "")).split())
            subject = subject if subject.lower().startswith("re:") else "Re: " + subject
            text = ctx.get("body")
            if (not isinstance(text, str) or not text.strip() or len(text) > 800 or "\x00" in text
                    or _CONTROL.search(text.replace("\n", " "))):
                raise ValueError("the reply body must be 1-800 characters of plain text")
            if redact_text(text) != text:
                raise ValueError("the reply body contains something that looks like a secret")
            thread, header = rec.private.get("thread_id", ""), rec.private.get("message_id_header", "")
            return {"to": [sender], "subject": subject[:200], "body": text, "action_key": key,
                    "thread_id": thread if THREAD_ID.fullmatch(thread) else "",
                    "in_reply_to": header if MESSAGE_ID_HEADER.match(header) else ""}, key
        except ValueError as exc:
            raise AssistantError(str(exc)) from None

    def file(self, *, tenant_id: str, user_id: str, kind: str, ctx: dict, requested_by: str, assistant_id: str = "") -> dict:
        """File one action for the owner's approval. Idempotent on the key. Starts nothing and calls no provider."""
        from .assistant import AssistantError, AssistantNotFound, AssistantUnavailable, RateLimited
        self._require_enabled()
        if kind not in EXTERNAL_KINDS:
            raise AssistantError("unknown external action kind")
        svc = self.assistant
        if not assistant_id:
            assistant_id = next((a.assistant_id for a in svc.store.list_assistants(tenant_id=tenant_id, user_id=user_id)
                                 if a.enabled), "")
        if not assistant_id:
            raise AssistantNotFound("not found")
        payload, key = self.build_payload(kind, ctx, tenant_id=tenant_id, user_id=user_id)
        with self._lock:
            have = self._existing(tenant_id, user_id, kind, key)
            if have is not None:
                return {**self.view(have, detail=False), "replayed": True}
            now = self._now()
            if sum(1 for x in self._rows(tenant_id, user_id) if now - x.created_at < timedelta(hours=1)) >= self.cfg["actions_per_hour"]:
                raise RateLimited("too many external actions this hour")
            title = (f"Create a GitHub issue in {payload['repo']}: {payload['title']}" if kind == ISSUE
                     else f"Save a Gmail draft reply: {payload['subject']}")
            corr = {"room_id": ctx.get("room_id"), "team_id": ctx.get("team_id"), "suggestion_id": ctx.get("suggestion_id"),
                    "brief_key": ctx["brief_key"], "attempts": 0}
            try:
                x, created = svc.create_proposal(
                    tenant_id=tenant_id, user_id=user_id, assistant_id=assistant_id, kind=kind, title=title, payload=payload,
                    requested_by=requested_by, dedupe_key=key, external_ref=key, correlation=corr, link="#/assistant")
            except (AssistantError, KeyError):
                raise AssistantNotFound("not found") from None
            try:
                svc.store.put_proposal_required(x)
            except Exception:                                      # noqa: BLE001 - the card must be durable before it is shown
                svc.store.proposals.pop(x.proposal_id, None)
                raise AssistantUnavailable("the action could not be stored durably; nothing was filed") from None
            self._audit(x, requested_by, "filed", f"{kind} key {key[:12]}")
            self._notify(x, f"{'Issue' if kind == ISSUE else 'Reply draft'} waiting for your approval: {x.title}")
            return {**self.view(x, detail=False), "replayed": not created}

    def _existing(self, tenant_id: str, user_id: str, kind: str, key: str):
        now = self._now()
        for x in self._rows(tenant_id, user_id):
            if x.kind != kind or x.dedupe_key != key or x.status == "rejected":
                continue
            if x.status == "awaiting_approval" and not (x.expires_at and x.expires_at > now):
                continue
            if x.status == "expired" and x.execution == "not_started":
                continue
            return x
        return None

    def _notify(self, x, text: str) -> None:
        try:
            self.assistant.notify(tenant_id=x.tenant_id, user_id=x.user_id, key=f"external-action:{x.proposal_id}:{x.execution}",
                                  priority="normal", text=text[:300], link="#/assistant")
        except Exception:                                          # noqa: BLE001 - a notification is never a reason to fail
            pass

    def _audit(self, x, actor: str, what: str, detail: str = "") -> None:
        self.assistant.store.audit_record(tenant_id=x.tenant_id, user_id=x.user_id, actor=actor,
                                          action=f"assistant.external.{what}", subject_id=x.proposal_id, detail=detail[:200])

    # ------------------------------------------------------------------ deciding
    def _owner_only(self, x, actor: str, what: str) -> None:
        from .assistant import AssistantForbidden
        if actor != x.user_id:
            self._audit(x, str(actor)[:80], "denied", f"{what}: not the owner")
            raise AssistantForbidden("only the account owner decides external actions")

    def decide(self, proposal_id: str, *, tenant_id: str, user_id: str, actor: str, decision: str, expected_sha256: Any) -> dict:
        """The owner approves (and the action runs once) or rejects. Nobody else, whatever their standing elsewhere, can approve a
        thing that leaves the system for this person. Approving needs the sha256 of the content the owner was shown."""
        from .assistant import AssistantError, AssistantUnavailable
        self._require_enabled()
        x = self._mine(proposal_id, tenant_id, user_id)
        self._owner_only(x, actor, "decision")
        if decision not in ("approved", "rejected"):
            raise AssistantError("decision must be 'approved' or 'rejected'")
        with self._lock:
            if x.status != "awaiting_approval":
                if x.status == decision:
                    return {**self.view(x), "replayed": True}      # the same decision again changes nothing
                raise AssistantError(f"the action is {self.state_of(x)}")
            if not (x.expires_at and x.expires_at > self._now()):
                x.status = "expired"
                self.assistant.store.put_proposal(x)
                raise AssistantError("the action expired; it must be filed again")
            if decision == "approved" and expected_sha256 != x.payload_sha256:
                raise Mismatch("approve with the sha256 of the content you were shown")
            x.status, x.decided_by, x.decided_at = decision, actor, self._now()
            try:
                self.assistant.store.put_proposal_required(x)
            except Exception:                                      # noqa: BLE001
                x.status, x.decided_by, x.decided_at = "awaiting_approval", "", None
                raise AssistantUnavailable("the decision could not be stored durably; nothing was done") from None
            self._audit(x, actor, decision, f"{x.kind} key {x.dedupe_key[:12]}")
        if decision == "approved":
            self._run(x, actor)
        return {**self.view(x), "replayed": False}

    def retry(self, proposal_id: str, *, tenant_id: str, user_id: str, actor: str, expected_sha256: Any) -> dict:
        """The owner runs a ``failed`` or ``unknown`` (or approved but never started) action again. It looks for the key first."""
        from .assistant import AssistantError
        self._require_enabled()
        x = self._mine(proposal_id, tenant_id, user_id)
        self._owner_only(x, actor, "retry")
        if expected_sha256 != x.payload_sha256:
            raise Mismatch("retry with the sha256 of the content you were shown")
        with self._lock:
            state = self.state_of(x)
            if x.status != "approved" or state not in ("failed", "unknown", "approved"):
                raise AssistantError(f"the action is {state}; only a failed, unknown or approved-but-not-run action is retried")
            self._audit(x, actor, "retry", state)
        self._run(x, actor)
        return {**self.view(x), "replayed": False}

    # ------------------------------------------------------------------ running
    def _provider(self, x):
        p = self.github if x.kind == ISSUE else self.gmail
        if p is None:
            raise ProviderError("failed", "not_wired")
        return p

    def _find(self, x, since: str) -> dict | None:
        p = self._provider(x)
        if x.kind == ISSUE:
            return p.find(x.payload["repo"], x.dedupe_key, since)
        return p.find(x.tenant_id, x.user_id, x.dedupe_key)

    def _create(self, x) -> dict:
        p, pl = self._provider(x), x.payload
        if x.kind == ISSUE:
            return p.create(pl["repo"], pl["title"], pl["body"], pl["labels"])
        return p.create(x.tenant_id, x.user_id, pl)

    @staticmethod
    def _intent_within(x, now: datetime, span: timedelta) -> bool:
        try:
            return now - datetime.fromisoformat((x.correlation or {}).get("last_intent_at") or "") < span
        except (TypeError, ValueError):
            return False

    def _run(self, x, actor: str) -> None:
        """Claim, record the intent durably, reconcile, create, record. The claim is the only door: a concurrent or repeated call
        finds ``executing``/``executed`` and does nothing."""
        from .assistant import AssistantError, AssistantUnavailable, RateLimited, canonical_json
        svc = self.assistant
        with self._lock:
            if x.execution == "executed":
                return
            if x.execution == "executing" and not self._stale(x):
                raise AssistantError("the action is already running")
            if hashlib.sha256(canonical_json([x.kind, x.payload]).encode()).hexdigest() != x.payload_sha256:
                x.execution, x.execution_error = "failed", "payload_changed"
                svc.store.put_proposal(x)
                raise AssistantError("the content changed after approval; nothing was run")
            now = self._now()
            if sum(1 for o in self._rows(x.tenant_id, x.user_id)
                   if self._intent_within(o, now, timedelta(hours=1))) >= self.cfg["executions_per_hour"]:
                raise RateLimited("too many external actions were run this hour")
            c = dict(x.correlation or {})
            prior = int(c.get("attempts") or 0)
            before = (x.execution, x.execution_error, x.correlation)
            c.update(attempts=prior + 1, last_intent_at=now.isoformat())
            c.setdefault("intent_at", now.isoformat())
            x.correlation, x.execution, x.execution_error = c, "executing", ""
            try:
                svc.store.put_proposal_required(x)                 # the intent is durable BEFORE any provider call
                self._audit(x, actor, "executing", f"attempt {prior + 1}")
            except Exception:                                      # noqa: BLE001 - no intent on record, no call
                x.execution, x.execution_error, x.correlation = before
                try:
                    svc.store.put_proposal(x)
                except Exception:                                  # noqa: BLE001
                    pass
                raise AssistantUnavailable("the intent could not be recorded durably; nothing was sent") from None
        since = utc_iso(datetime.fromisoformat(c["intent_at"]) - SINCE_MARGIN)
        outcome, ref, how = "executed", "", "created"
        try:
            found = self._find(x, since) if prior >= 1 else None
            if found is not None:
                ref, how = found["ref"], "adopted"
            else:
                ref = self._create(x)["ref"]
        except ProviderError as exc:
            outcome, how = exc.outcome, exc.reason
        except Exception:                                          # noqa: BLE001 - our own bug is not a provider answer
            logging.getLogger(__name__).exception("external action %s crashed", x.proposal_id)
            outcome, how = "unknown", "internal_error"
        with self._lock:
            x.execution = outcome
            x.executed_by, x.executed_at = actor, self._now()
            x.result_ref, x.execution_error = (ref, "") if outcome == "executed" else ("", how if _SAFE.match(how) else "provider_error")
            try:
                svc.store.put_proposal_required(x)
            except Exception:                                      # noqa: BLE001 - the truth stays in memory; recover() heals
                logging.getLogger(__name__).error("external action %s: the result could not be stored durably", x.proposal_id)
            try:
                self._audit(x, actor, outcome if outcome != "executed" else "executed",
                            f"{how} {x.kind}" + (f" {ref}" if ref and x.kind == ISSUE else ""))
            except Exception:                                      # noqa: BLE001
                logging.getLogger(__name__).error("external action %s: the result audit could not be written", x.proposal_id)
        if outcome != "executed":
            self._notify(x, f"An approved external action did not complete ({outcome}: {x.execution_error}): {x.title}")

    # ------------------------------------------------------------------ after a crash
    def recover(self) -> dict:
        """After a restart: every action left ``executing`` is looked up at the provider. Found -> executed (adopted); not found
        or no answer -> ``unknown`` for the operator. This only READS the provider; it never creates anything."""
        out = {"adopted": 0, "unknown": 0}
        if not self.enabled():
            return out
        for x in [r for r in list(self.assistant.store.proposals.values()) if r.kind in EXTERNAL_KINDS and r.execution == "executing"]:
            try:
                since = utc_iso(datetime.fromisoformat((x.correlation or {})["intent_at"]) - SINCE_MARGIN)
                found = self._find(x, since)
            except Exception:                                      # noqa: BLE001 - ProviderError or a bad record: still not known
                found = None
            with self._lock:
                if found is not None:
                    x.execution, x.result_ref, x.execution_error = "executed", found["ref"], ""
                    out["adopted"] += 1
                else:
                    x.execution, x.execution_error = "unknown", "interrupted"
                    out["unknown"] += 1
                x.executed_by, x.executed_at = "recovery", self._now()
                self.assistant.store.put_proposal(x)
                self._audit(x, "recovery", "executed" if found else "unknown", "adopted after restart" if found else "interrupted")
        return out
