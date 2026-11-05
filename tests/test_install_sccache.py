"""scripts/install-sccache.sh against real tarballs built in tmp dirs (no mocks)."""
import hashlib
import os
import stat
import subprocess
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-sccache.sh"
TRIPLE = "x86_64-unknown-linux-musl"
ASSET = f"sccache-v0.19.0-{TRIPLE}.tar.gz"


def _make_asset(src: Path, version: str = "0.19.0") -> str:
    src.mkdir(parents=True, exist_ok=True)
    stage = src / "stage" / f"sccache-v0.19.0-{TRIPLE}"
    stage.mkdir(parents=True)
    exe = stage / "sccache"
    exe.write_text(f"#!/bin/sh\necho 'sccache {version}'\n")
    exe.chmod(0o755)
    (stage / "README").write_text("not installed")
    tar = src / ASSET
    with tarfile.open(tar, "w:gz") as t:
        t.add(stage, arcname=stage.name)
    return hashlib.sha256(tar.read_bytes()).hexdigest()


def _run(tmp: Path, digest: str | None, platform: str = "linux-x86_64", **extra):
    sums = tmp / "sums.txt"
    sums.write_text(f"# test\n{digest}  {ASSET}\n" if digest else "# empty\n")
    dest = tmp / "dest"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp / "home"), "HERDR_TEST_MODE": "1",
           "SCCACHE_INSTALL_DEST": str(dest), "SCCACHE_INSTALL_PLATFORM": platform,
           "SCCACHE_CHECKSUMS": str(sums), "SCCACHE_SOURCE_DIR": str(tmp / "src"), **extra}
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return r, dest


def test_good_checksum_installs_0755_only_binary(tmp_path):
    digest = _make_asset(tmp_path / "src")
    r, dest = _run(tmp_path, digest)
    assert r.returncode == 0, r.stderr
    exe = dest / "sccache"
    assert stat.S_IMODE(exe.stat().st_mode) == 0o755
    assert sorted(p.name for p in dest.iterdir()) == ["sccache"]
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True).stdout
    assert "0.19.0" in out


def test_bad_checksum_refuses_and_installs_nothing(tmp_path):
    _make_asset(tmp_path / "src")
    r, dest = _run(tmp_path, "0" * 64)
    assert r.returncode == 1 and "mismatch" in r.stderr
    assert not dest.exists() or not list(dest.iterdir())


def test_missing_pinned_checksum_fails_closed(tmp_path):
    _make_asset(tmp_path / "src")
    r, dest = _run(tmp_path, None)
    assert r.returncode == 1 and "no pinned SHA-256" in r.stderr
    assert not dest.exists()


def test_already_installed_new_enough_skips(tmp_path):
    digest = _make_asset(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()
    exe = dest / "sccache"
    exe.write_text("#!/bin/sh\necho 'sccache 0.20.1'\n")
    exe.chmod(0o755)
    r, _ = _run(tmp_path, digest)
    assert r.returncode == 0 and "skipping" in r.stdout
    assert "0.20.1" in exe.read_text()


def test_old_version_is_replaced(tmp_path):
    digest = _make_asset(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()
    exe = dest / "sccache"
    exe.write_text("#!/bin/sh\necho 'sccache 0.18.0'\n")
    exe.chmod(0o755)
    r, _ = _run(tmp_path, digest)
    assert r.returncode == 0 and "0.19.0" in exe.read_text()


def test_unknown_platform_refuses(tmp_path):
    digest = _make_asset(tmp_path / "src")
    r, dest = _run(tmp_path, digest, platform="freebsd-riscv64")
    assert r.returncode == 1 and "unsupported platform" in r.stderr
    assert not dest.exists()


def test_source_override_is_test_only(tmp_path):
    digest = _make_asset(tmp_path / "src")
    r, dest = _run(tmp_path, digest, HERDR_TEST_MODE="0")
    assert r.returncode == 1 and "test-only" in r.stderr
    assert not dest.exists()


def test_repo_checksums_cover_every_supported_platform():
    text = (SCRIPT.parent / "sccache-checksums.txt").read_text()
    for t in ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl",
              "x86_64-apple-darwin", "aarch64-apple-darwin"):
        assert f"sccache-v0.19.0-{t}.tar.gz" in text
    assert os.access(SCRIPT, os.X_OK)
