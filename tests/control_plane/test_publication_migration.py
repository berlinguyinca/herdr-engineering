from pathlib import Path


def test_publication_migration_separates_approval_and_provider_state():
    path = Path(__file__).parents[2] / "deploy/control-plane/migrations/0004_artifact_publications.sql"
    sql = path.read_text()
    assert "CREATE TABLE publications" in sql
    assert "pinned_version_id" in sql
    assert "token_hash" in sql
    assert "desired_state" in sql
    assert "observed_state" in sql
    assert "CREATE TABLE publication_events" in sql
