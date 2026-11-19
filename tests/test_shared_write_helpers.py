"""The shared write helpers keep their behaviour for every caller except one change: a symlinked file is written through to its
target. A read-only file is still replaced for them; only the two hook-migration installers refuse it."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from herdr_engineering import fabric_join_setup as fj
from herdr_engineering import memory_install as mi

WRITERS = {"fabric": fj._write_text, "memory": mi._atomic_write}
pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")


@pytest.fixture(params=sorted(WRITERS))
def write(request):
    return WRITERS[request.param]


def test_a_read_only_file_in_a_writable_directory_is_still_replaced(tmp_path: Path, write) -> None:
    target = tmp_path / "f.json"
    target.write_text("old")
    target.chmod(0o444)
    write(target, "new")
    assert target.read_text() == "new"


def test_a_symlink_is_written_through_and_stays_a_link(tmp_path: Path, write) -> None:
    real = tmp_path / "dot" / "real.json"
    real.parent.mkdir()
    real.write_text("old")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    write(link, "new")
    assert link.is_symlink() and real.read_text() == "new"


def test_a_read_only_directory_still_raises_the_plain_permission_error(tmp_path: Path, write) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    (d / "f").write_text("old")
    d.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            write(d / "f", "new")
    finally:
        d.chmod(0o755)
    assert (d / "f").read_text() == "old"
