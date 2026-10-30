"""Unified workspace P05 (library only, flag off): team templates, task admission, reviewer separation, leases.

What this adds on top of the P01 broker (``workspace_contracts``):

* ``TeamTemplate``: reusable roles (planner, implementer(s), one independent reviewer, optional coordinator) that
  name no host, so an instance is bound to a host only at placement time, never in the template.
* ``admit``: a task enters only through an authorized human acting via the broker (``submit_task``). Text that came
  from a message or any tainted context can never become a task; agents never submit tasks themselves (they are
  not granted ``task``). The task's role, allowed actions, budget, deadline and dependencies are checked against the
  template and the dependency graph, and a review task is refused when its reviewer is not independent.
* ``LeaseStore``: one execution lease per task with a monotonically increasing fencing token. A result is accepted
  once, only from the holder of the live token; a crashed worker's lease expires and a successor gets a higher
  token, so the stale worker is rejected; an aborted or expired lease authorizes nothing.

Nothing in the control plane calls this yet; routes, persistence and Pi mission admission come next.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .collab import AGENT_ACTIONS
from .workspace_contracts import Broker, ContractError, ToolRequest

KINDS = ("planner", "implementer", "reviewer", "coordinator")
REVIEWER_ACTIONS = frozenset({"read", "post"})          # an independent reviewer reads and comments; it never writes
_HOST_KEYS = frozenset({"host", "host_id", "hostname", "machine", "pinned_host", "node"})


class TeamError(ContractError):
    """A template or task is refused; ``reasons`` says why, in terms safe to show."""

    def __init__(self, *reasons: str) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True)
class RoleSpec:
    name: str
    kind: str
    actions: frozenset = frozenset({"read", "post"})
    min_count: int = 1
    max_count: int = 1
    optional: bool = False


@dataclass(frozen=True)
class Budget:
    max_tasks: int
    max_runtime_s: int


@dataclass(frozen=True)
class TeamTemplate:
    template_id: str
    roles: tuple[RoleSpec, ...]
    budget: Budget

    def role(self, name: str) -> RoleSpec | None:
        return next((r for r in self.roles if r.name == name), None)


def template_from_dict(doc: Mapping) -> TeamTemplate:
    """Parse and validate. Refuses any host pinning, an unknown kind, anything beyond what an agent may do, a
    reviewer that can write, and a team without an implementer and exactly one independent reviewer."""
    if not isinstance(doc, Mapping):
        raise TeamError("template_not_an_object")
    bad = sorted(k for k in _walk_keys(doc) if k in _HOST_KEYS)
    if bad:
        raise TeamError("host_pinning_not_allowed:" + ",".join(bad))
    roles = []
    for raw in doc.get("roles") or ():
        kind = raw.get("kind")
        if kind not in KINDS:
            raise TeamError(f"unknown_role_kind:{kind}")
        actions = frozenset(raw.get("actions") or ("read", "post"))
        if not actions <= AGENT_ACTIONS:
            raise TeamError(f"role_exceeds_agent_authority:{raw.get('name')}")
        if kind == "reviewer" and not actions <= REVIEWER_ACTIONS:
            raise TeamError("reviewer_must_be_read_only")
        lo, hi = int(raw.get("min_count", 1)), int(raw.get("max_count", 1))
        if not 0 <= lo <= hi:
            raise TeamError(f"bad_role_count:{raw.get('name')}")
        roles.append(RoleSpec(str(raw["name"]), kind, actions, lo, hi, bool(raw.get("optional", False))))
    names = [r.name for r in roles]
    if len(set(names)) != len(names):
        raise TeamError("duplicate_role_name")
    if not any(r.kind == "implementer" for r in roles):
        raise TeamError("needs_an_implementer")
    if sum(r.kind == "reviewer" for r in roles) != 1:
        raise TeamError("needs_exactly_one_independent_reviewer")
    b = doc.get("budget") or {}
    budget = Budget(int(b.get("max_tasks", 0)), int(b.get("max_runtime_s", 0)))
    if budget.max_tasks <= 0 or budget.max_runtime_s <= 0:
        raise TeamError("budget_must_be_positive")
    return TeamTemplate(str(doc["template_id"]), tuple(roles), budget)


def _walk_keys(obj: Any) -> Iterable[str]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield str(k).lower()
            yield from _walk_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_keys(v)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    template_id: str
    role: str
    submitted_by: str                       # the authorized human
    room_id: str
    allowed_actions: frozenset
    max_runtime_s: int
    deadline: datetime
    origin: str = "actor"                   # "actor" (an authorized human's command) | "message" (text from the room)
    depends_on: tuple[str, ...] = ()
    reviews: str | None = None              # the task this one reviews
    executor: str = ""                      # agent instance that will run it (set at placement, not in the template)
    taint: frozenset = frozenset()
    idempotency_key: str = ""


def _acyclic(task_id: str, deps: tuple[str, ...], known: Mapping[str, TaskSpec]) -> bool:
    seen, stack = set(), list(deps)
    while stack:
        t = stack.pop()
        if t == task_id:
            return False
        if t in seen:
            continue
        seen.add(t)
        stack.extend(known[t].depends_on if t in known else ())
    return True


def admit(task: TaskSpec, template: TeamTemplate, broker: Broker, known: Mapping[str, TaskSpec],
          now: datetime | None = None) -> None:
    """Raise TeamError unless the task may run. Never raises for a task that is allowed."""
    now = now or datetime.now(UTC)
    why: list[str] = []
    if task.origin != "actor":
        why.append("task_must_come_from_an_authorized_actor_not_a_message")
    if task.taint:
        why.append("task_derived_from_untrusted_content")
    role = template.role(task.role)
    if task.template_id != template.template_id or role is None:
        raise TeamError("unknown_template_or_role")
    if not task.allowed_actions <= role.actions:
        why.append("allowed_actions_exceed_the_role")
    if task.max_runtime_s <= 0 or task.max_runtime_s > template.budget.max_runtime_s:
        why.append("runtime_outside_the_template_budget")
    if task.deadline <= now:
        why.append("deadline_in_the_past")
    if len(known) >= template.budget.max_tasks and task.task_id not in known:
        why.append("task_budget_exhausted")
    missing = [d for d in task.depends_on if d not in known]
    if missing:
        why.append("unknown_dependency:" + ",".join(sorted(missing)))
    elif not _acyclic(task.task_id, task.depends_on, known):
        why.append("dependency_cycle")
    if role.kind == "reviewer":
        target = known.get(task.reviews or "")
        if target is None:
            why.append("a_review_must_name_the_task_it_reviews")
        else:
            if not task.executor or task.executor in (target.executor, target.submitted_by):
                why.append("reviewer_is_not_independent_of_the_implementer")
    if why:
        raise TeamError(*why)
    decision = broker.authorize(ToolRequest("submit_task", task.submitted_by, room_id=task.room_id,
                                            taint=task.taint, idempotency_key=task.idempotency_key), now)
    if decision.outcome != "allow":
        raise TeamError("broker_" + decision.outcome, *decision.reasons)


# --------------------------------------------------------------------------- lease / fencing
class LeaseError(ContractError):
    pass


@dataclass
class Lease:
    task_id: str
    worker: str
    token: int
    expires_at: datetime
    actions: frozenset
    revoked: bool = False

    def live(self, now: datetime) -> bool:
        return not self.revoked and self.expires_at > now

    def authorizes(self, action: str, token: int, now: datetime) -> bool:
        """An aborted, expired or superseded lease authorizes nothing."""
        return self.live(now) and token == self.token and action in self.actions


@dataclass
class _Slot:
    next_token: int = 1
    lease: Lease | None = None
    result: Any = None
    done: bool = False
    done_token: int = 0


class LeaseStore:
    """In-memory reference implementation of the contract; the Postgres store must give the same answers."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def acquire(self, task: TaskSpec, worker: str, ttl_s: int = 60) -> Lease:
        with self._lock:
            now = self._clock()
            slot = self._slots.setdefault(task.task_id, _Slot())
            if slot.done:
                raise LeaseError("already_completed")
            if slot.lease is not None and slot.lease.live(now):
                raise LeaseError("held_by_another_worker")
            lease = Lease(task.task_id, worker, slot.next_token, now + timedelta(seconds=ttl_s), task.allowed_actions)
            slot.next_token += 1
            slot.lease = lease
            return lease

    def heartbeat(self, task_id: str, token: int, ttl_s: int = 60) -> Lease:
        with self._lock:
            lease = self._current(task_id, token)
            lease.expires_at = self._clock() + timedelta(seconds=ttl_s)
            return lease

    def complete(self, task_id: str, token: int, result: Any) -> Any:
        """Accept a result once, from the holder of the live token. A repeat from the same token returns the
        recorded result (a safe retry); anything else is fenced off."""
        with self._lock:
            slot = self._slots.get(task_id)
            if slot is None:
                raise LeaseError("unknown_task")
            if slot.done:
                if token == slot.done_token:
                    return slot.result
                raise LeaseError("already_completed_by_another_token")
            lease = self._current(task_id, token)
            slot.done, slot.result, slot.done_token = True, result, lease.token
            lease.revoked = True                      # nothing may act under it after completion
            return result

    def abort(self, task_id: str, token: int | None = None) -> None:
        with self._lock:
            slot = self._slots.get(task_id)
            if slot is not None and slot.lease is not None and (token is None or token == slot.lease.token):
                slot.lease.revoked = True

    def authorizes(self, task_id: str, token: int, action: str) -> bool:
        with self._lock:
            slot = self._slots.get(task_id)
            return bool(slot and slot.lease and slot.lease.authorizes(action, token, self._clock()))

    def _current(self, task_id: str, token: int) -> Lease:
        slot = self._slots.get(task_id)
        lease = slot.lease if slot else None
        if lease is None or token != lease.token:
            raise LeaseError("stale_fencing_token")
        if not lease.live(self._clock()):
            raise LeaseError("lease_expired_or_revoked")
        return lease
