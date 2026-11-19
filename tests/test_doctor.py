"""Doctor command tests (spec 0050/0170)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import json

from herdr_engineering.config import DEFAULTS
from herdr_engineering.doctor import DoctorReport, run_doctor


def test_doctor_report_structure():
    report = run_doctor(config=dict(DEFAULTS))
    d = report.to_dict()
    assert d["overall"] in ("ok", "warn", "fail")
    assert isinstance(d["checks"], list)
    names = {c["name"] for c in d["checks"]}
    assert {"config", "lock", "herdr", "listeners", "workspace"} <= names
    assert not any(c["status"] == "fail" for c in d["checks"])  # environment-safe


def test_doctor_no_secrets_in_output():
    report = run_doctor(config=dict(DEFAULTS))
    blob = json.dumps(report.to_dict())
    for secret in ("password", "token=", "Bearer", "id_rsa", "-----BEGIN"):
        assert secret.lower() not in blob.lower()


def test_human_render():
    r = DoctorReport()
    r.add("x", "ok", "fine")
    out = r.render_human()
    assert "overall" in out


def test_missing_handoff_is_a_warning_unless_strict(monkeypatch):
    from herdr_engineering.handoff_dependency import HandoffDependencyResult

    monkeypatch.setattr("herdr_engineering.handoff_dependency.check_handoff_dependency",
                        lambda: HandoffDependencyResult("fail", "missing integration"))
    for strict, expected in ((False, "warn"), (True, "fail")):
        report = run_doctor(config=dict(DEFAULTS), strict=strict).to_dict()
        check = next(item for item in report["checks"] if item["name"] == "handoff")
        assert check["status"] == expected


def test_build_cache_toolchain_checks_are_read_only_warnings(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no sccache/rustc/cargo anywhere
    checks = {c["name"]: c for c in run_doctor(config=dict(DEFAULTS)).to_dict()["checks"]}
    assert checks["build_cache_sccache"]["status"] == "warn"
    assert "scripts/install.sh --build-cache-tools-only" in checks["build_cache_sccache"]["detail"]
    assert checks["build_cache_rust"]["status"] == "warn"
    assert checks["build_cache_python_deps"]["status"] == "ok"


def test_build_cache_toolchain_old_sccache_warns_new_ok(monkeypatch, tmp_path):
    exe = tmp_path / "sccache"
    for ver, want in (("0.18.0", "warn"), ("0.19.0", "ok")):
        exe.write_text(f"#!/bin/sh\necho 'sccache {ver}'\n")
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        checks = {c["name"]: c for c in run_doctor(config=dict(DEFAULTS)).to_dict()["checks"]}
        assert checks["build_cache_sccache"]["status"] == want


# --- cwd independence and tailscale decision (real files, real cwd) -------------------------------------

import pytest  # noqa: E402

from herdr_engineering import doctor as doctor_mod  # noqa: E402

# Minimal shape of `tailscale status --json` with made-up values (only the fields doctor reads).
_TS_RUNNING = json.dumps({"Version": "1.0.0", "BackendState": "Running", "Self": {"HostName": "example-host"}})
_TS_STOPPED = json.dumps({"Version": "1.0.0", "BackendState": "Stopped", "Self": {"HostName": "example-host"}})
_TS_NEEDS_LOGIN = json.dumps({"Version": "1.0.0", "BackendState": "NeedsLogin"})


@pytest.mark.parametrize("rc,out,want", [
    (0, _TS_RUNNING, True),
    (0, _TS_STOPPED, False),
    (0, _TS_NEEDS_LOGIN, False),
    (1, _TS_RUNNING, True),                 # the JSON is authoritative when it parses
    (0, "not json at all", True),           # unparseable: fall back to the exit code
    (1, "not json at all", False),
    (0, "", True),
    (0, json.dumps(["a list"]), True),      # parses but is not the status object: exit code decides
    (0, json.dumps({"Version": "1.0.0"}), True),   # no BackendState: exit code decides
])
def test_tailscale_running_decision(rc, out, want):
    assert doctor_mod.tailscale_running(rc, out) is want


def test_tailscale_up_is_false_without_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert doctor_mod._tailscale_up() is False


def test_tailscale_up_reads_real_json_from_an_executable(monkeypatch, tmp_path):
    exe = tmp_path / "tailscale"
    exe.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'status --json' ] || exit 2\necho '{_TS_RUNNING}'\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert doctor_mod._tailscale_up() is True
    exe.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'status --json' ] || exit 2\necho '{_TS_STOPPED}'\n")
    assert doctor_mod._tailscale_up() is False


def test_resolve_lock_path_precedence(tmp_path):
    (tmp_path / "pkg" / "lock").mkdir(parents=True)
    (tmp_path / "pkg" / "lock" / "upstreams.yaml").write_text("dependencies: {}\n")
    (tmp_path / "repo" / "lock").mkdir(parents=True)
    (tmp_path / "repo" / "lock" / "upstreams.yaml").write_text("dependencies: {}\n")
    explicit = tmp_path / "x.yaml"
    pkg = tmp_path / "pkg"
    assert doctor_mod.resolve_lock_path({"HERDR_ENGINEERING_LOCK": str(explicit)}, pkg) == explicit
    assert doctor_mod.resolve_lock_path({"HERDR_ENGINEERING_REPO": str(tmp_path / "repo")}, pkg) \
        == tmp_path / "repo" / "lock" / "upstreams.yaml"
    assert doctor_mod.resolve_lock_path({}, pkg) == pkg / "lock" / "upstreams.yaml"
    assert doctor_mod.resolve_lock_path({}, tmp_path / "site-packages") is None   # installed copy, no checkout


def test_doctor_lock_and_plugins_do_not_depend_on_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_ENGINEERING_LOCK", raising=False)
    monkeypatch.delenv("HERDR_ENGINEERING_REPO", raising=False)
    monkeypatch.chdir(tmp_path)   # an empty directory that is not a checkout
    checks = {c["name"]: c for c in run_doctor(config=dict(DEFAULTS)).to_dict()["checks"]}
    assert checks["lock"]["status"] == "ok"
    assert checks["plugins"]["status"] == "ok"


def test_doctor_reports_not_applicable_when_no_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_ENGINEERING_LOCK", raising=False)
    monkeypatch.delenv("HERDR_ENGINEERING_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    report = run_doctor(config=dict(DEFAULTS), package_root=tmp_path / "site-packages")
    checks = {c["name"]: c for c in report.to_dict()["checks"]}
    for name in ("lock", "plugins"):
        assert checks[name]["status"] == "unknown"
        assert "not applicable" in checks[name]["detail"]
