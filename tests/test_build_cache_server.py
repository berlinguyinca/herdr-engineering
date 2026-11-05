"""Shared-tier server start (pure rules) and endpoint trust; plus a real outage build.

The outage test uses the real sccache and cargo (skips when absent). Live rustfs
proofs are in test_build_cache_rustfs_live.py.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import build_cache_env as bce
from herdr_engineering import build_cache_server as srv
from herdr_engineering import build_cache_storage as bcs
from herdr_engineering.build_cache_config import validate
from tests.test_build_cache_storage import RUSTC_A, _cfg, _creds, _plan, _registry

SECRET = "sUp3rS3cretKeyValue/AbC123+xyz"


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:9000", True), ("127.5.5.5", True), ("localhost:9000", True),
    ("100.104.39.6:9000", True), ("100.64.0.1:9", True), ("100.127.255.254", True),
    ("bender.tail1234.ts.net:9000", True), ("bender:9000", True), ("bender", True),
    ("100.63.255.255:9000", False), ("100.128.0.1:9000", False), ("10.0.0.5:9000", False),
    ("192.168.1.2:9000", False), ("s3.amazonaws.com", False), ("example.org:9000", False),
    ("evil.ts.net.example.org", False), ("", False),
])
def test_trusted_plaintext_hosts(host, ok):
    assert bcs.trusted_plaintext_host(host) is ok


def _reg(tmp_path, host):
    return _registry(tmp_path, host=host, use_ssl=False)


@pytest.mark.parametrize("host", ["127.0.0.1:9000", "100.104.39.6:9000", "bender.x.ts.net:9000"])
def test_plaintext_to_loopback_and_tailnet_passes_validation(tmp_path, host):
    p = _plan(tmp_path, _cfg(tmp_path, registry=_reg(tmp_path, host)), reachable=True)
    assert p.shared_setup is not None
    assert p.shared.reason == "shared_unavailable:shared_requires_server" and not p.shared.active
    assert "use_ssl = false" in p.shared_setup.toml and "no_credentials = false" in p.shared_setup.toml


@pytest.mark.parametrize("host", ["s3.example.org:9000", "10.1.2.3:9000", "100.200.0.1:9000"])
def test_plaintext_to_public_hosts_still_needs_tls(tmp_path, host):
    p = _plan(tmp_path, _cfg(tmp_path, registry=_reg(tmp_path, host)), reachable=True)
    assert p.shared_setup is None and p.shared.reason == "shared_unavailable:tls_required"


def test_embedded_secret_in_config_still_fails_closed():
    with pytest.raises(Exception):
        validate({"enabled": True, "mode": "shared_cache",
                  "shared_cache": {"enabled": True, "endpoint_ref": "https://user:pw@h/b"}})


def test_unreachable_tailnet_address_degrades_with_the_same_reason(tmp_path):
    p = _plan(tmp_path, _cfg(tmp_path, registry=_reg(tmp_path, "100.64.0.1:9")), reachable=False)
    assert p.applied and p.shared_setup is None
    assert p.shared.reason == "shared_unavailable:endpoint_unreachable"


def test_server_env_holds_credentials_and_strips_ambient_settings(tmp_path):
    p = _plan(tmp_path, _cfg(tmp_path, registry=_reg(tmp_path, "127.0.0.1:9000")), reachable=True)
    ambient = {"PATH": "/usr/bin", "AWS_PROFILE": "other", "SCCACHE_BUCKET": "evil",
               "SCCACHE_ENDPOINT": "x", "RUSTC_WRAPPER": "sccache", "HOME": "/h"}
    env = srv.server_env(p.shared_setup, ambient, 24000)
    assert env["AWS_ACCESS_KEY_ID"] == p.shared_setup.credentials.access_key_id
    assert env["AWS_SECRET_ACCESS_KEY"] == p.shared_setup.credentials.secret_access_key
    assert env["SCCACHE_SERVER_PORT"] == "24000" and env["SCCACHE_CONF"].endswith("sccache.toml")
    assert env["SCCACHE_S3_RW_MODE"] == "READ_ONLY"  # writes not enabled
    assert env["SCCACHE_IDLE_TIMEOUT"] == str(srv.IDLE_TIMEOUT_S)
    assert not ({"AWS_PROFILE", "SCCACHE_BUCKET", "SCCACHE_ENDPOINT", "RUSTC_WRAPPER"} & set(env))
    rw = _plan(tmp_path, _cfg(tmp_path, writes=True, registry=_reg(tmp_path, "127.0.0.1:9000")),
               reachable=True)
    assert "SCCACHE_S3_RW_MODE" not in srv.server_env(rw.shared_setup, ambient, 24000)


def _stats(loc):
    return {"stats": {"multi_level": [{"name": "L0 (disk)", "location": "Local disk: /x"},
                                      {"name": "L1 (s3)", "location": loc}]}}


def test_remote_check_requires_the_expected_bucket_and_prefix():
    ok = _stats("s3, name: herdr-build-cache, prefix: /ns/linux-x86_64/abc/")
    assert srv.serves_expected_remote(ok, "herdr-build-cache", "ns/linux-x86_64/abc/")
    assert not srv.serves_expected_remote(ok, "herdr-build-cache", "other/linux-x86_64/abc/")
    assert not srv.serves_expected_remote(ok, "other-bucket", "ns/linux-x86_64/abc/")
    assert not srv.serves_expected_remote({"stats": {"multi_level": None}}, "b", "p/")
    assert not srv.serves_expected_remote(None, "b", "p/")
    assert not srv.serves_expected_remote({"stats": {"multi_level": [{"name": "L0", "location": "x"}]}},
                                          "b", "p/")


def test_fallback_port_is_distinct_and_in_range():
    for port in (20000, 25000, 31999):
        fb = srv.local_fallback_port(port)
        assert fb != port and 20000 <= fb < 32000


def test_ensure_with_nothing_listening_reports_a_code_not_active(tmp_path):
    p = _plan(tmp_path, _cfg(tmp_path, registry=_reg(tmp_path, "127.0.0.1:1")), reachable=True)
    out = bce.activate_shared(p, bce.Tools(tmp_path / "no-such-sccache", "sccache 0.19.0",
                                           tmp_path / "rustc", tmp_path / "cargo", RUSTC_A), {})
    assert not out.shared.active and out.shared.reason == "shared_unavailable:endpoint_unreachable"
    assert out.env_overrides["SCCACHE_SERVER_PORT"] != p.env_overrides["SCCACHE_SERVER_PORT"]
    assert SECRET not in repr(out)


def test_outage_build_succeeds_locally_with_real_sccache_and_cargo(tmp_path):
    sccache = shutil.which("sccache") or str(Path.home() / ".local/bin/sccache")
    if not Path(sccache).exists() or not shutil.which("cargo"):
        pytest.skip("real sccache and cargo are required")
    import yaml
    reg = _reg(tmp_path, "100.64.0.1:9")  # tailnet-shaped, nothing listens
    creds = _creds(tmp_path)
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "Cargo.toml").write_text('[package]\nname="o"\nversion="0.1.0"\nedition="2021"\n')
    (proj / "src/lib.rs").write_text("pub fn f() -> u8 { 1 }\n")
    conf = tmp_path / "c.yaml"
    conf.write_text(yaml.safe_dump({"fabric_build_cache": {
        "enabled": True, "mode": "shared_cache", "local": {"capacity_gib": 1},
        "shared_cache": {"enabled": True, "endpoint_ref": "build-cache", "namespace": "outage",
                         "writes_enabled": True, "credentials_file": str(creds),
                         "registry_file": str(reg)}}}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCCACHE_", "AWS_"))}
    env.update({"HERDR_ENGINEERING_CONFIG": str(conf), "HERDR_ENGINEERING_STATE": str(tmp_path / "st"),
                "CARGO_INCREMENTAL": "0", "CARGO_TARGET_DIR": str(proj / "target"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "PATH": f"{Path(sccache).parent}:{os.environ['PATH']}"})
    r = subprocess.run([sys.executable, "-c",
                        "import sys; from herdr_engineering.cli import main; sys.exit(main())",
                        "build-coordinate", "--record", "--slots", "2", "--max-load", "0", "--",
                        "cargo", "build", "-q"], cwd=proj, env=env, capture_output=True, text=True,
                       timeout=300)
    port = None
    try:
        assert r.returncode == 0, r.stderr
        ev = json.loads((tmp_path / "st/build-gates/builds.jsonl").read_text().splitlines()[-1])
        assert ev["cache"]["applied"] is True and ev["cache"]["shared"]["active"] is False
        assert ev["cache"]["shared"]["reason"] == "shared_unavailable:endpoint_unreachable"
        assert (proj / "target/debug").is_dir()
    finally:
        p = bce.plan(validate(json.loads(json.dumps(yaml.safe_load(conf.read_text())["fabric_build_cache"]))),
                     ["cargo", "build"], env, bce.detect_tools(env["PATH"]), proj,
                     tmp_path / "st/build-gates")
        port = int(p.env_overrides["SCCACHE_SERVER_PORT"])
        srv.stop_server(sccache, port)
