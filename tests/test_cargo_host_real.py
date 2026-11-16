"""Real Cargo/rustc/sccache acceptance; no compiler or cache substitutes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from herdr_engineering.build_cache_config import validate
from herdr_engineering.build_cache_env import fingerprint_from_text, server_port
from herdr_engineering.cargo_host import install


@pytest.mark.integration
def test_two_real_builds_share_compiler_cache_and_keep_separate_targets(tmp_path: Path):
    cargo, rustc, sccache = (shutil.which(name) for name in ("cargo", "rustc", "sccache"))
    if not all((cargo, rustc, sccache)):
        pytest.skip("real cargo, rustc and sccache are required")
    assert cargo is not None and rustc is not None and sccache is not None
    home = tmp_path / "home"
    install(home, Path(cargo), Path(sys.executable))
    settings = {"fabric_build_cache": {"enabled": True,
                "local": {"capacity_gib": 1, "min_free_disk_percent": 0}}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(settings))
    state = tmp_path / "state"
    env = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "RUSTC": rustc, "HERDR_ENGINEERING_CONFIG": str(config_path),
           "HERDR_ENGINEERING_STATE": str(state),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "PATH": str(home / ".local/bin") + os.pathsep + os.environ["PATH"]}
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "SCCACHE_DIR",
                "SCCACHE_SERVER_PORT", "CARGO_TARGET_DIR", "HERDR_CARGO_HOST_COORDINATED"):
        env.pop(key, None)
    shared = tmp_path / "shared"
    (shared / "src").mkdir(parents=True)
    (shared / "Cargo.toml").write_text('[package]\nname="herdr-cache-shared"\nversion="0.1.0"\nedition="2021"\n')
    (shared / "src/lib.rs").write_text("pub fn answer() -> u32 { 42 }\n")
    for name in ("first", "second", "direct", "directenv"):
        project = tmp_path / name
        (project / "src").mkdir(parents=True)
        (project / "Cargo.toml").write_text(
            f'[package]\nname="{name}"\nversion="0.1.0"\nedition="2021"\n'
            '[dependencies]\nherdr-cache-shared={path="../shared"}\n')
        (project / "src/main.rs").write_text('fn main() { println!("{}", herdr_cache_shared::answer()); }\n')
        entry = cargo if name.startswith("direct") else str(home / ".local/bin/cargo")
        build_env = {**env, "RUSTC_WRAPPER": str(home / ".local/share/herdr-engineering/cargo-host/rustc-cache")} if name == "directenv" else env
        result = subprocess.run([entry, "build", "--offline",
                                 "--target-dir", str(tmp_path / f"target-{name}")],
                                cwd=project, env=build_env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / f"target-{name}/debug/{name}").is_file()
    cfg = validate(settings["fabric_build_cache"])
    vv = subprocess.check_output([rustc, "-vV"], env=env, text=True)
    fingerprint = fingerprint_from_text(vv)
    assert fingerprint is not None
    cache_dir = state / "build-gates/sccache"
    port = server_port(cfg, fingerprint, cache_dir)
    cache_env = {**env, "SCCACHE_SERVER_PORT": str(port), "SCCACHE_DIR": str(cache_dir)}
    try:
        stats = json.loads(subprocess.check_output(
            [sccache, "--show-stats", "--stats-format", "json"], env=cache_env, text=True))
        assert stats["stats"]["cache_hits"]["counts"].get("Rust", 0) >= 1, stats
        events = [json.loads(line) for line in (state / "build-gates/builds.jsonl").read_text().splitlines()]
        assert sum(event.get("cache", {}).get("applied") is True for event in events) >= 2
    finally:
        subprocess.run([sccache, "--stop-server"], env=cache_env, capture_output=True, timeout=10)
