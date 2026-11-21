"""The cleanup command's /proc view (``_process_paths``): a hidden Herdr descendant refuses, and no
identity lookups that can block on a hung mount. Real processes, real Linux /proc."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as bc
from herdr_engineering.cleanup_command import Refusal, in_use_snapshot

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real Linux /proc")

_NON_DUMPABLE = "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(120)"


def _refuses_fd_listing(parent: int) -> bool:
    try:
        children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
    except OSError:
        return False
    for child in children:
        try:
            os.listdir(f"/proc/{child}/fd")
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def _wait(predicate, what: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_hidden_herdr_descendant_refuses_the_cleanup_command(tmp_path, monkeypatch):
    herdr = tmp_path / "herdr"
    shutil.copy(shutil.which("sh"), herdr)
    proc = subprocess.Popen([str(herdr), "-c", f"{sys.executable} -c '{_NON_DUMPABLE}'; true"],
                            start_new_session=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("ps", "git"):  # no lsof: the /proc fallback is the path under test
        if found := shutil.which(tool):
            (bindir / tool).symlink_to(found)
    try:
        _wait(lambda: _refuses_fd_listing(proc.pid), "the non-dumpable child to refuse fd/")
        with pytest.raises(OSError):
            bc._process_paths()
        bc._scan()  # the sweep still does not abort (round 14)
        monkeypatch.setenv("PATH", str(bindir))
        with pytest.raises(Refusal, match="in_use"):
            in_use_snapshot()
    finally:
        proc.kill()
        proc.wait()


def test_process_paths_takes_no_identities_and_reads_no_maps(tmp_path, monkeypatch):
    holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], cwd=tmp_path)

    def boom(*_a, **_k):
        raise AssertionError("identity lookup (stat/maps) used by the cleanup command's view")

    monkeypatch.setattr(bc, "_link_ids", boom)
    monkeypatch.setattr(bc, "_mapped", boom)
    try:
        assert tmp_path.resolve() in {p.resolve() for p in bc._process_paths()}
    finally:
        holder.kill()
        holder.wait()
