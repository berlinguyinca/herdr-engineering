"""A real cargo for the cleanup tests: targets are built by cargo, never written by hand."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REAL_HOME = Path.home()


def cargo() -> Path:
    for candidate in (REAL_HOME / ".cargo/bin/cargo", shutil.which("cargo") or ""):
        path = Path(candidate) if candidate else None
        if path and path.is_file() and ".local/bin" not in str(path):
            return path
    pytest.skip("a real cargo is required")


def build(workspace: Path, target: Path) -> Path:
    """``cargo build`` of the crate at ``workspace`` into ``target`` (as cargo makes it, tag included)."""
    env = {**os.environ, "PATH": f"{cargo().parent}{os.pathsep}{os.environ['PATH']}",
           "CARGO_HOME": os.environ.get("CARGO_HOME", str(REAL_HOME / ".cargo")),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(REAL_HOME / ".rustup")),
           "CARGO_TARGET_DIR": str(target)}
    for key in ("RUSTC_WRAPPER", "HERDR_CARGO_HOST_COORDINATED"):
        env.pop(key, None)
    subprocess.run([str(cargo()), "build", "--offline"], cwd=workspace, env=env, check=True, capture_output=True)
    return target
