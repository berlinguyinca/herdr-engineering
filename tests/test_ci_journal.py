import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "ci"


def run_gate(tmp_path, body, ci=True):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "journal.sh", tmp_path / "scripts" / "journal.sh")
    (tmp_path / "scripts" / "log-archive.yml").write_text(f"directories:\n  - {tmp_path}/archive/ci-logs\n")
    gate = tmp_path / "gate.sh"
    gate.write_text(f'set -euo pipefail\n. {tmp_path}/scripts/journal.sh; ci_journal demo\n{body}\n')
    env = {"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path)}
    if ci:
        env.update(CI_PIPELINE_NUMBER="7", CI_REPO="o/r")
    (tmp_path / "archive").mkdir()
    return subprocess.run(["bash", str(gate)], env=env, capture_output=True, text=True)


def test_ci_run_keeps_its_output_in_the_archive_even_when_killed(tmp_path):
    result = run_gate(tmp_path, "echo before; echo oops >&2; kill -9 $$")
    assert result.returncode != 0
    saved = (tmp_path / "archive" / "ci-logs" / "o_r" / "7-demo.log").read_text()
    assert "before" in saved and "oops" in saved
    assert "journal: " + str(tmp_path / "archive" / "ci-logs" / "o_r" / "7-demo.log") in result.stdout


def test_outside_ci_the_journal_is_a_local_temp_file(tmp_path):
    result = run_gate(tmp_path, "echo hi", ci=False)
    assert result.returncode == 0
    assert not (tmp_path / "archive" / "ci-logs").exists()
    assert f"journal: {tmp_path}/herdr-demo-" in result.stdout
