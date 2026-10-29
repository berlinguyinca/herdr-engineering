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

