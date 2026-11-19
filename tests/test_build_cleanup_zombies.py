"""A zombie, a thread-group leader and a vanished pid never fail the process scan (tests copied from the
``feat/disk-cleanup-command`` branch, which carries the same ``_gone_or_zombie``). Real processes."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering.build_cleanup import _managed_process, _process_paths


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real Linux /proc")
def test_a_zombie_under_a_herdr_named_parent_does_not_fail_the_scan(tmp_path: Path) -> None:
    herdr = tmp_path / "herdr"
    herdr.symlink_to(sys.executable)  # a process named herdr that never reaps its child
    code = ("import subprocess,time;c=subprocess.Popen(['sleep','60']);c.kill();"
            "print(c.pid,flush=True);time.sleep(60)")
    parent = subprocess.Popen([str(herdr), "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout is not None
        child = int(parent.stdout.readline())
        deadline = time.time() + 10
        while time.time() < deadline:
            state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.05)
        assert state == "Z"
        _process_paths()  # must skip the zombie, not raise
    finally:
        parent.kill()
        parent.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real Linux /proc")
def test_a_zombie_leader_with_a_live_thread_still_counts_its_cwd(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    code = ("import ctypes,threading,time\n"
            "threading.Thread(target=time.sleep,args=(60,),daemon=False).start()\n"
            "import os;print(os.getpid(),flush=True)\n"
            "ctypes.CDLL(None).pthread_exit(None)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=target, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        pid = int(proc.stdout.readline())
        deadline = time.time() + 10
        state = ""
        while time.time() < deadline:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.05)
        assert state == "Z"  # leader exited, the other thread still runs
        assert target in {p.resolve() for p in _process_paths()}
    finally:
        proc.kill()
        proc.wait()


def test_a_starting_pid_that_is_already_gone_is_not_managed(tmp_path: Path) -> None:
    assert _managed_process(tmp_path / "424242", tmp_path) is False
