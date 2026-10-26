from pathlib import Path


_MIGRATION = Path(__file__).parents[2] / "deploy" / "control-plane" / "migrations" / "0008_artifact_registry.sql"


def test_artifact_registry_migration_adds_versioned_authority_tables():
    sql = _MIGRATION.read_text()
    for table in (
        "artifact_blobs",
        "artifact_versions",
        "artifact_idempotency",
        "artifact_reviews",
        "artifact_comments",
        "artifact_dependencies",
        "artifact_build_jobs",
        "artifact_audit",
        "artifact_collections",
    ):
        assert f"CREATE TABLE {table}" in sql


def test_artifact_registry_migration_freezes_cas_and_search_indexes():
    sql = _MIGRATION.read_text()
    assert "UNIQUE (artifact_id, version_number)" in sql
    assert "base_version_id" in sql
    assert "idempotency_key" in sql
    assert "idx_artifact_versions_artifact" in sql
    assert "idx_artifact_audit_artifact" in sql

