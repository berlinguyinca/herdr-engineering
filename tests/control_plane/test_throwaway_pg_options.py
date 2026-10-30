"""The throwaway cluster must not depend on /dev/shm (CI containers clean or shrink it under a running postmaster)."""
from pathlib import Path

from tests.e2e.pg import ThrowawayPostgres


def test_throwaway_postgres_keeps_dynamic_shared_memory_out_of_dev_shm(tmp_path):
    options = ThrowawayPostgres(Path("/nonexistent"), tmp_path)._options()
    assert "dynamic_shared_memory_type=mmap" in options and "listen_addresses=''" in options
