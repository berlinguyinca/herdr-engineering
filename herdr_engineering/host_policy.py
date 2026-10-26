"""Stable host identity and fail-closed placement policy primitives.

The scheduler deliberately keeps host eligibility independent from scoring.  A
host must first prove identity, ownership, trust, reachability and fresh
telemetry; only then can a placement request consider capacity or affinity.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class PolicyMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL_ONLY = "manual_only"
    DRAINING = "draining"
    DISABLED = "disabled"


@dataclass(frozen=True)
class HostPolicy:
    mode: PolicyMode = PolicyMode.AUTOMATIC
    revision: int = 0
    allow_tags: frozenset[str] = frozenset()
    deny_tags: frozenset[str] = frozenset()
    maintenance_until: datetime | None = None
    explicit_reason: str | None = None


@dataclass(frozen=True)
class HostSnapshot:
    host_uid: str
    host_name: str
    aliases: tuple[str, ...]
    platform: str
    owned: bool
    trusted: bool
    reachable: bool
    telemetry_at: datetime | None
    telemetry_freshness_seconds: float | None
    policy: HostPolicy = field(default_factory=HostPolicy)
    on_ac_power: bool | None = None


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason_codes: tuple[str, ...]
    policy_revision: int
    snapshot: HostSnapshot


def _clean_alias(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(".").lower()
    if not value or len(value) > 255 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        return None
    return value


def normalize_aliases(values: Iterable[object]) -> tuple[str, ...]:
    """Return deterministic, case-insensitive aliases with no empty entries."""
    result: list[str] = []
    for value in values:
        alias = _clean_alias(value)
        if alias and alias not in result:
            result.append(alias)
    return tuple(result)


def default_policy(*, platform: str, aliases: Iterable[object], on_ac_power: bool | None = None) -> HostPolicy:
    """Create a conservative default; portable macOS is never auto-enabled."""
    is_mac = platform.lower() in {"darwin", "macos", "mac"}
    reason = "portable macOS defaults to manual-only" if is_mac else None
    return HostPolicy(mode=PolicyMode.MANUAL_ONLY if is_mac else PolicyMode.AUTOMATIC,
                      explicit_reason=reason)


def stable_host_uid(machine_identity: str) -> str:
    """Derive a stable non-secret UID from an authenticated machine identity.

    Callers should persist the returned value with mode 0600.  Hostnames and
    aliases are intentionally not inputs, so renaming a machine cannot bypass
    policy.
    """
    if not isinstance(machine_identity, str) or not machine_identity.strip():
        raise ValueError("machine identity is required")
    digest = hashlib.sha256(machine_identity.strip().encode("utf-8")).hexdigest()
    return f"host-{digest[:32]}"


def verify_host_identity(stable_uid: str, host_name: str, aliases: Iterable[object], *, presented_uid: str | None = None) -> str:
    """Validate an authenticated UID; aliases only affect display resolution."""
    if not isinstance(stable_uid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", stable_uid):
        raise ValueError("stable host identity is invalid")
    if presented_uid is not None and presented_uid != stable_uid:
        raise ValueError("conflicting host identity")
    if _clean_alias(host_name) is None and not normalize_aliases(aliases):
        raise ValueError("host must have a valid alias")
    return stable_uid


def _fresh(snapshot: HostSnapshot, now: datetime) -> bool:
    if snapshot.telemetry_at is None or snapshot.telemetry_freshness_seconds is None:
        return False
    if snapshot.telemetry_at.tzinfo is None:
        return False
    age = max((now - snapshot.telemetry_at).total_seconds(), 0.0)
    reported_age = max(0.0, snapshot.telemetry_freshness_seconds)
    return reported_age <= 60.0 and age <= 60.0


def evaluate_eligibility(
    snapshot: HostSnapshot,
    *,
    placement_mode: str,
    explicit_host_uid: str | None = None,
    parent_host_uid: str | None = None,
    required_tags: Iterable[str] = (),
    now: datetime | None = None,
) -> Eligibility:
    """Evaluate hard policy constraints; parent placement is never implicit."""
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    explicit = placement_mode == "manual" and explicit_host_uid == snapshot.host_uid
    if explicit_host_uid is not None and explicit_host_uid != snapshot.host_uid:
        reasons.append("HOST_NOT_SELECTED")
    if not snapshot.owned:
        reasons.append("HOST_NOT_OWNED")
    if not snapshot.trusted:
        reasons.append("HOST_UNTRUSTED")
    if not snapshot.reachable:
        reasons.append("HOST_UNREACHABLE")
    if not _fresh(snapshot, now):
        reasons.append("HOST_STALE")
    if snapshot.policy.mode is PolicyMode.MANUAL_ONLY and not explicit:
        reasons.append("HOST_MANUAL_ONLY")
    if snapshot.policy.mode is PolicyMode.DISABLED:
        reasons.append("HOST_DISABLED")
    if snapshot.policy.mode is PolicyMode.DRAINING and not explicit:
        reasons.append("HOST_DRAINING")
    if snapshot.policy.maintenance_until and snapshot.policy.maintenance_until > now:
        reasons.append("HOST_MAINTENANCE")
    tags = {str(tag).strip().lower() for tag in required_tags if str(tag).strip()}
    if tags & snapshot.policy.deny_tags:
        reasons.append("HOST_POLICY_DENY")
    if snapshot.policy.allow_tags and not tags.issubset(snapshot.policy.allow_tags):
        reasons.append("HOST_POLICY_ALLOWLIST")
    if parent_host_uid == snapshot.host_uid and placement_mode != "manual":
        # This is represented by MANUAL_ONLY for a manual-only parent, but the
        # explicit reason prevents future callers from treating lineage as pin.
        reasons.append("PARENT_HOST_NOT_IMPLICIT")
    # An explicit host may bypass automatic-mode gating, never safety gates.
    safety = {"HOST_NOT_OWNED", "HOST_UNTRUSTED", "HOST_UNREACHABLE", "HOST_STALE", "HOST_DISABLED", "HOST_MAINTENANCE", "HOST_POLICY_DENY", "HOST_POLICY_ALLOWLIST"}
    eligible = not (set(reasons) & safety) and (explicit or snapshot.policy.mode is PolicyMode.AUTOMATIC)
    return Eligibility(eligible, tuple(dict.fromkeys(reasons)), snapshot.policy.revision, snapshot)

