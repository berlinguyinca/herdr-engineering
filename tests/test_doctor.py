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
