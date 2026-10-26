from datetime import UTC, datetime

import pytest

from herdr_engineering.host_policy import (
    HostPolicy,
    HostSnapshot,
    PolicyMode,
    default_policy,
    evaluate_eligibility,
    normalize_aliases,
    verify_host_identity,
)


def host(**overrides):
    data = dict(
        host_uid="uid-beast",
        host_name="beast",
        aliases=("beast", "beast.lan"),
        platform="linux",
        owned=True,
        trusted=True,
        reachable=True,
        telemetry_at=datetime.now(UTC),
        telemetry_freshness_seconds=5,
        policy=HostPolicy(mode=PolicyMode.AUTOMATIC),
    )
    data.update(overrides)
    return HostSnapshot(**data)


def test_mac_default_policy_is_manual_only_even_when_on_ac_power():
    policy = default_policy(platform="darwin", aliases=["mac.lan", "macbook-pro"], on_ac_power=True)
    assert policy.mode is PolicyMode.MANUAL_ONLY
    assert policy.explicit_reason


def test_aliases_are_normalized_without_creating_multiple_identities():
    assert normalize_aliases(["MacBook-Pro.", "mac.lan", "MACBOOK-PRO", ""]) == (
        "macbook-pro",
        "mac.lan",
    )
    assert verify_host_identity("uid-1", "macbook-pro", ["mac.lan", "MacBook-Pro"]) == "uid-1"


def test_conflicting_stable_identity_fails_closed():
    with pytest.raises(ValueError, match="conflicting host identity"):
        verify_host_identity("uid-1", "beast", ["beast"], presented_uid="uid-2")


def test_unknown_stale_untrusted_and_not_owned_hosts_are_rejected():
    cases = [
        (dict(trusted=False), "HOST_UNTRUSTED"),
        (dict(owned=False), "HOST_NOT_OWNED"),
        (dict(reachable=False), "HOST_UNREACHABLE"),
        (dict(telemetry_at=None), "HOST_STALE"),
        (dict(telemetry_freshness_seconds=61), "HOST_STALE"),
    ]
    for changes, reason in cases:
        result = evaluate_eligibility(host(**changes), placement_mode="automatic", now=datetime.now(UTC))
        assert reason in result.reason_codes
        assert not result.eligible


def test_mac_never_becomes_eligible_from_parent_or_power_state():
    snapshot = host(
        host_uid="uid-mac",
        host_name="mac.lan",
        aliases=("mac.lan", "macbook-pro"),
        platform="darwin",
        policy=default_policy(platform="darwin", aliases=["mac.lan"]),
    )
    result = evaluate_eligibility(snapshot, placement_mode="automatic", parent_host_uid="uid-mac")
    assert not result.eligible
    assert "HOST_MANUAL_ONLY" in result.reason_codes


def test_manual_selection_can_use_manual_only_but_not_disabled_and_never_inherited():
    manual = evaluate_eligibility(
        host(
            host_uid="uid-mac",
            host_name="mac.lan",
            platform="darwin",
            policy=HostPolicy(mode=PolicyMode.MANUAL_ONLY),
        ),
        placement_mode="manual",
        explicit_host_uid="uid-mac",
    )
    assert manual.eligible
    inherited = evaluate_eligibility(
        manual.snapshot,
        placement_mode="automatic",
        parent_host_uid="uid-mac",
    )
    assert not inherited.eligible


def test_deny_always_wins_and_policy_revision_is_exposed():
    snapshot = host(
        policy=HostPolicy(
            mode=PolicyMode.AUTOMATIC,
            revision=7,
            deny_tags=frozenset({"coding"}),
            allow_tags=frozenset({"coding"}),
        )
    )
    result = evaluate_eligibility(snapshot, placement_mode="automatic", required_tags={"coding"})
    assert not result.eligible
    assert "HOST_POLICY_DENY" in result.reason_codes
    assert result.policy_revision == 7
