"""Action grants, action requests and approval evidence for the assistant hub (state only, no decisions).

A grant is a per-user permission for one canonical action key. Its stored state is never trusted on its own:
``effective_state`` derives expiry and policy-version resets from the clock and the current policy, so nothing
depends on a sweeper having run. All fields have defaults so older snapshots restore.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import UTC, datetime

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def as_utc(value: datetime) -> datetime:
    """A naive datetime is taken to be UTC, so comparisons never raise TypeError."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class GrantState(enum.StrEnum):
    SHADOW = "shadow"
    PROPOSED = "proposed"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class ActionGrant:
    grant_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    worker_id: str = ""
    resource_scope: str = ""
    key_id: str = ""
    verb: str = ""
    key_canonical: str = ""
    state: GrantState = GrantState.SHADOW
    created_at: datetime = _EPOCH
    proposed_at: datetime | None = None
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    policy_version: int = 1
    judge_version: str = ""
    confirmed_by: str = ""

    def __post_init__(self) -> None:
        self.state = GrantState(self.state)      # a restored snapshot carries the plain string

    def effective_state(self, now: datetime, policy_version: int) -> GrantState:
        """The state that governs decisions at ``now`` under the current ``policy_version``."""
        now = as_utc(now)
        if self.state is GrantState.REVOKED:
            return GrantState.REVOKED
        if self.state is GrantState.ACTIVE and self.expires_at is not None and now >= as_utc(self.expires_at):
            return GrantState.EXPIRED
        if self.state in (GrantState.PROPOSED, GrantState.ACTIVE) and self.policy_version < policy_version:
            return GrantState.SHADOW             # a policy change sends earned trust back to observation
        if self.state is GrantState.ACTIVE and (self.activated_at is None or not self.confirmed_by
                                                or self.expires_at is None):
            return GrantState.SHADOW             # an ACTIVE row without its activation facts is not trusted
        return self.state


def revoke(grant: ActionGrant, now: datetime) -> ActionGrant:
    """A revoked copy; revocation is terminal and wins over every other state."""
    return replace(grant, state=GrantState.REVOKED, revoked_at=grant.revoked_at or now)


@dataclass
class ActionRequestRow:
    request_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    worker_id: str = ""
    session_id: str = ""
    task_id: str = ""
    key_canonical: str = ""
    key_id: str = ""
    verb: str = ""
    risk_class: str = ""
    status: str = "pending"          # pending | approved | denied | vetoed
    reason: str = ""
    created_at: datetime = _EPOCH
    decided_at: datetime | None = None
    decided_by: str = ""
    display: str = ""                # scrubbed, truncated command for the owner API only; never audit/grants/events


@dataclass
class ActionEvidence:
    evidence_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    worker_id: str = ""
    session_id: str = ""
    task_id: str = ""
    key_id: str = ""
    outcome: str = ""                # approved | denied
    provenance: str = ""             # owner_api | worker_reported
    created_at: datetime = _EPOCH
