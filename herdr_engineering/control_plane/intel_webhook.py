"""GitHub webhook intake for project intelligence: signature check, replay protection, normalisation.

Pure functions plus a small replay guard; no HTTP and no network. Wiring to a public receiver needs the App
webhook secret file and URL (blocker B2), so nothing here is reachable until the operator provides them.
Payload text (PR bodies, comments, logs) is untrusted data; only structural fields are read.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

MAX_BODY = 1_000_000
HANDLED = frozenset({"pull_request", "check_run", "workflow_run", "issues", "push", "pull_request_review"})


class WebhookRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def verify_signature(secret: bytes, body: bytes, header: str | None) -> None:
    """Raise ``WebhookRejected`` unless ``X-Hub-Signature-256`` matches. Constant-time; secret must be non-empty."""
    if not secret:
        raise WebhookRejected("no webhook secret configured")
    if len(body) > MAX_BODY:
        raise WebhookRejected("body too large")
    if not header or not header.startswith("sha256="):
        raise WebhookRejected("missing signature")
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header[7:]):
        raise WebhookRejected("bad signature")


class ReplayGuard:
    """Remembers recent ``X-GitHub-Delivery`` ids so a captured request cannot be replayed."""

    def __init__(self, capacity: int = 10_000):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._cap = capacity
        self._lock = threading.Lock()

    def seen(self, delivery_id: str | None) -> bool:
        """Non-recording check. Pair with ``mark`` AFTER the event is durably stored, so GitHub's retry of a delivery
        that failed to process is not dropped."""
        if not delivery_id:
            raise WebhookRejected("missing delivery id")
        with self._lock:
            return delivery_id in self._seen

    def mark(self, delivery_id: str) -> None:
        with self._lock:
            self._seen[delivery_id] = None
            if len(self._seen) > self._cap:
                self._seen.popitem(last=False)

    def first_time(self, delivery_id: str | None) -> bool:
        if not delivery_id:
            raise WebhookRejected("missing delivery id")
        with self._lock:
            if delivery_id in self._seen:
                return False
            self._seen[delivery_id] = None
            if len(self._seen) > self._cap:
                self._seen.popitem(last=False)
            return True


def _iso(value, received_at: datetime | None) -> str:
    """The source's own timestamp, else the real delivery time (labelled in the payload); never an invented ``now()``."""
    if isinstance(value, str) and value:
        return value
    if received_at is None:
        raise WebhookRejected("event carries no timestamp and no delivery time was supplied")
    return received_at.astimezone(UTC).isoformat()


def normalise(event_name: str, payload: dict, *, org_id: str, installation: str, project_ids: tuple[str, ...],
              received_at: datetime | None = None) -> dict | None:
    """Map one delivery to a project-intelligence raw event, or ``None`` for ignorable noise."""
    if event_name not in HANDLED or not isinstance(payload, dict):
        return None
    repo = (payload.get("repository") or {}).get("full_name")
    if not isinstance(repo, str):
        return None
    base = {"org_id": org_id, "source_system": "github", "source_instance": installation, "project_ids": list(project_ids),
            "payload_schema_version": 1, "category": "work"}
    if event_name == "pull_request":
        pr = payload.get("pull_request") or {}
        sha = (pr.get("head") or {}).get("sha", "")
        number = pr.get("number")
        # source_version includes the head SHA: a changed head is a new fact, not an edit of the old one.
        return dict(base, source_id=f"{repo}#pr{number}", source_version=f"{sha}:{payload.get('action')}",
                    event_type=f"pr.{payload.get('action')}", occurred_at=_iso(pr.get("updated_at"), received_at),
                    object_ref=f"pr:{repo}#{number}", actor_ref=f"github:{(pr.get('user') or {}).get('login')}",
                    title=f"PR #{number} {payload.get('action')}", payload={"head_sha": sha, "state": pr.get("state"),
                    "merged": bool(pr.get("merged")), "base_ref": (pr.get("base") or {}).get("ref")},
                    provenance_refs=[{"source_ref": f"github:{repo}", "anchor_type": "object", "anchor_value": f"pr/{number}",
                                      "git_sha": sha}])
    if event_name in ("check_run", "workflow_run"):
        run = payload.get(event_name) or {}
        sha = run.get("head_sha", "")
        rid = run.get("id")
        concl = run.get("conclusion") or run.get("status")
        return dict(base, source_id=f"{repo}#{event_name}{rid}", source_version=f"{sha}:{run.get('status')}:{concl}",
                    event_type=f"{event_name}.{payload.get('action')}", occurred_at=_iso(run.get("updated_at") or run.get("completed_at"), received_at),
                    object_ref=f"ci:{repo}#{rid}", actor_ref=None, category="ci",
                    title=f"{run.get('name')}: {concl}", payload={"head_sha": sha, "conclusion": run.get("conclusion"),
                    "status": run.get("status"), "name": run.get("name"), "cancelled": run.get("conclusion") == "cancelled"},
                    provenance_refs=[{"source_ref": f"github:{repo}", "anchor_type": "object", "anchor_value": f"{event_name}/{rid}",
                                      "git_sha": sha}])
    if event_name == "pull_request_review":
        rv = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        return dict(base, source_id=f"{repo}#review{rv.get('id')}", source_version=str(rv.get("commit_id", "")),
                    event_type=f"review.{rv.get('state')}", occurred_at=_iso(rv.get("submitted_at"), received_at),
                    object_ref=f"pr:{repo}#{pr.get('number')}", actor_ref=f"github:{(rv.get('user') or {}).get('login')}",
                    title=f"review {rv.get('state')}", payload={"commit_id": rv.get("commit_id"), "state": rv.get("state"),
                    "reviewer_type": (rv.get("user") or {}).get("type")},
                    provenance_refs=[{"source_ref": f"github:{repo}", "anchor_type": "object", "anchor_value": f"review/{rv.get('id')}"}])
    if event_name == "issues":
        iss = payload.get("issue") or {}
        return dict(base, source_id=f"{repo}#issue{iss.get('number')}", source_version=f"{iss.get('updated_at')}:{payload.get('action')}",
                    event_type=f"issue.{payload.get('action')}", occurred_at=_iso(iss.get("updated_at"), received_at),
                    object_ref=f"issue:{repo}#{iss.get('number')}", actor_ref=f"github:{(iss.get('user') or {}).get('login')}",
                    title=f"issue #{iss.get('number')} {payload.get('action')}", payload={"state": iss.get("state")},
                    provenance_refs=[{"source_ref": f"github:{repo}", "anchor_type": "object", "anchor_value": f"issue/{iss.get('number')}"}])
    return None


def stale(delivery_received: datetime, now: datetime, max_age: timedelta = timedelta(days=7)) -> bool:
    return now - delivery_received > max_age


def reconcile_open_prs(app, store, cfg: dict, *, org_id: str, installation: str, repo_projects: dict[str, list[str]],
                       received_at: datetime, max_prs: int = 200) -> dict:
    """Backstop for missed deliveries: record each open PR head that no webhook already recorded.

    ``app.open_prs(repo)`` is the existing review-gate GitHub client call. Events use ``pr.reconciled`` and a
    version of ``<head sha>:reconciled``, so a repeat run is a no-op and a webhook that later arrives is stored
    alongside (it is a different fact), never overwriting it. Unmapped repositories are not queried.
    """
    stats = {"seen": 0, "recorded": 0, "already": 0, "errors": 0}
    for repo, projects in sorted(repo_projects.items()):
        try:
            prs = list(app.open_prs(repo))[:max_prs]
        except Exception:  # noqa: BLE001 - one repository failing must not stop the others
            stats["errors"] += 1
            continue
        for pr in prs:
            stats["seen"] += 1
            source_id = f"{repo}#pr{pr.number}"
            if store.has_source_version(org_id, "github", installation, source_id, f"{pr.head_sha}:"):
                stats["already"] += 1
                continue
            raw = normalise("pull_request", {"action": "reconciled", "repository": {"full_name": repo},
                                             "pull_request": {"number": pr.number, "head": {"sha": pr.head_sha},
                                                              "user": {"login": pr.author_login}, "state": "open",
                                                              "base": {"ref": pr.base_ref}}},
                            org_id=org_id, installation=installation, project_ids=tuple(projects), received_at=received_at)
            store.append_event(raw, cfg=cfg)
            stats["recorded"] += 1
    return stats
