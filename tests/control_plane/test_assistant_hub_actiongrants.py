"""Action grant / request / evidence state and storage."""
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore
from herdr_engineering.control_plane.assistant_hub.actiongrants import (
    ActionEvidence,
    ActionGrant,
    ActionRequestRow,
    GrantState,
    revoke,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _grant(gid: str = "g1", tenant: str = "t", user: str = "u", **kw) -> ActionGrant:
    base = dict(grant_id=gid, tenant_id=tenant, user_id=user, key_id="k1", verb="git.push",
                state=GrantState.ACTIVE, created_at=T0, activated_at=T0, expires_at=T0 + timedelta(days=30),
                confirmed_by="owner")
    base.update(kw)
    return ActionGrant(**base)


def _req(rid: str, tenant: str = "t", user: str = "u", **kw) -> ActionRequestRow:
    base = dict(request_id=rid, tenant_id=tenant, user_id=user, created_at=T0)
    base.update(kw)
    return ActionRequestRow(**base)


def _ev(eid: str, tenant: str = "t", user: str = "u", **kw) -> ActionEvidence:
    base = dict(evidence_id=eid, tenant_id=tenant, user_id=user, key_id="k1", outcome="approved",
                provenance="owner_api", created_at=T0)
    base.update(kw)
    return ActionEvidence(**base)


def test_policy_version_bump_resets_active_grant_to_shadow() -> None:
    g = _grant(policy_version=1)
    assert g.effective_state(T0, 1) is GrantState.ACTIVE
    assert g.effective_state(T0, 2) is GrantState.SHADOW
    assert _grant(state=GrantState.PROPOSED, policy_version=1).effective_state(T0, 2) is GrantState.SHADOW


def test_expiry_is_effective_without_a_sweeper() -> None:
    g = _grant()
    assert g.effective_state(T0 + timedelta(days=29), 1) is GrantState.ACTIVE
    assert g.effective_state(T0 + timedelta(days=30), 1) is GrantState.EXPIRED
    assert g.state is GrantState.ACTIVE            # nothing mutated it


def test_revoked_stays_revoked() -> None:
    g = revoke(_grant(), T0 + timedelta(days=1))
    assert g.state is GrantState.REVOKED and g.revoked_at == T0 + timedelta(days=1)
    assert g.effective_state(T0 + timedelta(days=90), 5) is GrantState.REVOKED
    assert revoke(g, T0 + timedelta(days=2)).revoked_at == T0 + timedelta(days=1)


def test_round_trip_through_snapshot() -> None:
    s = AssistantStore()
    s.put_action_grant(_grant())
    s.put_action_request(_req("r1", status="approved", decided_at=T0))
    s.add_action_evidence(_ev("e1"))
    fresh = AssistantStore()
    fresh.restore(json.loads(json.dumps(s.snapshot())))
    assert fresh.get_action_grant("g1", tenant_id="t", user_id="u") == _grant()
    assert fresh.get_action_grant("g1", tenant_id="t", user_id="u").state is GrantState.ACTIVE
    assert fresh.get_action_request("r1", tenant_id="t", user_id="u").decided_at == T0
    assert fresh.list_action_evidence("t", "u") == [_ev("e1")]


def test_round_trip_through_real_persist_file_stays_0600(tmp_path: Path) -> None:
    path = str(tmp_path / "store.json")
    s = AssistantStore(persist_path=path)
    s.put_action_grant(_grant())
    s.add_action_evidence(_ev("e1"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    fresh = AssistantStore()
    fresh.restore(json.loads(Path(path).read_text()))
    assert fresh.get_action_grant("g1", tenant_id="t", user_id="u") == _grant()
    assert len(fresh.list_action_evidence("t", "u")) == 1


def test_old_snapshot_without_new_keys_restores() -> None:
    s = AssistantStore()
    s.restore({"assistants": []})
    assert s.action_grants == {} and s.action_requests == {} and s.action_evidence == {} and s.policy_state == {}


def test_old_grant_row_missing_fields_uses_defaults() -> None:
    s = AssistantStore()
    s.restore({"action_grants": [{"grant_id": "g", "tenant_id": "t", "user_id": "u", "state": "active"}]})
    g = s.get_action_grant("g", tenant_id="t", user_id="u")
    assert g.state is GrantState.ACTIVE and g.policy_version == 1 and g.judge_version == ""


def test_evidence_cap_drops_oldest_and_survives_restore() -> None:
    s = AssistantStore()
    s.max_action_rows_per_tenant = 3
    for i in range(5):
        s.add_action_evidence(_ev(f"e{i}", created_at=T0 + timedelta(seconds=i)))
    s.add_action_evidence(_ev("other", tenant="t2"))
    assert [e.evidence_id for e in s.list_action_evidence("t", "u")] == ["e2", "e3", "e4"]
    assert len(s.list_action_evidence("t2", "u")) == 1
    fresh = AssistantStore()
    fresh.max_action_rows_per_tenant = 3
    fresh.restore(s.snapshot())
    fresh.add_action_evidence(_ev("e5", created_at=T0 + timedelta(seconds=9)))
    assert [e.evidence_id for e in fresh.list_action_evidence("t", "u")] == ["e3", "e4", "e5"]


def test_request_cap_drops_oldest_and_reput_keeps_position() -> None:
    s = AssistantStore()
    s.max_action_rows_per_tenant = 2
    s.put_action_request(_req("r0", created_at=T0))
    s.put_action_request(_req("r1", created_at=T0 + timedelta(seconds=1)))
    s.put_action_request(_req("r0", created_at=T0, status="approved"))     # update, not a new row
    assert len(s.list_action_requests("t", "u")) == 2
    s.put_action_request(_req("r2", created_at=T0 + timedelta(seconds=2)))
    assert s.get_action_request("r0", tenant_id="t", user_id="u") is None
    assert [r.request_id for r in s.list_action_requests("t", "u")] == ["r1", "r2"]


def test_default_caps_are_5000_and_2000() -> None:
    s = AssistantStore()
    assert s.max_action_rows_per_tenant == 5000 and s.max_action_grants_per_tenant == 2000


def test_grant_cap_raises_per_tenant() -> None:
    s = AssistantStore()
    s.max_action_grants_per_tenant = 2
    s.put_action_grant(_grant("a"))
    s.put_action_grant(_grant("b"))
    with pytest.raises(ValueError, match="action grants"):
        s.put_action_grant(_grant("c"))
    s.put_action_grant(_grant("a", state=GrantState.REVOKED))              # updating an existing grant is fine
    s.put_action_grant(_grant("d", tenant="t2"))


def test_listing_filters_status_key_and_sorts_by_created_at() -> None:
    s = AssistantStore()
    s.put_action_request(_req("late", created_at=T0 + timedelta(hours=1)))
    s.put_action_request(_req("early", created_at=T0, status="denied"))
    s.put_action_request(_req("other-user", user="x"))
    assert [r.request_id for r in s.list_action_requests("t", "u")] == ["early", "late"]
    assert [r.request_id for r in s.list_action_requests("t", "u", status="denied")] == ["early"]
    s.add_action_evidence(_ev("e1", key_id="a"))
    s.add_action_evidence(_ev("e2", key_id="b"))
    assert [e.evidence_id for e in s.list_action_evidence("t", "u", key_id="b")] == ["e2"]
    assert s.list_action_grants("t", "nobody") == []


def test_store_keeps_defensive_copies() -> None:
    s = AssistantStore()
    g = _grant()
    s.put_action_grant(g)
    g.state = GrantState.REVOKED
    s.get_action_grant("g1", tenant_id="t", user_id="u").state = GrantState.REVOKED
    s.list_action_grants("t", "u")[0].state = GrantState.REVOKED
    assert s.get_action_grant("g1", tenant_id="t", user_id="u").state is GrantState.ACTIVE


def test_shadow_started_is_persisted_first_seen_and_config_wins(tmp_path: Path) -> None:
    path = str(tmp_path / "store.json")
    s = AssistantStore(persist_path=path)
    assert s.ensure_shadow_started(T0) == T0
    assert s.ensure_shadow_started(T0 + timedelta(days=3)) == T0           # first seen sticks
    fresh = AssistantStore()
    fresh.restore(json.loads(Path(path).read_text()))
    assert fresh.ensure_shadow_started(T0 + timedelta(days=9)) == T0       # survives restart
    cfg = T0 - timedelta(days=5)
    assert fresh.ensure_shadow_started(T0, configured=cfg) == cfg


def test_get_is_scoped_to_tenant_and_user() -> None:
    s = AssistantStore()
    s.put_action_grant(_grant())
    s.put_action_request(_req("r1"))
    assert s.get_action_grant("g1", tenant_id="t2", user_id="u") is None
    assert s.get_action_grant("g1", tenant_id="t", user_id="x") is None
    assert s.get_action_grant("nope", tenant_id="t", user_id="u") is None
    assert s.get_action_request("r1", tenant_id="t2", user_id="u") is None
    assert s.get_action_request("r1", tenant_id="t", user_id="x") is None
    assert s.get_action_request("r1", tenant_id="t", user_id="u") is not None


def test_lists_are_scoped_across_tenants_with_same_user() -> None:
    s = AssistantStore()
    s.put_action_grant(_grant("a", tenant="t1"))
    s.put_action_grant(_grant("b", tenant="t2"))
    s.put_action_request(_req("r1", tenant="t1"))
    s.put_action_request(_req("r2", tenant="t2"))
    s.add_action_evidence(_ev("e1", tenant="t1"))
    s.add_action_evidence(_ev("e2", tenant="t2"))
    assert [g.grant_id for g in s.list_action_grants("t1", "u")] == ["a"]
    assert [r.request_id for r in s.list_action_requests("t2", "u")] == ["r2"]
    assert [e.evidence_id for e in s.list_action_evidence("t1", "u")] == ["e1"]


def test_request_cap_survives_restore() -> None:
    s = AssistantStore()
    s.max_action_rows_per_tenant = 3
    for i in range(3):
        s.put_action_request(_req(f"r{i}", created_at=T0 + timedelta(seconds=i)))
    fresh = AssistantStore()
    fresh.max_action_rows_per_tenant = 3
    fresh.restore(s.snapshot())
    fresh.put_action_request(_req("r3", created_at=T0 + timedelta(seconds=9)))
    assert [r.request_id for r in fresh.list_action_requests("t", "u")] == ["r1", "r2", "r3"]


def test_restore_trims_oversized_snapshot_oldest_first() -> None:
    big = AssistantStore()
    for i in range(5):
        big.put_action_request(_req(f"r{i}", created_at=T0 + timedelta(seconds=i)))
        big.add_action_evidence(_ev(f"e{i}", created_at=T0 + timedelta(seconds=i)))
        big.put_action_grant(_grant(f"g{i}"))
    small = AssistantStore()
    small.max_action_rows_per_tenant = 2
    small.restore(big.snapshot())
    assert [r.request_id for r in small.list_action_requests("t", "u")] == ["r3", "r4"]
    assert [e.evidence_id for e in small.list_action_evidence("t", "u")] == ["e3", "e4"]
    assert len(small.list_action_grants("t", "u")) == 5


def test_reput_under_another_tenant_is_refused() -> None:
    s = AssistantStore()
    s.put_action_request(_req("r1", tenant="t1"))
    with pytest.raises(ValueError, match="another tenant"):
        s.put_action_request(_req("r1", tenant="t2"))
    s.add_action_evidence(_ev("e1", tenant="t1"))
    with pytest.raises(ValueError, match="another tenant"):
        s.add_action_evidence(_ev("e1", tenant="t2"))
    assert s.get_action_request("r1", tenant_id="t1", user_id="u") is not None


def test_naive_datetimes_are_treated_as_utc() -> None:
    naive_exp = (T0 + timedelta(days=30)).replace(tzinfo=None)
    g = _grant(expires_at=naive_exp)
    assert g.effective_state(T0 + timedelta(days=31), 1) is GrantState.EXPIRED
    assert g.effective_state((T0 + timedelta(days=1)).replace(tzinfo=None), 1) is GrantState.ACTIVE
    s = AssistantStore()
    assert s.ensure_shadow_started(T0.replace(tzinfo=None)) == T0
    assert s.ensure_shadow_started(T0, configured=T0.replace(tzinfo=None)) == T0
