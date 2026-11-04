"""Spec 03 (local sccache pilot): env planning + run_gated wiring.

Fake-executable tests below are CONTRACT TESTS OF ENV WIRING; they prove nothing
about cache savings (no real sccache exists on this host).
"""
import dataclasses
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from herdr_engineering import build_cache_env as bce
from herdr_engineering.build_cache_config import validate
from herdr_engineering.build_coord import run_gated

RUSTC_A = """rustc 1.97.0 (2d8144b78 2026-07-07)
binary: rustc
commit-hash: 2d8144b7880597b6e6d3dfd63a9a9efae3f533d3
commit-date: 2026-07-07
host: x86_64-unknown-linux-gnu
release: 1.97.0
LLVM version: 22.1.6
"""
RUSTC_B = RUSTC_A.replace("1.97.0", "1.91.0").replace("2d8144b78", "abcdef123") \
    .replace("2d8144b7880597b6e6d3dfd63a9a9efae3f533d3", "f" * 40).replace("22.1.6", "21.1.2")


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _cfg(**over):
    sec = {"enabled": True}
    sec.update(over)
    return validate(sec)


def _tools(tmp_path, with_sccache=True):
    scc = tmp_path / "sccache"
    return bce.Tools(sccache=scc if with_sccache else None,
                     sccache_version="sccache 0.19.0" if with_sccache else None,
                     rustc=tmp_path / "rustc", cargo=tmp_path / "cargo", rustc_vv=RUSTC_A)


def _plan(tmp_path, cfg=None, cmd=("cargo", "build"), env=None, tools=None, ws=None):
    ws = ws or tmp_path
    return bce.plan(cfg or _cfg(), list(cmd), env or {}, tools or _tools(tmp_path),
                    ws, tmp_path / "state")


# --- fingerprint -------------------------------------------------------------

def test_fingerprint_fields_and_stability():
    a1, a2 = bce.fingerprint_from_text(RUSTC_A), bce.fingerprint_from_text(RUSTC_A)
    assert a1 == a2 and a1.digest == a2.digest and len(a1.digest) == 64
    assert (a1.host, a1.release, a1.llvm) == ("x86_64-unknown-linux-gnu", "1.97.0", "22.1.6")
    assert a1.commit_hash.startswith("2d8144b7")


def test_fingerprint_differs_across_toolchain_and_target():
    a, b = bce.fingerprint_from_text(RUSTC_A), bce.fingerprint_from_text(RUSTC_B)
    assert a.digest != b.digest
    assert bce.fingerprint_from_text(RUSTC_A, "x86_64-unknown-linux-musl").digest != a.digest


def test_fingerprint_has_no_paths():
    blob = json.dumps(dataclasses.asdict(bce.fingerprint_from_text(RUSTC_A)))
    assert "/home" not in blob and "sysroot" not in blob


def test_fingerprint_garbage_is_none():
    assert bce.fingerprint_from_text("not rustc output") is None


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no real rustc")
def test_real_rustc_fingerprint_matches_text():
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True).stdout
    assert bce.toolchain_fingerprint(Path(shutil.which("rustc"))) == bce.fingerprint_from_text(out)


# --- detect / classify -------------------------------------------------------

def test_detect_tools_uses_explicit_path_only(tmp_path):
    _exe(tmp_path / "bin/sccache", "echo 'sccache 0.19.0'\n")
    _exe(tmp_path / "bin/rustc", f"cat <<'EOF'\n{RUSTC_A}EOF\n")
    t = bce.detect_tools(str(tmp_path / "bin"))
    assert t.sccache == tmp_path / "bin/sccache" and t.sccache_version == "sccache 0.19.0"
    assert t.rustc == tmp_path / "bin/rustc" and t.cargo is None
    assert bce.fingerprint_from_text(t.rustc_vv).release == "1.97.0"


def test_detect_tools_missing_and_hanging(tmp_path):
    t = bce.detect_tools(str(tmp_path / "empty"))
    assert t == bce.Tools(None, None, None, None, None)
    _exe(tmp_path / "bin/sccache", "exit 3\n")
    assert bce.detect_tools(str(tmp_path / "bin")).sccache_version is None


@pytest.mark.parametrize("cmd,cls", [
    (["cargo", "build"], bce.CmdClass.CACHEABLE_CARGO),
    (["cargo", "+stable", "check", "--workspace"], bce.CmdClass.CACHEABLE_CARGO),
    (["/x/cargo", "test"], bce.CmdClass.CACHEABLE_CARGO),
    (["cargo", "clippy"], bce.CmdClass.CACHEABLE_CARGO),
    (["cargo", "run"], bce.CmdClass.OTHER),
    (["tsc"], bce.CmdClass.OTHER),
    ([], bce.CmdClass.OTHER),
])
def test_classify(cmd, cls):
    assert bce.classify(cmd) is cls


# --- plan matrix ---------------------------------------------------------------

def test_applied_exact_overrides(tmp_path):
    p = _plan(tmp_path, _cfg(local={"capacity_gib": 7}))
    assert p.applied and p.reasons == ["applied"]
    assert p.env_overrides == {
        "RUSTC_WRAPPER": str(tmp_path / "sccache"),
        "SCCACHE_DIR": str(tmp_path / "state/sccache"),
        "SCCACHE_CACHE_SIZE": "7G",
        "CARGO_INCREMENTAL": "0",
    }
    assert p.fingerprint.digest == bce.fingerprint_from_text(RUSTC_A).digest


def test_fractional_capacity_formatting(tmp_path):
    p = _plan(tmp_path, _cfg(local={"capacity_gib": 0.5}))
    assert p.env_overrides["SCCACHE_CACHE_SIZE"] == "512M"


def test_never_touches_target_dir_or_idle_timeout(tmp_path):
    p = _plan(tmp_path)
    assert "CARGO_TARGET_DIR" not in p.env_overrides
    assert "SCCACHE_IDLE_TIMEOUT" not in p.env_overrides


@pytest.mark.parametrize("cfg,cmd,env,tools_kw,reason", [
    (None, ["cargo", "build"], {}, {}, "disabled"),
    (_cfg(), ["cargo", "run"], {}, {}, "not_cacheable_cmd"),
    (_cfg(), ["sh", "-c", "true"], {}, {}, "not_cacheable_cmd"),
    (_cfg(), ["cargo", "build"], {}, {"with_sccache": False}, "sccache_not_found"),
    (_cfg(), ["cargo", "build"], {"RUSTC_WRAPPER": "x"}, {}, "user_wrapper_present"),
    (_cfg(), ["cargo", "build"], {"RUSTC_WORKSPACE_WRAPPER": "x"}, {}, "user_wrapper_present"),
    (_cfg(), ["cargo", "build"], {"CARGO_BUILD_RUSTC_WRAPPER": "x"}, {}, "user_wrapper_present"),
    (_cfg(mode="shared_cache", shared_cache={"enabled": True, "endpoint_ref": "e"}),
     ["cargo", "build"], {}, {}, None),
])
def test_not_applied_matrix(tmp_path, cfg, cmd, env, tools_kw, reason):
    cfg = cfg if cfg is not None else validate(None)
    p = bce.plan(cfg, cmd, env, _tools(tmp_path, **tools_kw), tmp_path, tmp_path / "state")
    if reason is None:
        assert p.applied
        return
    assert not p.applied and p.env_overrides == {} and p.reasons == [reason]


def test_rustc_missing_not_applied(tmp_path):
    t = dataclasses.replace(_tools(tmp_path), rustc=None, rustc_vv=None)
    p = _plan(tmp_path, tools=t)
    assert not p.applied and p.reasons == ["rustc_not_found"]


def test_non_local_mode_not_applied(tmp_path):
    cfg = _cfg(mode="slurm_full_build", execution={
        "slurm_enabled": True, "allowed_clusters": ["c"], "allowed_partitions": ["p"]})
    assert _plan(tmp_path, cfg).reasons == ["mode_not_local"]


def test_host_allowlist(tmp_path):
    assert _plan(tmp_path, _cfg(allowed_hosts=["nope-host-xyz"])).reasons == ["host_not_allowed"]
    import socket
    assert _plan(tmp_path, _cfg(allowed_hosts=[socket.gethostname()])).applied


def test_workspace_must_exist(tmp_path):
    p = _plan(tmp_path, ws=tmp_path / "missing")
    assert p.reasons == ["workspace_invalid"] and not p.applied


def test_user_target_dir_applies_with_warning(tmp_path):
    p = _plan(tmp_path, env={"CARGO_TARGET_DIR": "/shared/t"})
    assert p.applied and "CARGO_TARGET_DIR" not in p.env_overrides
    assert any("CARGO_TARGET_DIR is user-set; not managed" in w for w in p.warnings)


def test_incremental_kept_when_config_allows_but_warned(tmp_path):
    cfg = _cfg(local={"incremental_disabled_for_cacheable_builds": False})
    p = _plan(tmp_path, cfg, env={"CARGO_INCREMENTAL": "1"})
    assert p.applied and "CARGO_INCREMENTAL" not in p.env_overrides
    assert any("CARGO_INCREMENTAL=1" in w for w in p.warnings)
    q = _plan(tmp_path, cfg, env={"CARGO_INCREMENTAL": "0"})
    assert q.warnings == [] or not any("INCREMENTAL" in w for w in q.warnings)


def test_ram_scratch_warns_explicitly(tmp_path):
    p = _plan(tmp_path, _cfg(local={"ram_scratch_gib": 2}))
    assert p.applied and "ram_scratch not implemented" in p.warnings


def test_shared_cache_in_local_mode_is_inactive_with_reason(tmp_path):
    cfg = _cfg(shared_cache={"enabled": True, "endpoint_ref": "e"})
    p = _plan(tmp_path, cfg)
    assert p.applied and p.shared.reason == "shared_unavailable:mode_not_shared"
    assert not any(k.startswith(("SCCACHE_BUCKET", "SCCACHE_ENDPOINT")) for k in p.env_overrides)


def test_low_free_disk(tmp_path):
    p = _plan(tmp_path, _cfg(local={"min_free_disk_percent": 99.99}))
    assert not p.applied and p.reasons == ["low_free_disk"]


def test_plan_is_pure(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    a, b = _plan(tmp_path), _plan(tmp_path)
    assert a == b
    assert sorted(tmp_path.rglob("*")) == before
    assert not (tmp_path / "state").exists()


# --- run_gated wiring (contract tests of env wiring; prove nothing about savings) ---

def _fake_env(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    _exe(bin_ / "sccache", "echo 'sccache 0.19.0'\n")
    _exe(bin_ / "rustc", f"cat <<'EOF'\n{RUSTC_A}EOF\n")
    out = tmp_path / "seen.txt"
    probe = textwrap.dedent(f"""
        import fcntl, os, sys
        held = []
        for n in ("gate.0", "build-maintenance.lock"):
            fd = os.open(os.path.join({str(tmp_path / 'state')!r}, n), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); held.append(n + ":FREE")
            except BlockingIOError:
                held.append(n + ":HELD")
        e = os.environ
        open({str(out)!r}, "w").write("\\n".join([
            "WRAP=" + e.get("RUSTC_WRAPPER", "-"), "INC=" + e.get("CARGO_INCREMENTAL", "-"),
            "DIR=" + e.get("SCCACHE_DIR", "-"), "TGT=" + e.get("CARGO_TARGET_DIR", "-"),
            *held]))
        sys.exit(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 0)
    """)
    (tmp_path / "probe.py").write_text(probe)
    _exe(bin_ / "cargo", f'exec {sys.executable} {tmp_path / "probe.py"} "$@"\n')
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    for k in ("RUSTC_WRAPPER", "CARGO_INCREMENTAL", "CARGO_TARGET_DIR", "SCCACHE_DIR"):
        monkeypatch.delenv(k, raising=False)
    return out


def _sub(cmd, tmp_path, section):
    """run_gated in a subprocess: the non-record path exec()s and would replace pytest."""
    repo = str(Path(bce.__file__).parents[1])
    script = (f"import sys; sys.path.insert(0, {repo!r}); "
              "from herdr_engineering.build_coord import run_gated; "
              "from herdr_engineering.build_cache_config import validate; "
              f"sys.exit(run_gated({cmd!r}, slots=2, max_load=0, "
              f"state_dir={str(tmp_path / 'state')!r}, "
              f"cache_config=validate({section!r})))")
    return subprocess.run([sys.executable, "-c", script], env=dict(os.environ)).returncode


def _seen(out: Path) -> dict:
    return dict(line.split("=", 1) if "=" in line else line.split(":", 1)
                for line in out.read_text().splitlines())


def test_run_gated_applies_overrides_to_child_only(tmp_path, monkeypatch):
    out = _fake_env(tmp_path, monkeypatch)
    before = dict(os.environ)
    rc = run_gated(["cargo", "build"], state_dir=tmp_path / "state", slots=2, max_load=0,
                   cache_config=_cfg())
    assert rc == 0
    seen = _seen(out)
    assert seen["WRAP"] == str(tmp_path / "bin/sccache")
    assert seen["INC"] == "0" and seen["DIR"] == str(tmp_path / "state/sccache")
    assert seen["TGT"] == "-"
    assert seen["gate.0"] == "HELD" and seen["build-maintenance.lock"] == "HELD"
    assert dict(os.environ) == before
    ev = [json.loads(line) for line in (tmp_path / "state/builds.jsonl").read_text().splitlines()]
    cache = ev[-1]["cache"]
    assert cache["applied"] is True and len(cache["fingerprint"]) == 64
    assert str(tmp_path) not in json.dumps(ev) and "sccache" not in cache["reasons"]


def test_run_gated_propagates_exit_code(tmp_path, monkeypatch):
    _fake_env(tmp_path, monkeypatch)
    assert run_gated(["cargo", "build", "7"], state_dir=tmp_path / "state", slots=2,
                     max_load=0, cache_config=_cfg()) == 7


def test_non_cargo_cmd_env_unchanged_even_when_enabled(tmp_path, monkeypatch):
    _fake_env(tmp_path, monkeypatch)
    out = tmp_path / "o.txt"
    rc = _sub(["sh", "-c", f'echo "${{RUSTC_WRAPPER:-unset}}" > {out}'], tmp_path,
              {"enabled": True})
    assert rc == 0 and out.read_text().strip() == "unset"


def test_cache_config_none_child_env_unchanged(tmp_path, monkeypatch):
    out = _fake_env(tmp_path, monkeypatch)
    script = (f"import sys; sys.path.insert(0, {str(Path(bce.__file__).parents[1])!r}); "
              "from herdr_engineering.build_coord import run_gated; "
              f"sys.exit(run_gated(['cargo','build'], slots=2, max_load=0, "
              f"state_dir={str(tmp_path / 'state')!r}))")
    assert subprocess.run([sys.executable, "-c", script], env=dict(os.environ)).returncode == 0
    seen = _seen(out)
    assert seen["WRAP"] == "-" and seen["INC"] == "-" and seen["DIR"] == "-"
    assert seen["gate.0"] == "HELD"  # exec path still holds the lock


def test_sccache_absent_build_proceeds_uncached(tmp_path, monkeypatch):
    out = _fake_env(tmp_path, monkeypatch)
    (tmp_path / "bin/sccache").unlink()
    rc = _sub(["cargo", "build"], tmp_path, {"enabled": True})
    assert rc == 0 and _seen(out)["WRAP"] == "-"
    assert not (tmp_path / "state/builds.jsonl").exists()  # exec path: no events, as today
    rc = run_gated(["cargo", "build"], state_dir=tmp_path / "state", slots=2, max_load=0,
                   record=True, cache_config=_cfg())
    ev = [json.loads(x) for x in (tmp_path / "state/builds.jsonl").read_text().splitlines()]
    assert rc == 0 and ev[-1]["cache"]["reasons"] == ["sccache_not_found"]


@pytest.mark.skipif(shutil.which("cargo") is None, reason="no real cargo")
def test_real_cargo_version_unmodified(tmp_path, monkeypatch):
    for k in ("RUSTC_WRAPPER", "CARGO_INCREMENTAL"):
        monkeypatch.delenv(k, raising=False)
    assert run_gated(["cargo", "--version"], state_dir=tmp_path / "state", slots=2,
                     max_load=0, record=True, cache_config=_cfg()) == 0
