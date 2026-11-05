"""Independent deploy outcome check: the hub itself looks at ``/api/v1/health`` after it authorized a deploy.

An authorized deploy opens one ``DeployCheck`` (stored, so it survives the hub's own restart when the control-plane
container is replaced).  ``verify_deploy`` is one polling step: it reads the deployed commit once and moves the check to
a final status or leaves it ``awaiting``.  An unreachable health endpoint is NOT a failure (the hub is down while its
container is replaced); only the deadline ends the wait, and a deploy nobody could confirm is ``unconfirmed``, never
"success".  The worker's word is used for one thing only: an exit code 3 ("failed to roll back") report can raise the
check to ``failed_rollback`` (a change that only ever makes the owner look sooner).
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from .deploy import DeployMatch, scope_config

if TYPE_CHECKING:
    from ..assistant import AssistantStore
    from .workers import Worker

__all__ = ["DeployCheck", "Status", "STATUSES", "FINAL", "sanitize_check", "short_label", "NOT_OPENED", "DEFAULT_CHECK_MINUTES", "DEFAULT_WINDOW_MINUTES", "open_check",
           "verify_deploy", "record_exit", "check_minutes", "describe_status"]

Status = Literal["awaiting", "healthy", "rolled_back", "failed_rollback", "unconfirmed"]
STATUSES: tuple[Status, ...] = ("awaiting", "healthy", "rolled_back", "failed_rollback", "unconfirmed")
FINAL = frozenset(STATUSES) - {"awaiting"}
NOT_OPENED = "not_opened"           # last_error of an unconfirmed row written because the real check could not be opened
DEFAULT_CHECK_MINUTES = 20
DEFAULT_WINDOW_MINUTES = 10
MIN_PREFIX = 12                     # a shorter commit prefix is never accepted as "the target commit"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_HEX = re.compile(r"[0-9a-f]{7,64}")
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class DeployCheck:
    check_id: str
    tenant_id: str
    user_id: str
    project: str = ""
    scope: str = ""
    sha: str = ""
    request_id: str = ""            # the owner-approved request, "" when a standing grant authorized it
    status: Status = "awaiting"
    started_at: datetime = _EPOCH
    deadline: datetime = _EPOCH
    last_error: str = ""            # a short code, never remote text
    worker_id: str = ""
    task_id: str = ""
    repo: str = ""
    health_url: str = ""
    window_ends: datetime = _EPOCH  # before this a different commit only means the deploy has not happened yet
    exit_code: int | None = None    # what the worker reported deploy.sh exited with (data, never trusted for "healthy")
    notified: bool = False          # the Slack outcome message was posted
    reverified: str = ""            # after a healthy result: on_main | off_main | unverified | newer | ""
    finished_at: datetime = _EPOCH
    seen_commit: str = ""           # the last commit health reported (12 chars)


_DATES = ("started_at", "deadline", "window_ends", "finished_at")


def sanitize_check(row: DeployCheck) -> DeployCheck | None:
    """Fail closed on a row read back from stored state: None when it cannot be trusted at all (no ids, malformed
    sha), otherwise a row whose status is one the hub knows.  Anything odd becomes ``unconfirmed``, never ``healthy``."""
    if not all(isinstance(v, str) and v for v in (row.check_id, row.tenant_id, row.user_id)):
        return None
    if not isinstance(row.sha, str) or not _SHA.fullmatch(row.sha):
        return None
    for name in ("project", "scope", "request_id", "worker_id", "task_id", "repo", "health_url", "last_error",
                 "reverified", "seen_commit"):
        if not isinstance(getattr(row, name), str):
            setattr(row, name, "")
    if row.exit_code is not None and type(row.exit_code) is not int:
        row.exit_code = None
    if type(row.notified) is not bool:
        row.notified = False
    bad = row.status not in STATUSES
    for name in _DATES:
        value = getattr(row, name)
        if not isinstance(value, datetime):
            setattr(row, name, _EPOCH)
            bad = True
        elif value.tzinfo is None:
            setattr(row, name, value.replace(tzinfo=UTC))
    if bad:
        row.status = "unconfirmed"
        row.last_error = "bad_state"
        row.notified = False
    return row


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def check_minutes(deploy_cfg: Any) -> int:
    value = deploy_cfg.get("deploy_check_minutes") if isinstance(deploy_cfg, dict) else None
    return value if type(value) is int and 1 <= value <= 240 else DEFAULT_CHECK_MINUTES


def _window_minutes(deploy_cfg: Any, total: int) -> int:
    value = deploy_cfg.get("deploy_window_minutes") if isinstance(deploy_cfg, dict) else None
    window = value if type(value) is int and 1 <= value <= 240 else DEFAULT_WINDOW_MINUTES
    return min(window, max(1, total - 1)) if total > 1 else 1


def open_check(store: AssistantStore, deploy_cfg: Any, worker: Worker, match: DeployMatch, task_id: str,
               request_id: str, now: datetime) -> DeployCheck:
    """Record that this exact deploy was authorized and must be verified.  One awaiting check per (worker, scope, sha):
    asking again returns the open one.  The health address comes from the live config, never from the request."""
    now = _utc(now)
    if not _SHA.fullmatch(match.sha):
        raise ValueError("a deploy check needs a full commit sha")
    with store._lock:
        for old in store.list_deploy_checks(worker.tenant_id, worker.user_id, worker_id=worker.worker_id):
            if old.status == "awaiting" and (old.project, old.scope, old.sha) == (match.project, match.scope, match.sha):
                return old
        total = check_minutes(deploy_cfg)
        health_url = scope_config(deploy_cfg, match).get("health_url")
        check = DeployCheck(
            check_id="dc_" + secrets.token_urlsafe(12), tenant_id=worker.tenant_id, user_id=worker.user_id,
            project=match.project, scope=match.scope, sha=match.sha, request_id=request_id, started_at=now,
            deadline=now + timedelta(minutes=total), worker_id=worker.worker_id, task_id=task_id, repo=match.repo,
            health_url=health_url if isinstance(health_url, str) else "",
            window_ends=now + timedelta(minutes=_window_minutes(deploy_cfg, total)),
            last_error="" if isinstance(health_url, str) and health_url else "no_health_url")
        # audit first: if the durable audit fails nothing is stored, and the caller records "not verified"
        store.audit_record(tenant_id=check.tenant_id, user_id=check.user_id, actor="hub:deploycheck",
                           action="assistant.deploy.check_opened", subject_id=check.check_id,
                           detail=f"check={check.check_id} project={check.project} scope={check.scope} "
                                  f"sha12={check.sha[:12]} worker={check.worker_id}")
        store.put_deploy_check(check)
    return check


def record_not_opened(store: AssistantStore, worker: Worker, match: DeployMatch, task_id: str, request_id: str,
                      now: datetime) -> None:
    """The check for an authorized deploy could not be opened.  Say so in the audit (ids only) and leave a final
    ``unconfirmed`` row so the task summary and the Slack sweep report "NOT verified by the hub".  Best effort."""
    now = _utc(now)
    row = DeployCheck(
        check_id="dc_" + secrets.token_urlsafe(12), tenant_id=worker.tenant_id, user_id=worker.user_id,
        project=match.project, scope=match.scope, sha=match.sha, request_id=request_id, status="unconfirmed",
        started_at=now, deadline=now, window_ends=now, finished_at=now, worker_id=worker.worker_id, task_id=task_id,
        repo=match.repo, last_error=NOT_OPENED)
    for step in (lambda: store.put_deploy_check(row),
                 lambda: store.audit_record(tenant_id=row.tenant_id, user_id=row.user_id, actor="hub:deploycheck",
                                            action="assistant.deploy.check_failed", subject_id=row.check_id,
                                            detail=f"check={row.check_id} project={row.project} scope={row.scope} "
                                                   f"sha12={row.sha[:12]} worker={row.worker_id}")):
        try:
            step()
        except Exception:                                  # noqa: BLE001 - each step on its own
            pass


def _matches(commit: str, sha: str) -> bool:
    if not isinstance(commit, str) or not _SHA.fullmatch(sha):
        return False
    return len(commit) >= MIN_PREFIX and _HEX.fullmatch(commit) is not None and (
        commit.startswith(sha) or sha.startswith(commit))


def _finish(store: AssistantStore, check_id: str, tenant: str, user: str, status: str, now: datetime,
            expect: tuple[str, ...], **fields: Any) -> DeployCheck | None:
    """Move a stored check to ``status`` unless it changed meanwhile (the exit-3 report may have raised it)."""
    with store._lock:
        cur = store.get_deploy_check(check_id, tenant_id=tenant, user_id=user)
        if cur is None or cur.status not in expect:
            return cur
        new = replace(cur, status=status, finished_at=_utc(now), **fields)
        store.put_deploy_check(new)
        store.audit_record(tenant_id=tenant, user_id=user, actor="hub:deploycheck",
                           action="assistant.deploy.outcome", subject_id=check_id,
                           detail=f"check={check_id} status={status} sha12={new.sha[:12]}")
        return new


def _live_health_url(deploy_cfg: Any, check: DeployCheck) -> str:
    """The health address the LIVE config has for this check's project and scope ("" when the scope is gone)."""
    url = scope_config(deploy_cfg, DeployMatch(check.project, check.scope, check.repo, check.sha, "")).get("health_url")
    return url if isinstance(url, str) else ""


def verify_deploy(store: AssistantStore, check: DeployCheck, health: Any, now: datetime, *,
                  reverify: Callable[[DeployCheck], str] | None = None,
                  lineage: Callable[[DeployCheck, str], str] | None = None,
                  deploy_cfg: Any = None) -> DeployCheck:
    """One polling step.  Healthy when the health endpoint reports the target commit; rolled back when it reports a
    different commit once the deploy window has passed (unless ``lineage`` says that commit CONTAINS the target: then
    healthy, ``reverified="newer"``); unconfirmed at the deadline.  An unreachable or odd answer only records a short
    code.  When ``deploy_cfg`` is given, a stored health address that differs from the live config's is never polled.
    Never raises."""
    now = _utc(now)
    try:
        cur = store.get_deploy_check(check.check_id, tenant_id=check.tenant_id, user_id=check.user_id)
        if cur is None or cur.status != "awaiting":
            return cur or check
        commit: Any = None
        error = ""
        if not cur.health_url:
            done = _finish(store, cur.check_id, cur.tenant_id, cur.user_id, "unconfirmed", now, ("awaiting",),
                           last_error="no_health_url")
            return done or cur
        if deploy_cfg is not None and _live_health_url(deploy_cfg, cur) != cur.health_url:
            done = _finish(store, cur.check_id, cur.tenant_id, cur.user_id, "unconfirmed", now, ("awaiting",),
                           last_error="health_url_changed")
            return done or cur
        else:
            try:
                commit = health.deployed_commit(cur.health_url)
            except Exception:                      # noqa: BLE001 - the hub is briefly down while its container is replaced
                error = "unreachable"
        if not error and not isinstance(commit, str):
            error, commit = "bad_reply", None
        seen = commit[:12] if isinstance(commit, str) else ""
        if isinstance(commit, str) and _matches(commit, cur.sha):
            note = ""
            if reverify is not None:
                try:
                    note = reverify(cur)
                except Exception:                  # noqa: BLE001
                    note = "unverified"
            done = _finish(store, cur.check_id, cur.tenant_id, cur.user_id, "healthy", now, ("awaiting",),
                           last_error="", seen_commit=seen, reverified=note)
            return done or cur
        if (isinstance(commit, str) and _HEX.fullmatch(commit) and len(commit) >= MIN_PREFIX
                and now >= _utc(cur.window_ends)):
            if lineage is not None:
                try:
                    relation = lineage(cur, commit)
                except Exception:                  # noqa: BLE001
                    relation = "unverified"
                if relation == "descendant":       # what is serving contains the target commit
                    done = _finish(store, cur.check_id, cur.tenant_id, cur.user_id, "healthy", now, ("awaiting",),
                                   last_error="", seen_commit=seen, reverified="newer")
                    return done or cur
            done = _finish(store, cur.check_id, cur.tenant_id, cur.user_id, "rolled_back", now, ("awaiting",),
                           last_error="", seen_commit=seen)
            return done or cur
        if now >= _utc(cur.deadline):
            done = _finish(store, cur.check_id, cur.tenant_id, cur.user_id, "unconfirmed", now, ("awaiting",),
                           last_error=error or "other_commit", seen_commit=seen)
            return done or cur
        with store._lock:                          # still waiting: remember only what was seen
            latest = store.get_deploy_check(cur.check_id, tenant_id=cur.tenant_id, user_id=cur.user_id)
            if latest is not None and latest.status == "awaiting" and (latest.last_error, latest.seen_commit) != (
                    error, seen or latest.seen_commit):
                latest = replace(latest, last_error=error, seen_commit=seen or latest.seen_commit)
                store.put_deploy_check(latest)
            return latest or cur
    except Exception:                              # noqa: BLE001 - one bad poll never takes the checker down
        return check


def record_exit(store: AssistantStore, worker: Worker, task_id: str | None, exit_code: int,
                now: datetime) -> DeployCheck | None:
    """The worker reported ``deploy.sh`` exited with ``exit_code``.  Exit 3 (failed to roll back) raises the worker's
    most recent unfinished-or-not-yet-failed check to ``failed_rollback``; other codes are only stored.  Returns the
    check when it changed to ``failed_rollback`` (the caller posts the urgent ask), else None.  Scoped to the worker's
    own tenant, user and worker id."""
    with store._lock:
        mine = [c for c in store.list_deploy_checks(worker.tenant_id, worker.user_id, worker_id=worker.worker_id)
                if task_id is None or c.task_id == task_id]
        if not mine:
            return None
        target = mine[-1]                          # most recent
        if exit_code != 3:
            if target.exit_code != exit_code:
                store.put_deploy_check(replace(target, exit_code=exit_code))
            return None
        if target.status in ("failed_rollback", "healthy"):
            return None                            # healthy was seen by the hub itself: the claim does not undo it
        new = replace(target, status="failed_rollback", exit_code=3, finished_at=_utc(now), notified=False)
        store.put_deploy_check(new)
        store.audit_record(tenant_id=new.tenant_id, user_id=new.user_id, actor=f"worker:{worker.worker_id}",
                           action="assistant.deploy.outcome", subject_id=new.check_id,
                           detail=f"check={new.check_id} status=failed_rollback sha12={new.sha[:12]} exit=3")
        return new


_WORDS = {
    "awaiting": "still being checked",
    "healthy": "healthy",
    "rolled_back": "NOT live: a different version is serving (cause unknown)",
    "failed_rollback": "FAILED TO ROLL BACK (needs you now)",
    "unconfirmed": "unconfirmed (the hub could not confirm the new version; do not assume it worked)",
}
_LABELS = {"awaiting": "still being checked", "healthy": "healthy", "rolled_back": "not live",
           "failed_rollback": "failed to roll back", "unconfirmed": "unconfirmed"}


def describe_status(check: DeployCheck) -> str:
    if check.status == "unconfirmed" and check.last_error == NOT_OPENED:
        return "NOT verified by the hub (its check could not be opened); do not assume it worked"
    return _WORDS.get(check.status, _WORDS["unconfirmed"])


def short_label(check: DeployCheck) -> str:
    return _LABELS.get(check.status, "unconfirmed")
