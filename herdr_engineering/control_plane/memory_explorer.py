"""Read-only Memory Explorer API (``/api/v1/memory/...``), kept out of api.py.

The control plane never talks to Hindsight and holds no memory store. Everything comes from the canonical
``herdr-memory`` CLI (CONTRACT.md v1, one JSON object on stdout) through the single :class:`MemoryAdapter` below.

Fail-closed rules:

* Off unless ``control_plane.memory.enabled``; disabled answers 404.
* The viewer is the authenticated API-key principal mapped in YAML
  (``control_plane.memory.viewers: {"key:<digest>": {projects: [..] | "*", scopes: ["global", "infra"]}}``). An
  unmapped principal, or a role other than viewer/operator, is refused (403). Nothing here consults a request header.
* Every returned memory is authorized by its *bank* on the server, whatever the CLI returned. A memory in a bank the
  viewer may not read is dropped from searches and answers 404 on detail, exactly like a missing id.
* Personal/assistant (Rakazo) banks are never reachable, even for a ``"*"`` viewer.
* Audit events carry metadata only (action, actor, counts, scopes, memory id); never query or memory text.
* If the CLI is missing, slow or answers something that is not contract v1, the answer is ``503 unavailable``;
  there is no sample or cached data.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess

from .redact import redact_text

log = logging.getLogger(__name__)

READ_ROLES = frozenset({"viewer", "operator"})
CONTRACT = 1
MAX_QUERY = 500
MAX_LIMIT = 50
MAX_OFFSET = 500
MAX_PROJECT_CALLS = 8
RECALL_CAP = 200
STATUSES = ("candidate", "verified", "superseded", "retracted")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MISSION_SCOPE = re.compile(r"^mission:[a-z0-9][a-z0-9_-]{0,63}$")
_DENIED_PREFIXES = ("rakazo", "personal", "assistant")   # private assistant memory is never an engineering bank
PROVENANCE_KEYS = ("source_system", "actor_id", "harness_id", "host_id", "repo_id", "mission_id", "session_id",
                   "execution_id", "commit_sha", "pr_ref")


class MemoryUnavailable(Exception):
    """The memory service cannot answer (no CLI, timeout, bad output). Carries no detail for the client."""


class MemoryNotFound(Exception):
    """The CLI says the memory does not exist."""


class MemoryAdapter:
    """The one place that runs ``herdr-memory``. ``runner(argv, timeout) -> (returncode, stdout)`` is injectable."""

    def __init__(self, command=("herdr-memory",), runner=None, timeout=8.0):
        self.command, self.timeout = tuple(command), timeout
        self._runner = runner or self._subprocess

    @staticmethod
    def _subprocess(argv, timeout):
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)  # noqa: S603
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def _call(self, *args):
        try:
            code, out = self._runner([*self.command, *args], self.timeout)
        except (OSError, subprocess.SubprocessError):
            raise MemoryUnavailable("herdr-memory could not be run") from None
        try:
            data = json.loads(out)
        except ValueError:
            raise MemoryUnavailable("herdr-memory answered something that is not JSON") from None
        if not isinstance(data, dict):
            raise MemoryUnavailable("unexpected herdr-memory output")
        if code != 0:
            if str(data.get("error") or data.get("code") or "").lower().replace(" ", "_") in {"not_found", "notfound"}:
                raise MemoryNotFound()
            raise MemoryUnavailable("herdr-memory reported an error")
        return data

    def recall(self, query, *, scope=None, project=None, max_results=RECALL_CAP):
        args = ["recall", query, "--max-results", str(int(max_results))]
        if scope:
            args += ["--scope", scope]
        if project:
            args += ["--project", project]
        data = self._call(*args)
        if not isinstance(data.get("results"), list):
            raise MemoryUnavailable("herdr-memory recall is not contract v1")
        return data

    def get(self, memory_id):
        return self._call("get", memory_id)

    def status(self):
        data = self._call("status")
        if data.get("contract") not in (None, CONTRACT):
            raise MemoryUnavailable("herdr-memory contract version is not supported")
        return data


class Viewer:
    """Which banks a principal may read. Deny by default: an empty viewer reads nothing."""

    def __init__(self, projects, scopes):
        self.all_projects = projects == "*"
        self.projects = frozenset(projects) if isinstance(projects, list) else frozenset()
        self.scopes = frozenset(s for s in scopes if s in ("global", "infra"))

    @classmethod
    def from_entry(cls, entry):
        if not isinstance(entry, dict):
            return None
        projects, scopes = entry.get("projects", []), entry.get("scopes", [])
        if not (projects == "*" or (isinstance(projects, list) and all(isinstance(p, str) and _PROJECT.match(p) for p in projects))):
            return None
        if not (isinstance(scopes, list) and all(isinstance(s, str) for s in scopes)):
            return None
        return cls(projects, scopes)

    def may_read_project(self, project):
        if not isinstance(project, str) or not _PROJECT.match(project) or project.lower().startswith(_DENIED_PREFIXES):
            return False
        return self.all_projects or project in self.projects

    def bank_scope(self, bank_id):
        """``(scope, project)`` for a readable bank, else None (the only authority on what a memory belongs to)."""
        if not isinstance(bank_id, str):
            return None
        if bank_id == "herdr-global":
            return ("global", None) if "global" in self.scopes else None
        if bank_id == "herdr-infra":
            return ("infra", None) if "infra" in self.scopes else None
        for suffix in ("-facts", "-summaries"):
            if bank_id.endswith(suffix) and self.may_read_project(bank_id[:-len(suffix)]):
                return ("project", bank_id[:-len(suffix)])
        if "-mission-" in bank_id:
            # try every split, so a project name that itself contains "-mission-" still resolves exactly
            for m in re.finditer("-mission-", bank_id):
                project, mission = bank_id[:m.start()], bank_id[m.end():]
                if _SLUG.match(mission) and self.may_read_project(project):
                    return (f"mission:{mission}", project)
        return None

    @property
    def empty(self):
        return not (self.all_projects or self.projects or self.scopes)


def _str(value, limit=300):
    return value[:limit] if isinstance(value, str) else None


def clean_memory(raw, viewer, *, detail=False):
    """Whitelist one CLI record for the browser, or None when its bank is not readable. Text is credential-redacted
    and labelled untrusted; provenance values are never inferred and only the contract keys are passed on."""
    if not isinstance(raw, dict):
        return None
    scope = viewer.bank_scope(raw.get("bank_id"))
    memory_id = raw.get("memory_id")
    if scope is None or not isinstance(memory_id, str) or not _ID.match(memory_id):
        return None
    prov_in = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    prov = {k: _str(prov_in.get(k), 200) for k in PROVENANCE_KEYS if _str(prov_in.get(k), 200)}
    refs = prov_in.get("evidence_refs")
    if isinstance(refs, list):
        prov["evidence_refs"] = [r[:300] for r in refs if isinstance(r, str)][:50]
    status = raw.get("status") if raw.get("status") in STATUSES else "unknown"
    out = {"memory_id": memory_id, "bank_id": raw["bank_id"], "scope": scope[0], "project": scope[1],
           "revision": raw["revision"] if isinstance(raw.get("revision"), int) and not isinstance(raw.get("revision"), bool) else None,
           "kind": _str(raw.get("kind"), 40), "status": status, "provenance": prov, "untrusted": True,
           "text": redact_text(_str(raw.get("text"), 20000 if detail else 2000) or "")}
    if isinstance(raw.get("score"), (int, float)) and not isinstance(raw.get("score"), bool):
        out["score"] = raw["score"]
    for key in ("created_at", "updated_at"):
        if _str(raw.get(key), 40):
            out[key] = raw[key][:40]
    if detail:
        revs = raw.get("revisions")
        out["revisions"] = [{"revision": r["revision"], "status": r.get("status") if r.get("status") in STATUSES else "unknown",
                             "at": _str(r.get("at") or r.get("created_at"), 40), "text": redact_text(_str(r.get("text"), 20000) or ""),
                             "evidence_refs": [e[:300] for e in (r.get("evidence_refs") or []) if isinstance(e, str)][:50]}
                            for r in revs[:100] if isinstance(r, dict) and isinstance(r.get("revision"), int)] if isinstance(revs, list) else None
    return out


class MemoryExplorer:
    def __init__(self, repo, config_fn=lambda: None, adapter=None):
        self._repo, self._config, self._adapter = repo, config_fn, adapter

    def _cfg(self):
        raw = self._config()
        return raw if isinstance(raw, dict) else {}

    def enabled(self):
        return bool(self._cfg().get("enabled"))

    def adapter(self):
        if self._adapter is not None:
            return self._adapter
        command = self._cfg().get("command")
        ok = isinstance(command, list) and command and all(isinstance(c, str) for c in command)
        return MemoryAdapter(command=command if ok else ("herdr-memory",))

    def viewer_for(self, principal, role):
        if role not in READ_ROLES:
            return None
        viewers = self._cfg().get("viewers")
        entry = viewers.get(principal.get("id")) if isinstance(viewers, dict) and principal.get("id") else None
        viewer = Viewer.from_entry(entry)
        return None if viewer is None or viewer.empty else viewer

    def _audit(self, principal, action, outcome, **meta):
        """Metadata only. Never the query, memory text or a provenance value."""
        try:
            self._repo.record_event("memory_explorer", "reads", "MemoryExplorerRead",
                                    {"action": action, "outcome": outcome, "actor": principal.get("id"), **meta},
                                    publish=False, feed=False)
        except Exception:  # noqa: BLE001 - reads stay available; the failure is counted in the log by class only
            log.warning("memory explorer audit event could not be recorded")

    def get(self, parts, query, principal, role):
        """``parts`` are the segments after ``/api/v1/memory``. Returns ``(status, body)``."""
        if not self.enabled():
            return 404, {"error": "not found"}
        viewer = self.viewer_for(principal, role)
        if viewer is None:
            self._audit(principal, "-".join(parts[:1]) or "root", "forbidden")
            return 403, {"error": "forbidden"}
        try:
            if parts == ["status"]:
                return self._status(principal)
            if parts == ["search"]:
                return self._search(query, principal, viewer)
            if len(parts) == 1 and _ID.match(parts[0]):
                return self._detail(parts[0], principal, viewer)
        except MemoryUnavailable:
            self._audit(principal, parts[0] if parts else "root", "unavailable")
            return 503, {"error": "memory service unavailable", "state": "unavailable"}
        return 404, {"error": "not found"}

    def _status(self, principal):
        data = self.adapter().status()
        out = {"state": "available", "healthy": data.get("healthy") if isinstance(data.get("healthy"), bool) else None,
               "contract": data.get("contract"), "server_version": _str(data.get("server_version"), 60)}
        for key in ("outbox_pending", "dead_letter"):
            out[key] = data[key] if isinstance(data.get(key), int) and not isinstance(data.get(key), bool) else None
        # CONTRACT v1 status reports the service only. Per harness/host adapter state is not reported by anything the
        # control plane has, so it is stated as unknown instead of guessed.
        out["integrations"] = {"state": "unknown", "rows": [],
                               "reason": "herdr-memory contract v1 does not report per-harness or per-host installation"}
        self._audit(principal, "status", "ok")
        return 200, out

    def _detail(self, memory_id, principal, viewer):
        try:
            raw = self.adapter().get(memory_id)
        except MemoryNotFound:
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("memory"), dict):
            raw = {**raw["memory"], "revisions": raw.get("revisions", raw["memory"].get("revisions"))}
        memory = clean_memory(raw, viewer, detail=True)
        if memory is None or memory["memory_id"] != memory_id:   # unreadable and missing look the same
            self._audit(principal, "detail", "not_found", memory_id=memory_id)
            return 404, {"error": "not found"}
        self._audit(principal, "detail", "ok", memory_id=memory_id, scope=memory["scope"])
        return 200, {"memory": memory, "retrieval_history": {
            "state": "unknown", "events": [], "reason": "herdr-memory contract v1 does not expose retrieval events"}}

    def _plan(self, query, viewer):
        """(scopes to ask, project filter) or an error string. A filter the viewer may not use is a refusal."""
        project = query.get("project") or None
        if project and not viewer.may_read_project(project):
            return "forbidden"
        raw_scopes = [s for s in (query.get("scope") or "").split(",") if s]
        for s in raw_scopes:
            if s not in ("project", "global", "infra") and not _MISSION_SCOPE.match(s):
                return "bad scope"
            if s in ("global", "infra") and s not in viewer.scopes:
                return "forbidden"
            if (s == "project" or s.startswith("mission:")) and not (viewer.all_projects or viewer.projects):
                return "forbidden"
        scopes = raw_scopes or [s for s in ("project", "global", "infra")
                                if s in viewer.scopes or (s == "project" and (viewer.all_projects or viewer.projects))]
        return scopes, project

    def _search(self, query, principal, viewer):
        q = (query.get("q") or "").strip()
        if not q or len(q) > MAX_QUERY:
            return 400, {"error": "q is required (1-500 characters)"}
        try:
            limit = min(MAX_LIMIT, max(1, int(query.get("limit") or 20)))
            offset = min(MAX_OFFSET, max(0, int(query.get("offset") or 0)))
        except ValueError:
            return 400, {"error": "limit and offset must be integers"}
        plan = self._plan(query, viewer)
        if plan == "bad scope":
            return 400, {"error": "unknown scope"}
        if plan == "forbidden":
            self._audit(principal, "search", "forbidden")
            return 403, {"error": "forbidden"}
        scopes, project = plan
        kind, status = query.get("kind") or None, query.get("status") or None
        if status and status not in STATUSES:
            return 400, {"error": "unknown status"}
        calls = []
        for scope in scopes:
            if scope == "project" and not project and not viewer.all_projects:
                calls += [(scope, p) for p in sorted(viewer.projects)[:MAX_PROJECT_CALLS]]
            else:
                calls.append((scope, project if scope != "global" and scope != "infra" else None))
        adapter, merged, failures, degraded, offline = self.adapter(), {}, 0, False, False
        for scope, proj in calls:
            try:
                data = adapter.recall(q, scope=scope, project=proj, max_results=min(RECALL_CAP, offset + limit + 1))
            except MemoryUnavailable:
                failures += 1
                continue
            degraded = degraded or bool(data.get("degraded"))
            offline = offline or bool(data.get("offline"))
            for raw in data["results"]:
                item = clean_memory(raw, viewer)
                if item is not None and item["memory_id"] not in merged:
                    merged[item["memory_id"]] = item
        if calls and failures == len(calls):
            raise MemoryUnavailable("every recall failed")
        items = [i for i in merged.values()
                 if (not project or i["project"] == project) and (not kind or i["kind"] == kind)
                 and (not status or i["status"] == status)
                 and (not query.get("harness") or i["provenance"].get("harness_id") == query["harness"])
                 and (not query.get("host") or i["provenance"].get("host_id") == query["host"])]
        if items and all("score" in i for i in items):
            items.sort(key=lambda i: -i["score"])
        page = items[offset:offset + limit]
        self._audit(principal, "search", "degraded" if (degraded or failures) else "ok", result_count=len(page),
                    scopes=sorted(scopes), query_chars=len(q))
        return 200, {"results": page, "offset": offset, "limit": limit, "has_more": len(items) > offset + limit,
                     "degraded": bool(degraded or failures), "offline": offline, "untrusted": True,
                     "note": ("A status filter of superseded or retracted finds nothing: herdr-memory recall excludes them."
                              if status in ("superseded", "retracted") else None)}
