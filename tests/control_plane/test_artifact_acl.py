from herdr_engineering.control_plane.artifact_acl import ArtifactAcl


def test_acl_is_private_by_default_and_grants_expire():
    now = [100.0]
    acl = ArtifactAcl(now=lambda: now[0])
    assert acl.can("artifact_1", "agent:1", "read") is False
    acl.grant("artifact_1", "agent:1", {"read", "edit"}, expires_at=101.0)
    assert acl.can("artifact_1", "agent:1", "read") is True
    assert acl.can("artifact_1", "agent:1", "share") is False
    now[0] = 102.0
    assert acl.can("artifact_1", "agent:1", "read") is False


def test_acl_revoke_removes_only_one_principal_grant():
    acl = ArtifactAcl()
    acl.grant("artifact_1", "agent:1", {"read"})
    acl.grant("artifact_1", "agent:2", {"read"})
    acl.revoke("artifact_1", "agent:1")
    assert acl.can("artifact_1", "agent:1", "read") is False
    assert acl.can("artifact_1", "agent:2", "read") is True



def test_collab_principals_cannot_be_granted_through_the_web_acl():
    import pytest
    acl = ArtifactAcl()
    for pid in ("collab:x", "collab-invite:y"):
        with pytest.raises(ValueError, match="collaborators route"):
            acl.grant("artifact_1", pid, {"read"})
    assert acl.has_grants("artifact_1") is False


def test_load_skips_collab_rows_and_keeps_legacy_open_rule():
    acl = ArtifactAcl()
    acl.load([{"artifact_id": "a", "principal_id": "collab:x", "permissions": ["read"]},
              {"artifact_id": "a", "principal_id": "collab-invite:z", "permissions": ["read"]}])
    assert acl.has_grants("a") is False
    assert acl.principals_with("a", "read") == []
    acl.load([{"artifact_id": "a", "principal_id": "agent:1", "permissions": ["read"]}])
    assert acl.has_grants("a") is True


def test_load_acl_query_excludes_collab_rows():
    import asyncio

    from herdr_engineering.control_plane.artifacts import ArtifactStore

    seen = []

    class Conn:
        async def fetch(self, sql, *args):
            seen.append(sql)
            return []

    assert asyncio.run(ArtifactStore(Conn(), None).load_acl()) == []
    assert "principal_id NOT LIKE 'collab:%'" in seen[0]
    assert "principal_id NOT LIKE 'collab-invite:%'" in seen[0]


def test_acl_routes_reject_collab_principals_with_400():
    from tests.control_plane.test_artifact_access import call, principal, running
    with running() as server:
        server.artifact_acl.grant('private', principal('owner'), {'admin'})
        for path in ('/api/v1/artifacts/private/acl', '/api/v1/artifacts/private/acl/revoke'):
            for pid in ('collab:x', 'collab-invite:y'):
                assert call(server, 'POST', path, {'principal_id': pid, 'permissions': ['read']})[0] == 400
        assert server.artifact_acl.has_grants('private') is True
        assert server.artifact_acl.principals_with('private', 'read') == [principal('owner')]


def test_room_user_principals_are_web_grants_but_agent_collaborators_stay_reserved():
    import pytest

    from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
    acl = ArtifactAcl()
    acl.grant("a1", "collab:user:bob", {"read"}, granted_by="room:r1")
    for reserved in ("collab:agent1", "collab-invite:i1"):
        with pytest.raises(ValueError):
            acl.grant("a1", reserved, {"read"})
    fresh = ArtifactAcl()
    fresh.load([{"artifact_id": "a1", "principal_id": "collab:user:bob", "permissions": ["read"], "granted_by": "room:r1"},
                {"artifact_id": "a1", "principal_id": "collab:agent1", "permissions": ["edit"]}])
    assert fresh.has_grants("a1")
    assert [g["principal_id"] for g in fresh.grants_with_source_prefix("room:")] == ["collab:user:bob"]
    assert not any(g.get("principal_id") == "collab:agent1" for g in fresh._grants.values())


def test_expiring_grants_survive_a_store_round_trip():
    """The store hands timestamps back as ISO strings (datetime or text); can/held/principals_with must still decide."""
    from datetime import datetime, timezone
    now = [100.0]
    acl = ArtifactAcl(now=lambda: now[0])
    future = datetime.fromtimestamp(1000.0, tz=timezone.utc)
    acl.load([{"artifact_id": "a1", "principal_id": "u1", "permissions": ["read"], "expires_at": future.isoformat()},
              {"artifact_id": "a1", "principal_id": "u2", "permissions": ["read"], "expires_at": future}])
    assert acl.can("a1", "u1", "read") and acl.held("a1", "u2") is not None
    assert acl.principals_with("a1", "read") == ["u1", "u2"]
    now[0] = 2000.0
    assert acl.can("a1", "u1", "read") is False and acl.principals_with("a1", "read") == []


def test_grant_normalises_an_iso_expiry_and_rejects_nonsense():
    import pytest
    acl = ArtifactAcl(now=lambda: 0.0)
    grant = acl.grant("a1", "u1", {"read"}, expires_at="1970-01-01T00:16:40+00:00")
    assert grant["expires_at"] == 1000.0 and acl.can("a1", "u1", "read")
    for bad in ("tomorrow", [1], True):
        with pytest.raises(ValueError, match="expires_at"):
            acl.grant("a1", "u2", {"read"}, expires_at=bad)


def test_concurrent_grants_and_reads_do_not_corrupt_the_maps():
    import threading
    acl = ArtifactAcl()
    errors = []

    def writer(n):
        try:
            for i in range(300):
                acl.grant("a1", f"u{n}-{i}", {"read"})
                acl.revoke("a1", f"u{n}-{i}")
        except Exception as exc:        # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(300):
                acl.has_grants("a1")
                acl.principals_with("a1", "read")
                acl.grants_by("x")
                acl.grants_with_source_prefix("room:")
        except Exception as exc:        # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)] + [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
