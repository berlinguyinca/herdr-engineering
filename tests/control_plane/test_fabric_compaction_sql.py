"""The compaction move statement builds without a database."""
from herdr_engineering.control_plane.fabric_compaction import move_chunk_sql


def test_the_move_names_every_column_so_the_archive_order_never_has_to_match():
    sql = move_chunk_sql(["event_id", "trace_id", 'we"ird'])
    assert '("event_id", "trace_id", "we""ird", archived_at)' in sql
    assert 'SELECT moved."event_id", moved."trace_id", moved."we""ird", now() FROM moved' in sql
    assert "moved.*" not in sql
