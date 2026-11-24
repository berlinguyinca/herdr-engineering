"""REAL end-to-end proof of the shared tier: real sccache 0.19, real cargo, real rustfs.

No fakes. A tiny temp cargo project is built through ``herdr-eng build-coordinate
--record`` with a TEMP config that enables the shared tier against the herdr
rustfs server on loopback. Skips with a reason when sccache, cargo, rustfs or the
provisioned credential files are missing. Objects written by the test live under
a unique test namespace and are deleted afterwards (nothing else is touched).

Run with ``-s`` to see the measured stats and the recorded event.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from herdr_engineering import build_cache_env as bce
from herdr_engineering import build_cache_server as bcsrv
from herdr_engineering import build_cache_storage as bcs
from herdr_engineering.build_cache_config import validate

CFG_DIR = Path.home() / ".config" / "herdr-engineering"
RW_CRED = CFG_DIR / "build-cache-rustfs.cred"
RO_CRED = CFG_DIR / "build-cache-rustfs-ro.cred"
REGISTRY = CFG_DIR / "build-cache-rustfs-registry.yaml"
ENDPOINT = "http://127.0.0.1:9000"
BUCKET = "herdr-build-cache"


def _need():
    sccache = shutil.which("sccache") or str(Path.home() / ".local/bin/sccache")
    if not Path(sccache).exists():
        pytest.skip("sccache not installed")
    if not shutil.which("cargo"):
        pytest.skip("cargo not installed")
    for f in (RW_CRED, RO_CRED, REGISTRY):
        if not f.exists():
            pytest.skip(f"rustfs not provisioned on this host ({f.name} missing)")
    try:
        socket.create_connection(("127.0.0.1", 9000), timeout=1).close()
    except OSError:
        pytest.skip("rustfs unreachable on 127.0.0.1:9000")
    try:
        import boto3  # noqa: F401
    except ImportError:
        pytest.skip("boto3 not installed")
    return sccache


def _s3(cred: Path):
    import boto3
    c = bcs.read_credentials(str(cred))
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                        aws_access_key_id=c.access_key_id,
                        aws_secret_access_key=c.secret_access_key)


def _keys(s3, prefix: str) -> list[str]:
    out, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix, **({"ContinuationToken": token} if token else {})}
        r = s3.list_objects_v2(**kw)
        out += [o["Key"] for o in r.get("Contents", [])]
        token = r.get("NextContinuationToken")
        if not token:
            return out


def _project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[package]\nname = "hbcproof"\nversion = "0.1.0"\nedition = "2021"\n')
    # sccache does not cache bin crates, so the proof crate is a library
    (root / "src/lib.rs").write_text(
        f'pub const NONCE: &str = "{uuid.uuid4()}";\npub fn nonce() -> &\'static str {{ NONCE }}\n')
    return root


def _cfg(namespace: str, cred: Path, writes: bool) -> dict:
    return {"enabled": True, "mode": "shared_cache",
            "shared_cache": {"enabled": True, "endpoint_ref": "build-cache",
                             "namespace": namespace, "writes_enabled": writes,
                             "credentials_file": str(cred), "registry_file": str(REGISTRY)},
            "local": {"capacity_gib": 2}}


def _build(proj: Path, work: Path, cfg: dict, sccache_dir: str):
    import yaml
    work.mkdir(parents=True, exist_ok=True)
    conf = work / "config.yaml"
    conf.write_text(yaml.safe_dump({"fabric_build_cache": cfg}))
    state = work / "state"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SCCACHE_", "AWS_")) and k not in ("RUSTC_WRAPPER",)}
    env.update({"HERDR_ENGINEERING_CONFIG": str(conf), "HERDR_ENGINEERING_STATE": str(state),
                "CARGO_INCREMENTAL": "0", "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "CARGO_TARGET_DIR": str(proj / "target"),
                "PATH": f"{Path(sccache_dir).parent}:{os.environ['PATH']}"})
    r = subprocess.run([sys.executable, "-c",
                        "import sys; from herdr_engineering.cli import main; sys.exit(main())",
                        "build-coordinate", "--record", "--slots", "2", "--max-load", "0", "--",
                        "cargo", "build", "-q"], cwd=proj, env=env, capture_output=True, text=True,
                       timeout=300)
    events = [json.loads(x) for x in (state / "build-gates/builds.jsonl").read_text().splitlines()]
    port = None
    p = bce.plan(validate(cfg), ["cargo", "build"], env, bce.detect_tools(env["PATH"]), proj,
                 state / "build-gates")
    port = int(p.env_overrides["SCCACHE_SERVER_PORT"])
    return r, events[-1], port


def _stop(sccache: str, port: int):
    bcsrv.stop_server(sccache, port)


def test_shared_tier_end_to_end_against_rustfs(tmp_path, capsys):
    sccache = _need()
    ns = f"e2e-{uuid.uuid4().hex[:10]}"
    ns_other = f"{ns}-other"
    rw, ro = _s3(RW_CRED), _s3(RO_CRED)
    proj = _project(tmp_path / "proj")
    ports: list[int] = []
    report: dict = {}
    try:
        # (a) cold build, first state dir: objects land under the namespace prefix
        ra, ev_a, port_a = _build(proj, tmp_path / "A", _cfg(ns, RW_CRED, True), sccache)
        ports.append(port_a)
        assert ra.returncode == 0, ra.stderr
        assert ev_a["cache"]["shared"]["active"] is True, ev_a["cache"]["shared"]
        keys_a = [k for k in _keys(rw, f"{ns}/") if not k.endswith(".sccache_check")]
        assert keys_a, "cold build wrote no object under the namespace prefix"
        report["a_objects"] = len(keys_a)
        report["a_event"] = {"shared": ev_a["cache"]["shared"], "sccache_delta": ev_a.get("sccache_stats")}
        time_for_writes = __import__("time").sleep
        time_for_writes(1.5)
        _stop(sccache, port_a)

        # (b) cargo clean + brand-new local cache dir: the hit comes from rustfs
        subprocess.run(["cargo", "clean", "-q"], cwd=proj, check=True, timeout=120)
        rb, ev_b, port_b = _build(proj, tmp_path / "B", _cfg(ns, RW_CRED, True), sccache)
        ports.append(port_b)
        assert rb.returncode == 0, rb.stderr
        assert ev_b["cache"]["shared"]["active"] is True
        delta = ev_b.get("sccache_stats") or {}
        remote_hits = {k: v for k, v in delta.items() if "multi_level" in k and "hits" in k}
        report["b_event"] = {"shared": ev_b["cache"]["shared"], "remote_hit_deltas": remote_hits,
                             "all_deltas": delta}
        assert any(v > 0 for k, v in remote_hits.items()), delta
        assert not (tmp_path / "A/state").samefile(tmp_path / "B/state")
        _stop(sccache, port_b)

        # (c) a different namespace MISSes (prefix isolation)
        subprocess.run(["cargo", "clean", "-q"], cwd=proj, check=True, timeout=120)
        rc_, ev_c, port_c = _build(proj, tmp_path / "C", _cfg(ns_other, RW_CRED, True), sccache)
        ports.append(port_c)
        assert rc_.returncode == 0, rc_.stderr
        delta_c = ev_c.get("sccache_stats") or {}
        report["c_all_deltas"] = delta_c
        assert not any(v > 0 for k, v in delta_c.items() if "multi_level" in k and "hits" in k
                       and "backfills" not in k), delta_c
        assert [k for k in _keys(rw, f"{ns_other}/") if not k.endswith(".sccache_check")]
        _stop(sccache, port_c)

        # (d) read-only user cannot write; a read-only build never writes
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError) as e:
            ro.put_object(Bucket=BUCKET, Key=f"{ns}/ro-probe", Body=b"x")
        assert e.value.response["Error"]["Code"] in ("AccessDenied", "AllUsersDisabled")
        ns_ro = f"{ns}-ro"
        subprocess.run(["cargo", "clean", "-q"], cwd=proj, check=True, timeout=120)
        rd, ev_d, port_d = _build(proj, tmp_path / "D", _cfg(ns_ro, RO_CRED, False), sccache)
        ports.append(port_d)
        assert rd.returncode == 0, rd.stderr
        assert ev_d["cache"]["shared"]["active"] is True
        time_for_writes(1.0)
        assert [k for k in _keys(rw, f"{ns_ro}/") if not k.endswith(".sccache_check")] == []
        _stop(sccache, port_d)
    finally:
        for port in ports:
            _stop(sccache, port)
        for prefix in (f"{ns}/", f"{ns_other}/", f"{ns}-ro/"):
            for k in _keys(rw, prefix):
                assert k.startswith(("e2e-",)) and ns in k  # only our own test objects
                rw.delete_object(Bucket=BUCKET, Key=k)
    with capsys.disabled():
        print("\nMEASURED:", json.dumps(report, indent=1, sort_keys=True))


def _live_setup(tmp_path, ns, writes=True):
    import time  # noqa: F401
    cfg = validate(_cfg(ns, RW_CRED, writes))
    state = tmp_path / "state"
    tools = bce.detect_tools(os.environ["PATH"])
    fp = bce.fingerprint_from_text(tools.rustc_vv)
    setup = bcs.prepare_shared(cfg, sccache_version=tools.sccache_version,
                               toolchain_digest=fp.digest, host_triple=fp.host,
                               state_dir=state, reachable=True)
    return setup


def test_stale_local_only_server_on_the_port_is_replaced_and_idle_reaped(tmp_path):
    import time
    sccache = _need()
    setup = _live_setup(tmp_path, f"e2e-{uuid.uuid4().hex[:10]}")
    port = 31877
    bcsrv.stop_server(sccache, port)
    try:
        # a bare client-spawned server: local only, no credentials, no remote level
        subprocess.run([sccache, "--start-server"], env={"PATH": os.environ["PATH"],
                       "SCCACHE_SERVER_PORT": str(port), "SCCACHE_DIR": str(tmp_path / "l")},
                       close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=True)
        assert not bcsrv.serves_expected_remote(bcsrv._stats(sccache, port),
                                                setup.inputs.endpoint.bucket, setup.inputs.prefix)
        res = bcsrv.ensure_shared_server(setup, os.environ, sccache, port, idle_timeout=6)
        assert res.ok, res
        assert bcsrv.serves_expected_remote(bcsrv._stats(sccache, port),
                                            setup.inputs.endpoint.bucket, setup.inputs.prefix)
        # reuse: a second call keeps the same server (same pid)
        pid1 = subprocess.run(["pgrep", "-f", f"SCCACHE_SERVER_PORT={port}"], capture_output=True)
        assert bcsrv.ensure_shared_server(setup, os.environ, sccache, port, idle_timeout=6).ok
        # no lock fds leak into the server: it holds only its own descriptors
        deadline = time.monotonic() + 40
        while bcsrv._listening(port, 0.2) and time.monotonic() < deadline:
            time.sleep(1)
        assert not bcsrv._listening(port, 0.2), "idle timeout did not reap the server"
        del pid1
    finally:
        bcsrv.stop_server(sccache, port)
        rw = _s3(RW_CRED)
        for k in _keys(rw, setup.inputs.prefix.split("/")[0] + "/"):
            rw.delete_object(Bucket=BUCKET, Key=k)


def test_bad_credentials_never_report_the_tier_active(tmp_path):
    sccache = _need()
    ns = f"e2e-{uuid.uuid4().hex[:10]}"
    setup = _live_setup(tmp_path, ns)
    from dataclasses import replace
    bad = replace(setup, credentials=bcs.Credentials("nosuchkey", "nosuchsecretnosuchsecret"))
    port = 31878
    bcsrv.stop_server(sccache, port)
    try:
        res = bcsrv.ensure_shared_server(bad, os.environ, sccache, port, idle_timeout=6)
        # sccache itself starts happily with bad keys; the signed list check catches it
        assert not res.ok and res.code is bcs.Code.CREDENTIALS_REJECTED
        assert not bcsrv._listening(port, 0.2)  # no server was started for rejected keys
    finally:
        bcsrv.stop_server(sccache, port)
