"""Spec 06: host capability inventory + pure compatibility (no mocks)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import build_cache_env as bce
from herdr_engineering import build_cache_inventory as inv
from herdr_engineering.build_cache_config import validate
from herdr_engineering.build_cache_storage import namespace_prefix

RUSTC_X86 = """rustc 1.97.0 (2d8144b78 2026-07-07)
binary: rustc
commit-hash: 2d8144b7880597b6e6d3dfd63a9a9efae3f533d3
commit-date: 2026-07-07
host: x86_64-unknown-linux-gnu
release: 1.97.0
LLVM version: 22.1.6
"""
RUSTC_ARM = RUSTC_X86.replace("x86_64", "aarch64")
RUSTC_MAC = RUSTC_X86.replace("x86_64-unknown-linux-gnu", "aarch64-apple-darwin")
RUSTC_OLD = RUSTC_X86.replace("1.97.0", "1.91.0").replace("22.1.6", "21.1.2")


def _cfg(**over):
    sec = {"enabled": True}
    sec.update(over)
    return validate(sec)


def _cap(tmp_path, host, vv, cfg=None, sccache=True):
    tools = bce.Tools(Path("/x/sccache") if sccache else None,
                      "sccache 0.19.0" if sccache else None,
                      Path("/x/rustc"), Path("/x/cargo"), vv)
    return inv.build_capability(cfg or _cfg(), host_id=host, tools=tools,
                                cache_dir=tmp_path / host / "sccache")


def test_record_fields(tmp_path):
    c = _cap(tmp_path, "h1", RUSTC_X86)
    assert (c.host_id, c.os, c.arch) == ("h1", "linux", "x86_64")
    assert c.rustc_version == "1.97.0" and len(c.toolchain_digest) == 64
    assert c.sccache_present and c.sccache_version == "sccache 0.19.0"
    assert c.compat_key == namespace_prefix(c.namespace, c.toolchain_digest, "linux", "x86_64")
    assert c.cache.size_cap_gib == _cfg().local.capacity_gib
    assert c.cache.used_bytes == 0 and c.cache.free_disk_bytes > 0
    assert not (tmp_path / "h1").exists()  # probing creates nothing


def test_used_bytes_counts_files(tmp_path):
    d = tmp_path / "h1" / "sccache"
    d.mkdir(parents=True)
    (d / "a").write_bytes(b"x" * 1000)
    assert _cap(tmp_path, "h1", RUSTC_X86).cache.used_bytes == 1000


def test_compatible_hosts_share_namespace(tmp_path):
    a, b = _cap(tmp_path, "a", RUSTC_X86), _cap(tmp_path, "b", RUSTC_X86)
    r = inv.compatibility(a, b)
    assert r.compatible and r.reason is inv.Reason.COMPATIBLE
    assert a.compat_key == b.compat_key  # same safe objects addressable


@pytest.mark.parametrize("other,reason", [
    (RUSTC_ARM, inv.Reason.ARCH_MISMATCH),
    (RUSTC_MAC, inv.Reason.OS_MISMATCH),
    (RUSTC_OLD, inv.Reason.TOOLCHAIN_MISMATCH),
])
def test_incompatible_misses(tmp_path, other, reason):
    a, b = _cap(tmp_path, "a", RUSTC_X86), _cap(tmp_path, "b", other)
    r = inv.compatibility(a, b)
    assert not r.compatible and r.reason is reason
    assert a.compat_key != b.compat_key
    assert inv.compatibility(b, a).reason is reason or not inv.compatibility(b, a).compatible


def test_namespace_mismatch(tmp_path):
    a = _cap(tmp_path, "a", RUSTC_X86)
    b = _cap(tmp_path, "b", RUSTC_X86, cfg=_cfg(shared_cache={"namespace": "other"}))
    r = inv.compatibility(a, b)
    assert not r.compatible and r.reason is inv.Reason.NAMESPACE_MISMATCH


def test_no_fingerprint_is_explicit_miss(tmp_path):
    a, b = _cap(tmp_path, "a", RUSTC_X86), _cap(tmp_path, "b", None)
    assert b.compat_key is None and b.toolchain_digest is None
    r = inv.compatibility(a, b)
    assert not r.compatible and r.reason is inv.Reason.NO_FINGERPRINT
    assert not inv.compatibility(b, b).compatible


def test_view_is_redacted(tmp_path):
    cfg = _cfg(shared_cache={"enabled": True, "endpoint_ref": "ep1",
                             "credentials_file": "/secret/creds.env"}, mode="shared_cache")
    text = json.dumps(inv.inventory_view(cfg, [_cap(tmp_path, "a", RUSTC_X86, cfg)]))
    assert "/secret" not in text and "creds" not in text and str(tmp_path) not in text
    assert json.loads(text)["hosts"][0]["policy"]["mode"] == "shared_cache"


def test_disabled_collects_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "st"))
    assert inv.collect(validate(None)) is None
    assert inv.inventory_view(validate(None), []) == {"enabled": False, "hosts": []}
    assert not (tmp_path / "st").exists()


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no real rustc on this host")
def test_collect_with_real_rustc(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "st"))
    c = inv.collect(_cfg(), path_env=os.environ["PATH"])
    assert c.os == "linux" and c.toolchain_digest and c.rustc_version
    assert inv.compatibility(c, inv.collect(_cfg(), path_env=os.environ["PATH"])).compatible
    assert not (tmp_path / "st").exists()


def test_cli_disabled_and_json(tmp_path):
    env = {**os.environ, "HERDR_ENGINEERING_STATE": str(tmp_path / "st"),
           "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c",
                        "import sys; from herdr_engineering.cli import main; "
                        "sys.exit(main(['build-cache-inventory', '--json']))"], capture_output=True, text=True, env=env,
                       cwd=str(Path(__file__).resolve().parents[1]))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"enabled": False, "hosts": []}
    assert not (tmp_path / "st").exists()
