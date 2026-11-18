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


def test_policy_write_failure_preserves_previous_policy():
    class FailedSink:
        def record(self, event):
            pass

        def record_required(self, event):
            raise RuntimeError("storage unavailable")

    repo = ControlPlaneRepo(sink=FailedSink())
    repo.upsert_host("mac", "mac", None,
                     scheduler_policy={"mode": "manual_only", "revision": 1})
    with pytest.raises(RuntimeError, match="storage unavailable"):
        repo.set_host_policy("mac", mode="automatic", expected_revision=1)
    assert repo.get_host("mac")["scheduler_policy"] == {"mode": "manual_only", "revision": 1}

