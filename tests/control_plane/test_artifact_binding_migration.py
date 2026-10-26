from pathlib import Path


def test_binding_context_migration_extends_existing_binding_table():
    path = Path(__file__).parents[2] / "deploy/control-plane/migrations/0010_artifact_binding_context.sql"
    sql = path.read_text()
    assert "ALTER TABLE artifact_bindings" in sql
    for field in ("workspace_id", "room_id", "issue_ref", "commit_sha"):
        assert f"ADD COLUMN {field}" in sql
