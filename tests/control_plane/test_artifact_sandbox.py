import sys

import pytest

from herdr_engineering.control_plane.artifact_sandbox import (
    ArtifactSandbox,
    SandboxError,
)


def test_sandbox_runs_allowlisted_command_with_scrubbed_environment(tmp_path):
    sandbox = ArtifactSandbox(allowed_executables={sys.executable}, timeout_seconds=2)
    result = sandbox.run([sys.executable, "-c", "import os; print(os.getenv('SECRET', 'none'))"],
                         workdir=tmp_path, env={"SECRET": "must-not-pass"})
    assert result.returncode == 0
    assert result.stdout.strip() == "none"
    assert result.timed_out is False


def test_sandbox_rejects_unallowlisted_commands_and_timeouts(tmp_path):
    sandbox = ArtifactSandbox(allowed_executables={sys.executable}, timeout_seconds=0.05)
    with pytest.raises(SandboxError, match="not allowlisted"):
        sandbox.run(["/bin/echo", "no"], workdir=tmp_path)
    result = sandbox.run([sys.executable, "-c", "import time; time.sleep(1)"], workdir=tmp_path)
    assert result.timed_out is True
    assert result.returncode is None


def test_sandbox_bounds_output(tmp_path):
    sandbox = ArtifactSandbox(allowed_executables={sys.executable}, max_output_bytes=16)
    result = sandbox.run([sys.executable, "-c", "print('x' * 100)"], workdir=tmp_path)
    assert result.output_truncated is True
    assert len(result.stdout.encode()) <= 16

