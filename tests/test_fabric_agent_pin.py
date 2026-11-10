"""The fabric agent's PYTHONPATH drop-in must follow the release that was installed.

Why this file exists: the drop-in was hand-maintained, so `fleet update` left agents on an old
release and they logged thousands of HTTP 401s. scripts/fabric-agent-pin.sh now writes it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fabric-agent-pin.sh"
SHA = "4514f3154b502dbf5818967fd63ef4f88e1923e4"


def stage(tmp_path: Path, parent: str) -> Path:
    root = tmp_path / parent
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    return root


def run(root: Path, dropin: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / SCRIPT.name), str(dropin)],
        capture_output=True, text=True, check=False,
    )


def test_release_checkout_is_pinned(tmp_path: Path) -> None:
    root = stage(tmp_path, f"share/herdr/releases/{SHA}")
    dropin = tmp_path / "unit.d"
    assert run(root, dropin).returncode == 0
    assert (dropin / "services-source.conf").read_text() == (
        f'[Service]\nEnvironment="PYTHONPATH={root}"\n'
    )


def test_new_release_replaces_old_pin(tmp_path: Path) -> None:
    dropin = tmp_path / "unit.d"
    run(stage(tmp_path / "a", f"herdr/releases/{'9' * 40}"), dropin)
    new = stage(tmp_path / "b", f"herdr/releases/{SHA}")
    run(new, dropin)
    assert str(new) in (dropin / "services-source.conf").read_text()
    assert "9" * 40 not in (dropin / "services-source.conf").read_text()


def test_developer_checkout_is_not_pinned(tmp_path: Path) -> None:
    root = stage(tmp_path, "src/herdr-engineering")
    dropin = tmp_path / "unit.d"
    assert run(root, dropin).returncode == 0
    assert not (dropin / "services-source.conf").exists()


def test_missing_argument_fails(tmp_path: Path) -> None:
    root = stage(tmp_path, f"herdr/releases/{SHA}")
    result = subprocess.run(["bash", str(root / "scripts" / SCRIPT.name)], capture_output=True, text=True, check=False)
    assert result.returncode != 0
