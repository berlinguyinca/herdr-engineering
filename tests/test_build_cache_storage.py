"""Spec 04 (central store adapter): registry, namespace, credentials, sccache TOML, health.

Real local TLS servers/sockets (self-signed cert via openssl) and fake
sccache/cargo executables for env wiring. No S3 server exists here: these tests
prove configuration, isolation and fallback, NOT cache hits or corrupt-entry
handling (that needs a real sccache and a real endpoint).
"""
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import textwrap
import threading
import tomllib
from pathlib import Path

import pytest

from herdr_engineering import build_cache_env as bce
from herdr_engineering import build_cache_storage as bcs
from herdr_engineering.build_cache_config import validate
from herdr_engineering.build_cache_storage import Code, Endpoint, StorageError
from herdr_engineering.build_coord import run_gated

SECRET = "sUp3rS3cretKeyValue/AbC123+xyz"
KEY_ID = "AKIATESTKEYID12345"
TOKEN = "session-token-VALUE-987"
RUSTC_A = """rustc 1.97.0 (2d8144b78 2026-07-07)
binary: rustc
commit-hash: 2d8144b7880597b6e6d3dfd63a9a9efae3f533d3
commit-date: 2026-07-07
host: x86_64-unknown-linux-gnu
release: 1.97.0
LLVM version: 22.1.6
"""
RUSTC_B = RUSTC_A.replace("1.97.0", "1.91.0").replace("2d8144b7880597b6e6d3dfd63a9a9efae3f533d3",
                                                      "f" * 40)


def _private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _registry(tmp_path, host="s3.example.org:9000", **over) -> Path:
    entry = {"host": host, "region": "us-east-1", "bucket": "sccache-bucket", **over}
    lines = ["endpoints:", "  build-cache:"] + [f"    {k}: {json.dumps(v)}" for k, v in entry.items()]
    return _private(tmp_path / "reg/registry.yaml", "\n".join(lines) + "\n")


def _creds(tmp_path, token=True) -> Path:
    text = f"access_key_id: {KEY_ID}\nsecret_access_key: '{SECRET}'\n"
    if token:
        text += f"session_token: {TOKEN}\n"
    return _private(tmp_path / "reg/creds.yaml", text)


def _cfg(tmp_path, writes=False, registry=True, creds=True, mode="shared_cache", tls=True,
         ref="build-cache", **shared):
    sc = {"enabled": True, "endpoint_ref": ref, "writes_enabled": writes,
          "tls_required": tls, **shared}
    if registry is True:
        registry = _registry(tmp_path)
    if creds is True:
        creds = _creds(tmp_path)
    if registry:
        sc["registry_file"] = str(registry)
    if creds:
        sc["credentials_file"] = str(creds)
    return validate({"enabled": True, "mode": mode, "shared_cache": sc})


def _tools(tmp_path, version="sccache 0.19.0"):
    return bce.Tools(tmp_path / "sccache", version, tmp_path / "rustc", tmp_path / "cargo", RUSTC_A)


def _plan(tmp_path, cfg, reachable=None, tools=None, rustc=RUSTC_A):
    t = tools or _tools(tmp_path)
    t = bce.Tools(t.sccache, t.sccache_version, t.rustc, t.cargo, rustc)
    return bce.plan(cfg, ["cargo", "build"], {}, t, tmp_path, tmp_path / "state", reachable)


# --- namespace prefix ---------------------------------------------------------

D1, D2 = "a" * 64, "b" * 64


def test_prefix_shape_and_isolation():
    p = bcs.namespace_prefix("trusted-dev", D1, "linux", "x86_64")
    assert p == "trusted-dev/linux-x86_64/" + "a" * 16 + "/"
    others = {bcs.namespace_prefix("trusted-dev", D2, "linux", "x86_64"),
              bcs.namespace_prefix("trusted-dev", D1, "darwin", "x86_64"),
              bcs.namespace_prefix("trusted-dev", D1, "linux", "aarch64"),
              bcs.namespace_prefix("untrusted", D1, "linux", "x86_64")}
    assert p not in others and len(others) == 4


def test_prefix_charset_and_length_even_for_hostile_input():
    p = bcs.namespace_prefix("Ev/il ..\\x", D1, "Li nux/..", "x86 64;" + "z" * 500)
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789._-/" for c in p)
    assert len(p) <= bcs.MAX_PREFIX_LEN and p.endswith("/") and "//" not in p
    with pytest.raises(StorageError) as e:
        bcs.namespace_prefix("ns", "abc", "linux", "x86_64")
    assert e.value.code is Code.NO_FINGERPRINT


def test_triple_to_os_arch_has_no_hostnames():
    assert bcs.os_arch_from_triple("x86_64-unknown-linux-gnu") == ("linux", "x86_64")
    assert bcs.os_arch_from_triple("aarch64-apple-darwin") == ("darwin", "aarch64")


def test_prefix_from_plan_differs_by_toolchain(tmp_path):
    a = _plan(tmp_path, _cfg(tmp_path), reachable=True)
    b = _plan(tmp_path, _cfg(tmp_path), reachable=True, rustc=RUSTC_B)
    assert a.shared.reason == "shared_unavailable:shared_requires_server" and not a.shared.active
    assert a.shared.namespace_digest != b.shared.namespace_digest
    assert a.env_overrides["SCCACHE_SERVER_PORT"] != b.env_overrides["SCCACHE_SERVER_PORT"]


# --- registry -----------------------------------------------------------------

def test_registry_resolves(tmp_path):
    ep = bcs.resolve_endpoint("build-cache", str(_registry(tmp_path)))
    assert ep == Endpoint("s3.example.org:9000", "us-east-1", "sccache-bucket", True)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_registry_loose_mode_rejected(tmp_path, mode):
    reg = _registry(tmp_path)
    reg.chmod(mode)
    with pytest.raises(StorageError) as e:
        bcs.resolve_endpoint("build-cache", str(reg))
    assert e.value.code is Code.REGISTRY_UNREADABLE


def test_registry_symlink_missing_dir_rejected(tmp_path):
    reg = _registry(tmp_path)
    link = tmp_path / "link.yaml"
    link.symlink_to(reg)
    for bad in (link, tmp_path, tmp_path / "missing.yaml"):
        with pytest.raises(StorageError) as e:
            bcs.resolve_endpoint("build-cache", str(bad))
        assert e.value.code is Code.REGISTRY_UNREADABLE


def test_registry_unknown_ref_and_unset(tmp_path):
    reg = str(_registry(tmp_path))
    with pytest.raises(StorageError) as e:
        bcs.resolve_endpoint("nope", reg)
    assert e.value.code is Code.UNKNOWN_ENDPOINT
    with pytest.raises(StorageError) as e:
        bcs.resolve_endpoint("build-cache", None)
    assert e.value.code is Code.NO_REGISTRY


@pytest.mark.parametrize("over", [{"host": "https://evil.example"}, {"host": "a b"},
                                  {"host": "h:99999"}, {"bucket": "UP"}, {"region": "../x"},
                                  {"use_ssl": "yes"}, {"extra": 1}])
def test_registry_invalid_entries(tmp_path, over):
    with pytest.raises(StorageError) as e:
        bcs.resolve_endpoint("build-cache", str(_registry(tmp_path, **over)))
    assert e.value.code is Code.REGISTRY_INVALID


def test_registry_garbage_yaml(tmp_path):
    reg = _private(tmp_path / "r.yaml", "endpoints: [unclosed\n")
    with pytest.raises(StorageError) as e:
        bcs.resolve_endpoint("build-cache", str(reg))
    assert e.value.code is Code.REGISTRY_INVALID


# --- credentials --------------------------------------------------------------

def test_credentials_parse_and_child_env(tmp_path):
    c = bcs.read_credentials(str(_creds(tmp_path)))
    assert c.as_child_env() == {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET,
                                "AWS_SESSION_TOKEN": TOKEN}
    assert "AWS_SESSION_TOKEN" not in bcs.read_credentials(
        str(_creds(tmp_path, token=False))).as_child_env()


def test_credentials_repr_redacted(tmp_path):
    c = bcs.read_credentials(str(_creds(tmp_path)))
    for text in (repr(c), str(c), f"{c}", repr([c]), repr({"c": c})):
        assert SECRET not in text and KEY_ID not in text and TOKEN not in text


@pytest.mark.parametrize("body", ["access_key_id: a\n", "- a\n", "secret_access_key: x\n",
                                  "access_key_id: a b\nsecret_access_key: c\n",
                                  "access_key_id: a\nsecret_access_key: c\nregion: x\n",
                                  "access_key_id: 12\nsecret_access_key: c\n", ": [\n"])
def test_credentials_invalid_never_echo_content(tmp_path, body):
    with pytest.raises(StorageError) as e:
        bcs.read_credentials(str(_private(tmp_path / "c.yaml", body)))
    assert e.value.code is Code.CREDENTIALS_INVALID


def test_credentials_permissions_and_unset(tmp_path):
    loose = _creds(tmp_path)
    loose.chmod(0o640)
    with pytest.raises(StorageError) as e:
        bcs.read_credentials(str(loose))
    assert e.value.code is Code.CREDENTIALS_UNREADABLE and SECRET not in str(e.value)
    with pytest.raises(StorageError) as e:
        bcs.read_credentials(None)
    assert e.value.code is Code.NO_CREDENTIALS


# --- TOML ---------------------------------------------------------------------

def _inputs(tmp_path, writes=True, ssl_=True, tls=True):
    return bcs.StorageInputs(Endpoint("s3.example.org:9000", "us-east-1", "bkt", ssl_),
                             "ns/linux-x86_64/" + "a" * 16 + "/", tmp_path / "cache",
                             20 * 1024 ** 3, writes, tls)


def test_toml_matches_verified_schema_and_parses(tmp_path):
    doc = tomllib.loads(bcs.render_sccache_config(_inputs(tmp_path)))
    assert doc["cache"]["multilevel"] == {"chain": ["disk", "s3"], "write_error_policy": "ignore"}
    assert doc["cache"]["disk"] == {"dir": str(tmp_path / "cache"), "size": 20 * 1024 ** 3}
    assert doc["cache"]["s3"] == {"bucket": "bkt", "endpoint": "s3.example.org:9000",
                                  "region": "us-east-1", "key_prefix": "ns/linux-x86_64/" + "a" * 16 + "/",
                                  "use_ssl": True}
    assert set(doc) == {"cache"}


def test_toml_has_no_credentials_or_rw_mode(tmp_path):
    toml = bcs.render_sccache_config(_inputs(tmp_path))
    for needle in (SECRET, KEY_ID, TOKEN, "AWS_", "secret_", "token", "rw_mode"):
        assert needle not in toml


def test_plaintext_refused_when_tls_required(tmp_path):
    with pytest.raises(StorageError) as e:
        bcs.render_sccache_config(_inputs(tmp_path, ssl_=False, tls=True))
    assert e.value.code is Code.TLS_REQUIRED
    assert "use_ssl = false" in bcs.render_sccache_config(_inputs(tmp_path, ssl_=False, tls=False))


def test_plan_refuses_plaintext_registry_when_tls_required(tmp_path):
    reg = _registry(tmp_path, use_ssl=False)
    p = _plan(tmp_path, _cfg(tmp_path, registry=reg), reachable=True)
    assert p.applied and not p.shared.active and p.shared.reason == "shared_unavailable:tls_required"


def test_plaintext_allowed_only_for_local_test_ref(tmp_path):
    reg = _private(tmp_path / "r2.yaml", "endpoints:\n  box.local-test:\n    host: 127.0.0.1:9\n"
                   "    region: us-east-1\n    bucket: bkt\n    use_ssl: false\n")
    cfg = _cfg(tmp_path, registry=reg, ref="box.local-test", tls=False)
    p = _plan(tmp_path, cfg, reachable=True)
    assert p.shared.reason == "shared_unavailable:shared_requires_server"  # validated, then degraded
    assert "use_ssl = false" in bcs.render_sccache_config(_inputs(tmp_path, ssl_=False, tls=False))


def test_shared_never_puts_conf_rw_mode_or_credentials_in_cargo_env(tmp_path):
    for writes in (False, True):
        p = _plan(tmp_path, _cfg(tmp_path, writes=writes), reachable=True)
        assert not p.shared.active
        assert not (set(p.env_overrides) & {"SCCACHE_CONF", "SCCACHE_S3_RW_MODE"})
        assert not any(k.startswith("AWS_") for k in p.env_overrides)
        assert SECRET not in repr(p) and KEY_ID not in repr(p)
        assert not hasattr(p, "secret_env")


def test_server_port_is_per_policy(tmp_path):
    def port(**kw):
        return _plan(tmp_path, _cfg(tmp_path, **kw), reachable=True).env_overrides["SCCACHE_SERVER_PORT"]
    ro, rw = port(writes=False), port(writes=True)
    assert ro != rw
    assert ro == port(writes=False)  # deterministic
    assert port(mode="local_only") != ro
    other_ns = _plan(tmp_path, _cfg(tmp_path), reachable=True, rustc=RUSTC_B)
    assert other_ns.env_overrides["SCCACHE_SERVER_PORT"] != ro


# --- version gate / plan fallbacks -------------------------------------------

@pytest.mark.parametrize("text,ok", [("sccache 0.19.0", True), ("sccache 0.20.1", True),
                                     ("sccache v1.0.0", True), ("sccache 0.18.9", False),
                                     ("sccache 0.9.0", False), (None, False), ("garbage", False)])
def test_version_gate(tmp_path, text, ok):
    p = _plan(tmp_path, _cfg(tmp_path), reachable=True, tools=_tools(tmp_path, text))
    assert p.applied  # the local cache is unaffected
    assert not p.shared.active
    if ok:
        assert p.shared.reason == "shared_unavailable:shared_requires_server"
    else:
        want = "sccache_version_unknown" if parse_none(text) else "sccache_version_unsupported"
        assert p.shared.reason == f"shared_unavailable:{want}"
        assert "SCCACHE_CONF" not in p.env_overrides


def parse_none(text):
    return bcs.parse_sccache_version(text) is None


@pytest.mark.parametrize("kw,code", [
    (dict(registry=False), "registry_not_configured"),
    (dict(ref="other"), "unknown_endpoint"),
    (dict(creds=False), "credentials_not_configured"),
    (dict(mode="local_only"), None),
])
def test_plan_unavailable_reasons(tmp_path, kw, code):
    cfg = _cfg(tmp_path, **kw) if "mode" not in kw else _cfg(tmp_path, mode="local_only")
    if code is None:
        p = _plan(tmp_path, cfg, reachable=True)
        assert p.shared.reason == "shared_unavailable:mode_not_shared"
    else:
        if kw.get("ref") == "other":
            reg = _registry(tmp_path)
            cfg = _cfg(tmp_path, registry=reg, ref="other")
        p = _plan(tmp_path, cfg, reachable=True)
        assert p.shared.reason == f"shared_unavailable:{code}"
    assert p.applied and not p.shared.active and "SCCACHE_CONF" not in p.env_overrides


def test_plan_unreachable_means_local(tmp_path):
    p = _plan(tmp_path, _cfg(tmp_path), reachable=False)
    assert p.applied and p.shared.reason == "shared_unavailable:endpoint_unreachable"
    assert "SCCACHE_DIR" in p.env_overrides


def test_shared_not_requested_by_default(tmp_path):
    p = _plan(tmp_path, validate({"enabled": True}), reachable=None)
    assert p.shared == bce.NOT_REQUESTED


# --- health probe -------------------------------------------------------------

@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("no openssl")
    d = tmp_path_factory.mktemp("cert")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
                       capture_output=True)
    if r.returncode != 0:
        pytest.skip("openssl could not make a certificate")
    return d / "c.pem", d / "k.pem"


class _Server:
    def __init__(self, tls_files=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.ctx = None
        if tls_files:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*map(str, tls_files))
        self.accepted = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                if self.ctx:
                    c = self.ctx.wrap_socket(c, server_side=True)
                else:
                    c.recv(1)  # plain socket: never speaks TLS
            except (OSError, ssl.SSLError):
                pass
            finally:
                c.close()

    def close(self):
        self.sock.close()


def _ep(port, ssl_=True):
    return Endpoint(f"127.0.0.1:{port}", "us-east-1", "bkt", ssl_)


def _trusting(cert):
    return ssl.create_default_context(cafile=str(cert[0]))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_good_tls(cert):
    srv = _Server(cert)
    try:
        h = bcs.probe_health(_ep(srv.port), 2.0, context=_trusting(cert))
    finally:
        srv.close()
    assert h == bcs.Health(True, "ok")


def test_probe_untrusted_self_signed_is_tls_failed(cert):
    srv = _Server(cert)
    try:
        assert bcs.probe_health(_ep(srv.port), 2.0) == bcs.Health(False, "tls_failed")
    finally:
        srv.close()


def test_probe_non_tls_listener_and_closed_port(tmp_path):
    srv = _Server()
    try:
        assert bcs.probe_health(_ep(srv.port), 1.0) == bcs.Health(False, "tls_failed")
    finally:
        srv.close()
    assert bcs.probe_health(_ep(_free_port()), 1.0) == bcs.Health(False, "connect_failed")


def test_probe_is_bounded_by_timeout():
    import time
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(0)  # accepts nothing; TLS handshake never completes
    t = time.monotonic()
    try:
        h = bcs.probe_health(_ep(srv.getsockname()[1]), 0.5)
    finally:
        srv.close()
    assert not h.ok and time.monotonic() - t < 3


def test_negative_cache_ttl_and_recovery(tmp_path, cert):
    port, now = _free_port(), [1000.0]
    clock = lambda: now[0]  # noqa: E731
    assert bcs.probe_health(_ep(port), 1.0, state_dir=tmp_path, clock=clock).code == "connect_failed"
    # server comes up, but inside the TTL we do not even try
    srv = _Server(cert)
    ep = _ep(srv.port)
    try:
        assert bcs.probe_health(ep, 1.0, state_dir=tmp_path, clock=clock,
                                context=_trusting(cert)).ok
        down = _ep(srv.port)
        srv.close()
        assert not bcs.probe_health(down, 1.0, state_dir=tmp_path, clock=clock).ok
        n = srv.accepted
        now[0] += 10
        assert bcs.probe_health(down, 1.0, state_dir=tmp_path, clock=clock) == \
            bcs.Health(False, "cached_down")
        assert srv.accepted == n
        now[0] += 25  # past the 30 s TTL: probes again
        assert bcs.probe_health(down, 1.0, state_dir=tmp_path, clock=clock).code == "connect_failed"
    finally:
        srv.close()


def test_negative_cache_cleared_by_success(tmp_path, cert):
    srv = _Server(cert)
    try:
        ep = _ep(srv.port)
        neg = bcs._neg_path(tmp_path, ep)
        neg.parent.mkdir(parents=True)
        neg.write_text("")
        os.utime(neg, (0, 0))  # ancient => expired
        assert bcs.probe_health(ep, 1.0, state_dir=tmp_path, context=_trusting(cert)).ok
        assert not neg.exists()
    finally:
        srv.close()


def test_check_shared_applicability(tmp_path):
    state = tmp_path / "state"
    assert bcs.check_shared(validate({"enabled": True}), state) is None
    assert bcs.check_shared(_cfg(tmp_path, mode="local_only"), state) is None
    assert bcs.check_shared(_cfg(tmp_path, registry=False), state) is None
    reg = _registry(tmp_path, host=f"127.0.0.1:{_free_port()}")
    assert bcs.check_shared(_cfg(tmp_path, registry=reg), state, timeout=1.0) is False


# --- end to end through run_gated (fake sccache/cargo) --------------------------

def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake(tmp_path, monkeypatch, scc_version="sccache 0.19.0"):
    bin_ = tmp_path / "bin"
    _exe(bin_ / "sccache", f"echo '{scc_version}'\n")
    _exe(bin_ / "rustc", f"cat <<'EOF'\n{RUSTC_A}EOF\n")
    out = tmp_path / "seen.json"
    (tmp_path / "probe.py").write_text(textwrap.dedent(f"""
        import json, os, sys
        e = os.environ
        conf = e.get("SCCACHE_CONF")
        json.dump({{
          "env": {{k: v for k, v in e.items() if k.startswith(("AWS_", "SCCACHE_"))}},
          "toml": open(conf).read() if conf and os.path.exists(conf) else None,
          "mode": oct(os.stat(conf).st_mode & 0o777) if conf and os.path.exists(conf) else None,
        }}, open({str(out)!r}, "w"))
        print("child stderr line", file=sys.stderr)
        sys.exit(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 0)
    """))
    _exe(bin_ / "cargo", f'exec {sys.executable} {tmp_path / "probe.py"} "$@"\n')
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    for k in list(os.environ):
        if k.startswith(("AWS_", "SCCACHE_")) or k in ("RUSTC_WRAPPER", "CARGO_INCREMENTAL"):
            monkeypatch.delenv(k, raising=False)
    return out


def _events(tmp_path):
    return [json.loads(x) for x in (tmp_path / "state/builds.jsonl").read_text().splitlines()]


def _everything_text(tmp_path):
    return "\n".join(p.read_text(errors="replace") for p in (tmp_path / "state").rglob("*")
                     if p.is_file() and p.name != "sccache.toml") + "\n" + \
        (tmp_path / "state/sccache/sccache.toml").read_text()


def test_e2e_shared_request_never_leaks_credentials_to_cargo(tmp_path, monkeypatch, cert, capfd):
    out = _fake(tmp_path, monkeypatch)
    srv = _Server(cert)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert[0]))
    reg = _registry(tmp_path, host=f"127.0.0.1:{srv.port}")
    try:
        rc = run_gated(["cargo", "build", "3"], state_dir=tmp_path / "state", slots=2, max_load=0,
                       cache_config=_cfg(tmp_path, registry=reg))
    finally:
        srv.close()
    assert rc == 3  # child exit code preserved
    seen = json.loads(out.read_text())
    assert not any(k.startswith("AWS_") for k in seen["env"])
    assert "SCCACHE_CONF" not in seen["env"] and "SCCACHE_S3_RW_MODE" not in seen["env"]
    assert seen["env"]["SCCACHE_DIR"] == str(tmp_path / "state/sccache")  # local cache
    assert "SCCACHE_SERVER_PORT" in seen["env"]
    ev = _events(tmp_path)[-1]["cache"]
    assert ev["shared"]["active"] is False
    assert ev["shared"]["reason"] == "shared_unavailable:shared_requires_server"
    captured = capfd.readouterr()
    blob = "\n".join(p.read_text(errors="replace") for p in (tmp_path / "state").rglob("*")
                     if p.is_file())
    for b_ in (blob, captured.out, captured.err):
        for s_ in (SECRET, KEY_ID, TOKEN):
            assert s_ not in b_


def test_e2e_dead_endpoint_falls_back_to_local_and_keeps_exit_code(tmp_path, monkeypatch, capfd):
    out = _fake(tmp_path, monkeypatch)
    reg = _registry(tmp_path, host=f"127.0.0.1:{_free_port()}")
    rc = run_gated(["cargo", "build", "5"], state_dir=tmp_path / "state", slots=2, max_load=0,
                   cache_config=_cfg(tmp_path, registry=reg))
    assert rc == 5
    seen = json.loads(out.read_text())
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"] and "SCCACHE_CONF" not in seen["env"]
    assert seen["env"]["SCCACHE_DIR"] == str(tmp_path / "state/sccache")  # still local cache
    ev = _events(tmp_path)[-1]["cache"]
    assert ev["applied"] is True and ev["shared"]["reason"] == "shared_unavailable:endpoint_unreachable"
    err = capfd.readouterr()
    assert SECRET not in err.out + err.err + _everything_text_safe(tmp_path)


def _everything_text_safe(tmp_path):
    return "\n".join(p.read_text(errors="replace") for p in (tmp_path / "state").rglob("*")
                     if p.is_file())


def test_e2e_tls_failure_falls_back(tmp_path, monkeypatch, cert):
    out = _fake(tmp_path, monkeypatch)
    srv = _Server(cert)  # self-signed and NOT trusted (no SSL_CERT_FILE) => tls_failed
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    reg = _registry(tmp_path, host=f"127.0.0.1:{srv.port}")
    try:
        rc = run_gated(["cargo", "build"], state_dir=tmp_path / "state", slots=2, max_load=0,
                       cache_config=_cfg(tmp_path, registry=reg))
    finally:
        srv.close()
    assert rc == 0
    assert "SCCACHE_CONF" not in json.loads(out.read_text())["env"]
    assert _events(tmp_path)[-1]["cache"]["shared"]["reason"] == "shared_unavailable:endpoint_unreachable"


def test_e2e_old_sccache_is_local_only(tmp_path, monkeypatch):
    out = _fake(tmp_path, monkeypatch, scc_version="sccache 0.12.0")
    rc = run_gated(["cargo", "build"], state_dir=tmp_path / "state", slots=2, max_load=0,
                   cache_config=_cfg(tmp_path))
    assert rc == 0
    seen = json.loads(out.read_text())
    assert "AWS_ACCESS_KEY_ID" not in seen["env"] and "SCCACHE_CONF" not in seen["env"]
    assert _events(tmp_path)[-1]["cache"]["shared"]["reason"] == \
        "shared_unavailable:sccache_version_unsupported"


def test_e2e_bad_credentials_file_never_fails_build(tmp_path, monkeypatch):
    out = _fake(tmp_path, monkeypatch)
    creds = _creds(tmp_path)
    creds.chmod(0o644)
    rc = run_gated(["cargo", "build", "9"], state_dir=tmp_path / "state", slots=2, max_load=0,
                   cache_config=_cfg(tmp_path, creds=creds))
    assert rc == 9 and "AWS_ACCESS_KEY_ID" not in json.loads(out.read_text())["env"]
    assert _events(tmp_path)[-1]["cache"]["shared"]["reason"] == \
        "shared_unavailable:credentials_unreadable"
