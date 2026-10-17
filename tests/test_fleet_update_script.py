"""Legacy shell entry point must share the CLI's failure handling."""
import os
import subprocess
from pathlib import Path


def test_script_delegates_flags_and_propagates_failure(tmp_path):
    helper = tmp_path / 'python'
    helper.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 7\n')
    helper.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / 'scripts/fleet-self-update.sh'
    result = subprocess.run(['bash', str(script), '--host', 'example', '--no-pull',
                             '--no-local-reinstall'], capture_output=True, text=True,
                            env=dict(os.environ, HERDR_ENGINEERING_PYTHON=str(helper)), timeout=5)
    assert result.returncode == 7
    assert result.stdout.splitlines() == ['-m', 'herdr_engineering.cli', 'fleet', 'update',
                                          '--host', 'example', '--no-pull', '--no-reinstall']
