import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.host_policy import PolicyMode


def test_host_scheduler_projection_is_conservative_until_trusted_registration():
    repo = ControlPlaneRepo()
    repo.upsert_host("mac", "mac.lan", "100.0.0.1", platform="darwin")
    snapshot = repo.host_snapshot("mac")
    assert snapshot.host_uid == "mac"
    assert not snapshot.owned
    assert not snapshot.trusted
    assert snapshot.policy.mode is PolicyMode.MANUAL_ONLY


def test_host_policy_uses_revision_and_records_audit_event():
    repo = ControlPlaneRepo()
    repo.upsert_host(
        "beast", "beast", "100.0.0.2", host_uid="uid-beast", aliases=["beast"],
        platform="linux", scheduler_owned=True, scheduler_trusted=True,
        scheduler_policy={"mode": "automatic", "revision": 0},
    )
    updated = repo.set_host_policy(
        "beast", mode="draining", expected_revision=0, reason="maintenance",
    )
    assert updated["mode"] == "draining" and updated["revision"] == 1
    assert repo.stream("host", "beast")[-1]["event_type"] == "HostPolicyChanged"
    with pytest.raises(ValueError, match="revision conflict"):
        repo.set_host_policy("beast", mode="automatic", expected_revision=0)

