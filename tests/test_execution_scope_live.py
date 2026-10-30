"""Opt-in real sandbox/cgroup revocation test, with no model calls."""
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.execution_scope import SystemdExecutionScope
from herdr_engineering.recovery import ExecutionLease


@pytest.mark.skipif(os.environ.get("HERDR_SCHEDULER_SCOPE_LIVE") != "1", reason="explicit live scope test")
def test_stopping_real_sandbox_scope_stops_workspace_writes():
    root = Path.home() / "herdr-agents" / "work"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = Path(__file__).resolve().parents[1] / "deploy/control-plane/collab-sandbox.sh"
    scope = SystemdExecutionScope(sandbox)
    lease = ExecutionLease("test-" + uuid4().hex, "local-test-host", 1, "test-token", False)
    with tempfile.TemporaryDirectory(prefix="scheduler-fence-test-", dir=root) as directory:
        marker = Path(directory) / "writes"
        child_marker = Path(directory) / "child-writes"
        child = "import time\nfrom pathlib import Path\np=Path('child-writes')\nwhile True:\n with p.open('a') as f: f.write('child\\n')\n time.sleep(.01)\n"
        program = ("import time,subprocess,sys\nfrom pathlib import Path\n"
                   f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
                   "p=Path('writes')\nwhile True:\n with p.open('a') as f: f.write('write\\n')\n time.sleep(.01)\n")
        try:
            receipt = scope.start(lease, workspace=Path(directory), argv=["/usr/bin/python3", "-c", program])
            deadline = time.monotonic() + 10
            while not (marker.exists() and child_marker.exists()) and time.monotonic() < deadline:
                time.sleep(.05)
            assert marker.exists() and child_marker.exists(), "sandbox process tree did not start"
            accounting = scope.accounting(receipt)
            assert accounting["source"] == "cgroup_v2"
            assert accounting["cpu_seconds"] > 0 and accounting["memory_bytes"] > 0
            result = scope.stop(lease, receipt)
            assert result["stop_verified"] is True
            after = marker.read_bytes()
            child_after = child_marker.read_bytes()
            time.sleep(.15)
            assert marker.read_bytes() == after
            assert child_marker.read_bytes() == child_after
        finally:
            subprocess.run(["systemctl", "--user", "stop", scope.unit_for(lease)],
                           capture_output=True, timeout=15)
