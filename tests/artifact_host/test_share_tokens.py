"""The host's copy of the public pages' design tokens equals the generated light tokens (no drift from the dashboard)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST_STATIC = ROOT / "herdr_artifact_host" / "share_static"
CP_STATIC = ROOT / "herdr_engineering" / "control_plane" / "share_static"
SYNC = ROOT / "scripts" / "sync_share_tokens.py"


def test_host_tokens_equal_the_control_planes_generated_tokens():
    assert (HOST_STATIC / "tokens.css").read_text() == (CP_STATIC / "tokens.css").read_text()


def test_sync_check_covers_the_host_copy(tmp_path):
    done = subprocess.run([sys.executable, str(SYNC), "--check"], capture_output=True)
    assert done.returncode == 0, done.stderr
    stale = tmp_path / "tokens.css"
    stale.write_text(":root {\n  --accent: #000000;\n}\n")
    done = subprocess.run(
        [
            sys.executable,
            str(SYNC),
            "--check",
            "--out",
            str(HOST_STATIC / "tokens.css"),
            "--out",
            str(stale),
        ],
        capture_output=True,
    )
    assert done.returncode == 1


def test_host_static_has_the_pages_the_share_code_reads():
    for name in (
        "rich-toggle.js",
        "share.css",
        "annotate.js",
        "viewer.html",
        "view.html",
        "tokens.css",
    ):
        assert (HOST_STATIC / name).is_file(), name
