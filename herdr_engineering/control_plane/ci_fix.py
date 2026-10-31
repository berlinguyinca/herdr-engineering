"""Sandboxed CI fix agent: request, state machine and result record.

The control plane never starts a process. It validates a click, records ``CIFixRequested`` and lets a
host-side runner (``deploy/control-plane/ci-fix-runner.py``) claim it once and report every transition
back. State is the fold of the ``ci_fix`` event stream, so the dashboard and a restarted runner agree
and every transition is a durable event. Everything stored is bounded, redacted data: ids, the exact
commit, a branch name under ``herdr-ci-fix/`` and a short summary, never a credential or a command.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from urllib.parse import urlsplit

from .ci_monitor import _credential_redactor
from .ids import is_valid_id, new_id
from .redact import redact_text

log = logging.getLogger(__name__)
ENTITY = "ci_fix"
ACTIVE = frozenset({"claimed", "preparing", "running", "finishing"})
TERMINAL = frozenset({"succeeded", "no_change", "failed", "cancelled", "expired"})
# The runner moves a fix through these; ``queued -> claimed`` happens only in ``claim`` and
# ``queued -> cancelled`` only in ``cancel``.
_TRANSITIONS = {
    "claimed": {"preparing", "running", "finishing", "failed", "cancelled", "expired"},
    "preparing": {"running", "finishing", "failed", "cancelled", "expired"},
    "running": {"finishing", "failed", "cancelled", "expired"},
    "finishing": {"succeeded", "no_change", "failed", "cancelled", "expired"},
}
BRANCH_PREFIX = "herdr-ci-fix/"

_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}\Z")
_BRANCH = re.compile(re.escape(BRANCH_PREFIX) + r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:-]{0,49}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_DEFAULTS = {
    "enabled": False,
    "agent_api_key": "",
    "hosts": [],
    "max_concurrent": {"global": 4, "per_host": 2},
    "models": [],
    "deadline_minutes": 60,
    "idle_minutes": 15,
    "queue_timeout_minutes": 30,
    "egress": {"default": ["gateway"], "per_project": {}},
    # A fix branch is pushed to the repository itself, so its CI runs without approval and with whatever secrets the
    # CI exposes to pull-request pipelines (some repositories do: checked on ci.metabolomics.us, spike 5).
    # ``deny`` always wins; a non-empty ``allow`` limits fixes to those repositories.
    "repositories": {"allow": [], "deny": []},
    "limits": {"max_commits": 10, "max_files": 50, "max_diff_kb": 512, "mirror_gb": 5, "free_disk_gb": 10},
    "paths": {"flag": [".woodpecker/**", ".github/**", "*.lock"], "block": [".git*/hooks/**"]},
    "pull_request": {"draft": True, "label": "herdr-ci-fix"},
}
_RANGES = {
    ("max_concurrent", "global"): (1, 64), ("max_concurrent", "per_host"): (1, 16),
    ("limits", "max_commits"): (1, 100), ("limits", "max_files"): (1, 1000),
    ("limits", "max_diff_kb"): (1, 65536), ("limits", "mirror_gb"): (1, 1024),
    ("limits", "free_disk_gb"): (1, 4096),
}


def _fail(path, message):
    raise ValueError(f"control_plane.ci_fix.{path} {message}")


def _mapping(value, path, allowed):
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(path, "must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        _fail(path, f"has unknown keys: {', '.join(unknown)}")
    return value


def _int(value, path, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(path, f"must be an integer from {low} to {high}")
    return value


def _strings(value, path, pattern, limit):
    if not isinstance(value, list) or len(value) > limit:
        _fail(path, f"must be a list of at most {limit} entries")
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            _fail(path, "has an entry that is not allowed")
    return list(dict.fromkeys(value))


def _patterns(value, path):
    if not isinstance(value, list) or len(value) > 50:
        _fail(path, "must be a list of at most 50 path patterns")
    for item in value:
        if (not isinstance(item, str) or not item or len(item) > 200 or _CONTROL.search(item)
                or item.startswith("/") or ".." in item.split("/")):
            _fail(path, "has a path pattern that is not allowed")
    return list(dict.fromkeys(value))


def ci_fix_config(raw):
    """Validated ``control_plane.ci_fix`` settings. ``None`` means every default, which is *disabled*.

    Forge credentials never belong here: the runner reads them from files it owns, by path, in its own
    configuration."""
    top = _mapping(raw, "", set(_DEFAULTS))
    cfg = {key: (dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
           for key, value in _DEFAULTS.items()}
    if "enabled" in top:
        if not isinstance(top["enabled"], bool):
            _fail("enabled", "must be true or false")
        cfg["enabled"] = top["enabled"]
    if "hosts" in top:
        cfg["hosts"] = _strings(top["hosts"], "hosts", _HOST, 32)
    if "models" in top:
        cfg["models"] = _strings(top["models"], "models", _MODEL, 20)
    for key, high in (("deadline_minutes", 1440), ("idle_minutes", 240), ("queue_timeout_minutes", 1440)):
        if key in top:
            cfg[key] = _int(top[key], key, 1, high)
    for section in ("max_concurrent", "limits"):
        given = _mapping(top.get(section), section, set(_DEFAULTS[section]))
        for key, value in given.items():
            cfg[section][key] = _int(value, f"{section}.{key}", *_RANGES[(section, key)])
    egress = _mapping(top.get("egress"), "egress", {"default", "per_project"})
    if "default" in egress:
        cfg["egress"]["default"] = _strings(egress["default"], "egress.default", _HOSTNAME, 32)
    projects = egress.get("per_project", {})
    if not isinstance(projects, dict) or len(projects) > 200:
        _fail("egress.per_project", "must be a mapping of at most 200 repositories")
    cfg["egress"]["per_project"] = {}
    for repository, hosts in projects.items():
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or len(repository) > 300:
            _fail("egress.per_project", "has a repository name that is not allowed")
        cfg["egress"]["per_project"][repository] = _strings(hosts, "egress.per_project", _HOSTNAME, 32)
    repositories = _mapping(top.get("repositories"), "repositories", {"allow", "deny"})
    for key, value in repositories.items():
        cfg["repositories"][key] = _strings(value, f"repositories.{key}", _REPOSITORY, 200)
    paths = _mapping(top.get("paths"), "paths", {"flag", "block"})
    for key, value in paths.items():
        cfg["paths"][key] = _patterns(value, f"paths.{key}")
    pull = _mapping(top.get("pull_request"), "pull_request", {"draft", "label"})
    if "draft" in pull:
        if not isinstance(pull["draft"], bool):
            _fail("pull_request.draft", "must be true or false")
        cfg["pull_request"]["draft"] = pull["draft"]
    if "label" in pull:
        if not isinstance(pull["label"], str) or not _LABEL.fullmatch(pull["label"]):
            _fail("pull_request.label", "must be a short plain label")
        cfg["pull_request"]["label"] = pull["label"]
    if cfg["enabled"] and not cfg["hosts"]:
        _fail("hosts", "must list at least one host when ci_fix is enabled")
    key = top.get("agent_api_key", "")
    if not isinstance(key, str) or not re.fullmatch(r"[!-~]*", key):
        _fail("agent_api_key", "must be an ASCII string without whitespace or control characters")
    if cfg["enabled"] and not key:
        _fail("agent_api_key", "must be nonempty when ci_fix is enabled")
    cfg["agent_api_key"] = key
    return cfg


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit * 4 or _TEXT_CONTROL.search(value):
        raise ValueError(f"{name} is required")
    return redact_text(value)[:limit]


def _reason(value):
    """A display reason: redacted and truncated, never rejected for length."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or _TEXT_CONTROL.search(value):
        raise ValueError("reason is invalid")
    return redact_text(value)[:500] or None


def _session_id(value):
    """An identity, so never truncated: too long or odd means refused."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 1000 or _CONTROL.search(value):
        raise ValueError("session_id is invalid")
    return value


def _run_key(repository, number):
    return f"{repository}:{number}"


class CIFixService:
    """The one writer of ``ci_fix`` events. Callers enforce authorization; this enforces the machine."""

    def __init__(self, repo, config):
        self.repo = repo
        self.config = config
        self.redact = _credential_redactor(config["agent_api_key"])
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ reading
    def _fold(self, fix_id):
        stream = self.repo.stream(ENTITY, fix_id)
        if not stream or stream[0].get("event_type") != "CIFixRequested":
            raise KeyError(fix_id)
        first = stream[0]["payload"]
        record = {
            "id": fix_id, "state": "queued", "reason": None, "repository": first["repository"],
            "number": first["number"], "run": first["run"], "revision": first["revision"],
            "ci_url": first["ci_url"], "operation_id": first["operation_id"],
            "request": first["request"], "context": first["context"], "requested_by": first.get("requested_by"),
            "host": None, "model": None, "session_id": None, "branch": None, "pr_url": None,
            "result": None, "cancel_requested": False,
            "requested_at": stream[0]["source_timestamp"], "started_at": None, "ended_at": None,
            "updated_at": stream[0]["source_timestamp"],
        }
        for event in stream[1:]:
            payload, kind = event["payload"], event["event_type"]
            record["updated_at"] = event["source_timestamp"]
            if kind == "CIFixStateChanged":
                record["state"] = payload["state"]
                record["reason"] = payload.get("reason")
                for key in ("host", "model", "session_id", "branch"):
                    if payload.get(key):
                        record[key] = payload[key]
                if payload["state"] == "running" and not record["started_at"]:
                    record["started_at"] = event["source_timestamp"]
                if payload["state"] in TERMINAL:
                    record["ended_at"] = event["source_timestamp"]
            elif kind == "CIFixCancelRequested":
                record["cancel_requested"] = True
            elif kind == "CIFixResult":
                record["result"] = payload
                record["pr_url"] = payload.get("pr_url") or record["pr_url"]
        return record

    def _all(self):
        records = []
        for fix_id in self.repo.entity_ids(ENTITY):
            try:
                records.append(self._fold(fix_id))
            except KeyError:
                continue
        return sorted(records, key=lambda row: row["id"])

    def view(self, record, *, runner=False):
        """The answer for the dashboard; ``runner`` adds the bounded context the agent is told about."""
        keys = ("id", "state", "reason", "repository", "number", "revision", "ci_url", "host", "model",
                "session_id", "branch", "pr_url", "result", "cancel_requested", "requested_at",
                "started_at", "ended_at", "updated_at")
        out = {key: record[key] for key in keys}
        out["run"] = record["run"]
        if runner:
            out["context"] = record["context"]
        return self.redact(out)

    def get(self, fix_id, *, runner=False):
        if not isinstance(fix_id, str) or not is_valid_id(fix_id, "cifix"):
            raise KeyError(fix_id)
        return self.view(self._fold(fix_id), runner=runner)

    def list(self, run=None, limit=100, host=None, active=False):
        rows = self._all()
        if run is not None:
            rows = [row for row in rows if row["run"] == run]
        if host is not None:
            rows = [row for row in rows if row["host"] == host]
        if active:
            rows = [row for row in rows if row["state"] in ACTIVE]
        return {"fixes": [self.view(row) for row in reversed(rows)][:max(1, min(int(limit), 200))]}

    # ------------------------------------------------------------------ writing
    def _event(self, fix_id, event_type, payload):
        self.repo.record_event(ENTITY, fix_id, event_type, self.redact(payload))

    def request(self, monitor, repository, number, operation_id, *, provider_url=None, actor=None):
        """Record one click. Returns ``(view, created)``; a replay or an already-active run returns the
        existing fix instead of starting another."""
        from .ci_debug import ci_run_context
        if not isinstance(repository, str) or not repository.strip() or len(repository) > 300:
            raise ValueError("repository is required")
        if isinstance(number, bool) or not re.fullmatch(r"[1-9][0-9]{0,15}", str(number)):
            raise ValueError("positive pipeline number is required")
        number = int(number)
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
            raise ValueError("operation_id is required")
        request = {"repository": repository, "number": number,
                   **({"provider_url": provider_url} if provider_url is not None else {})}
        previous = self._by_operation(operation_id, request)
        if previous is not None:
            return self.view(previous), False
        context = ci_run_context(monitor, repository, number, request, provider_url)
        run = _run_key(context["repository"], number)
        self._repository_allowed(context["repository"])
        with self._lock:
            previous = self._by_operation(operation_id, request)
            if previous is not None:
                return self.view(previous), False
            for row in self._all():
                if row["run"] == run and row["state"] not in TERMINAL:
                    return self.view(row), False
            fix_id = new_id("cifix")
            self._event(fix_id, "CIFixRequested", {
                "repository": context["repository"], "number": number, "run": run,
                "revision": context["revision"], "ci_url": context["ci_url"], "operation_id": operation_id,
                "request": request, "context": context, "requested_by": actor})
            return self.view(self._fold(fix_id)), True

    def _repository_allowed(self, repository):
        rules = self.config["repositories"]
        name = repository.lower()
        if name in {item.lower() for item in rules["deny"]} or (
                rules["allow"] and name not in {item.lower() for item in rules["allow"]}):
            raise ValueError("this repository is not enabled for CI fix agents (control_plane.ci_fix.repositories)")

    def _by_operation(self, operation_id, request):
        for row in self._all():
            if row["operation_id"] == operation_id:
                if row["request"] != request:
                    raise ValueError("operation_id already used for a different request")
                return row
        return None

    def runner_config(self):
        """The settings a runner follows, excluding the server's authentication secret."""
        return self.redact({key: value for key, value in self.config.items() if key != "agent_api_key"})

    def _age_minutes(self, record):
        import datetime
        then = datetime.datetime.fromisoformat(record["requested_at"]).timestamp()
        return (self.repo._now() - then) / 60

    def claim(self, host, defer=None):
        """Hand the runner the oldest queued request it may start on ``host``, once. Over a cap the
        request stays queued and says why.

        ``defer`` is the runner saying it cannot start anything right now for a reason that holds for
        every host (``{"kind": "no_model", "reason": ...}``): nothing is claimed, the oldest queued
        request shows the reason, and a request that has waited past ``queue_timeout_minutes`` for a
        model fails with it instead of waiting for ever."""
        if not isinstance(host, str) or not _HOST.fullmatch(host) or host not in self.config["hosts"]:
            raise ValueError("host is not enabled for CI fix agents")
        if defer is not None:
            if not isinstance(defer, dict) or defer.get("kind") != "no_model" or set(defer) - {"kind", "reason"}:
                raise ValueError("defer must be {kind: no_model, reason}")
            note = _reason(self.redact(defer.get("reason"))) or "no model available"
        with self._lock:
            rows = self._all()
            queued = [row for row in rows if row["state"] == "queued"]
            if not queued:
                return {"request": None, "reason": "nothing queued"}
            if defer is not None:
                limit = self.config["queue_timeout_minutes"]
                for row in queued:
                    if self._age_minutes(row) > limit:
                        self._event(row["id"], "CIFixStateChanged",
                                    {"state": "failed", "reason": f"gave up after {limit} min: {note}"[:500]})
                    elif row["reason"] != note:
                        self._event(row["id"], "CIFixStateChanged", {"state": "queued", "reason": note})
                return {"request": None, "reason": note}
            active = [row for row in rows if row["state"] in ACTIVE]
            limits = self.config["max_concurrent"]
            reason = None
            if len(active) >= limits["global"]:
                reason = f"waiting for a free slot (global limit {limits['global']})"
            elif sum(1 for row in active if row["host"] == host) >= limits["per_host"]:
                reason = f"waiting for a free slot on {host} (limit {limits['per_host']})"
            if reason:
                if queued[0]["reason"] != reason:
                    self._event(queued[0]["id"], "CIFixStateChanged", {"state": "queued", "reason": reason})
                return {"request": None, "reason": reason}
            first = queued[0]
            self._event(first["id"], "CIFixStateChanged", {"state": "claimed", "host": host})
            return {"request": self.view(self._fold(first["id"]), runner=True), "reason": None}

    def set_state(self, fix_id, state, *, reason=None, host=None, model=None, session_id=None, branch=None):
        """A runner-reported transition. Repeating the current state with nothing new is a no-op, so a
        retried POST after a lost reply is safe."""
        if not isinstance(fix_id, str) or not is_valid_id(fix_id, "cifix"):
            raise KeyError(fix_id)
        if not isinstance(state, str) or state not in set(_TRANSITIONS) | TERMINAL:
            raise ValueError("state is not one the runner may set")
        fields = {
            "reason": _reason(self.redact(reason)),
            "host": host, "model": model, "session_id": _session_id(session_id),
            "branch": branch,
        }
        if host is not None and (not isinstance(host, str) or not _HOST.fullmatch(host)):
            raise ValueError("host is invalid")
        if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
            raise ValueError("model is invalid")
        if branch is not None and (not isinstance(branch, str) or not _BRANCH.fullmatch(branch)
                                   or branch.endswith((".lock", "."))):
            raise ValueError(f"branch must be a name under {BRANCH_PREFIX}")
        with self._lock:
            record = self._fold(fix_id)
            current = record["state"]
            if current in TERMINAL:
                if current == state:
                    return self.view(record)
                raise ValueError(f"fix is already {current}")
            if current == "queued" or state not in (_TRANSITIONS.get(current, set()) | {current}):
                raise ValueError(f"cannot move a fix from {current} to {state}")
            if state == "succeeded" and not (record["result"] or {}).get("pr_url"):
                raise ValueError("succeeded requires a recorded result with a pull request")
            fresh = {key: value for key, value in fields.items() if value and record.get(key) != value}
            if state == current and not fresh:
                return self.view(record)
            self._event(fix_id, "CIFixStateChanged", {"state": state, **fresh})
            record = self._fold(fix_id)
            if "session_id" in fresh or "branch" in fresh:
                self._associate(record)
            return self.view(record)

    _RESULT_INT = {"commits": 1000, "files": 100000, "insertions": 10**9, "deletions": 10**9, "diff_kb": 10**7}

    def record_result(self, fix_id, body):
        """The runner's bounded result, accepted once and only while it is finishing."""
        if not isinstance(fix_id, str) or not is_valid_id(fix_id, "cifix"):
            raise KeyError(fix_id)
        if not isinstance(body, dict):
            raise ValueError("result must be an object")
        allowed = {"pr_url", "pr_number", "branch", "summary", "flags", *self._RESULT_INT}
        unknown = sorted(str(key) for key in body if key not in allowed)
        if unknown:
            raise ValueError(f"result has unknown fields: {', '.join(unknown)}")
        result = {}
        for key, high in self._RESULT_INT.items():
            if key in body:
                result[key] = _int(body[key], key, 0, high)
        if "pr_number" in body:
            result["pr_number"] = _int(body["pr_number"], "pr_number", 1, 10**9)
        if body.get("pr_url") is not None:
            url = body["pr_url"]
            parts = urlsplit(url) if isinstance(url, str) and len(url) <= 2048 else None
            if (parts is None or parts.scheme != "https" or not parts.hostname or parts.username
                    or parts.password or _CONTROL.search(url)):
                raise ValueError("pr_url must be an https URL")
            result["pr_url"] = url
        if body.get("branch") is not None:
            if not isinstance(body["branch"], str) or not _BRANCH.fullmatch(body["branch"]):
                raise ValueError(f"branch must be a name under {BRANCH_PREFIX}")
            result["branch"] = body["branch"]
        if body.get("summary") is not None:
            result["summary"] = _text(self.redact(body["summary"]), "summary", 4000)
        if "flags" in body:
            flags = body["flags"]
            if not isinstance(flags, list) or len(flags) > 20:
                raise ValueError("flags must be a list of at most 20 entries")
            result["flags"] = [_text(self.redact(flag), "flag", 200) for flag in flags]
        with self._lock:
            record = self._fold(fix_id)
            if record["state"] != "finishing":
                raise ValueError("a result is accepted only while the fix is finishing")
            if record["result"] is not None:
                raise ValueError("result already recorded")
            self._event(fix_id, "CIFixResult", result)
            record = self._fold(fix_id)
            if result.get("pr_url"):
                self._associate(record)
            return self.view(record)

    def _associate(self, record):
        """Tie the agent's session to the run it works on. The control plane writes this itself from the
        validated state the runner reported (the agent in the box has no way to call it), as an inspector
        association: the branch, a worktree label naming the run and, once it exists, the draft pull request.
        Best effort: a session id the inspector does not accept must not fail the transition."""
        session_id = record.get("session_id")
        if not session_id:
            return
        event = {"kind": "association", "agent_id": "main", "worktree": f"ci-fix {record['run']}"[:512]}
        if record.get("branch"):
            event["branch"] = record["branch"]
        result = record.get("result") or {}
        if result.get("pr_url"):
            event["pr"] = {"number": result.get("pr_number"), "url": result["pr_url"], "repo": record["repository"]}
        digest = hashlib.sha256(repr(sorted(event.items(), key=str)).encode()).hexdigest()[:24]
        event["event_id"] = f"cifix:{record['id']}:{digest}"[:128]
        try:
            self.repo.inspector.ingest(session_id, event)
        except (ValueError, RuntimeError) as exc:
            log.warning("ci fix %s: association not recorded: %s", record["id"], type(exc).__name__)

    def cancel(self, fix_id, *, actor=None):
        """Operator cancel. A queued fix ends at once; a running one is flagged and the runner stops it,
        cleans up and reports ``cancelled``."""
        if not isinstance(fix_id, str) or not is_valid_id(fix_id, "cifix"):
            raise KeyError(fix_id)
        with self._lock:
            record = self._fold(fix_id)
            if record["state"] in TERMINAL or record["cancel_requested"]:
                return self.view(record)
            if record["state"] == "queued":
                self._event(fix_id, "CIFixStateChanged", {"state": "cancelled", "reason": "cancelled by operator"})
            else:
                self._event(fix_id, "CIFixCancelRequested", {"actor": actor})
            return self.view(self._fold(fix_id))
