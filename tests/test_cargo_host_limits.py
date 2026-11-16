"""Fault injection through real POSIX filesystem limits, without mocked writes."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_publication_failure_restores_original_configuration(tmp_path: Path):
    if os.name != "posix":
        pytest.skip("POSIX file-size limits required")
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("real Cargo required")
    home = tmp_path / "home"
    config = home / ".cargo/config.toml"
    config.parent.mkdir(parents=True)
    original = "[net]\nretry=4\n"
    config.write_text(original)
    profile = home / ".zshrc"
    large_profile = "# operator profile\n" * 200
    profile.write_text(large_profile)
    (home / ".zshrc.before-herdr-cargo-host").write_text(large_profile)
    env = {**os.environ, "HOME": str(home),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    code = ("import resource,signal,sys; "
            "signal.signal(signal.SIGXFSZ,signal.SIG_IGN); "
            "resource.setrlimit(resource.RLIMIT_FSIZE,(1024,resource.getrlimit(resource.RLIMIT_FSIZE)[1])); "
            "from herdr_engineering.cargo_host import main; "
            "sys.exit(main(['install','--real-cargo',sys.argv[1]]))")
    result = subprocess.run([sys.executable, "-c", code, cargo], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "File too large" in result.stderr
    assert config.read_text() == original
    assert profile.read_text() == large_profile
    assert not (home / ".local/bin/cargo").exists()
    assert not (home / ".local/share/herdr-engineering/cargo-host/install.json").exists()
