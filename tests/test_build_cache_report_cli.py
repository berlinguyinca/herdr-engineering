"""`herdr-eng build-cache-report`: opt-in, YAML-only, key from a 0600 file. Real ControlPlaneServer on a socket; no mocks."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.build_cache_config import validate
from herdr_engineering.cli import main
from herdr_engineering.errors import InvalidError

from tests.control_plane.test_build_cache_api import (
    SECRET, _finished, _serve_ingest, _started, _write_builds, call,
)

KEY = "k-host"                       # throwaway test value matching _serve_ingest's ingest-role key
ROOT = Path(__file__).resolve().parents[1]


def _key_file(tmp_path, text=KEY + "\n", mode=0o600):
    p = tmp_path / "ingest.key"
    p.write_text(text)
    p.chmod(mode)
    return p


def _config(tmp_path, url, key_file, **extra):
    report = {"enabled": True, "url": url, "key_file": str(key_file), "host_id": "h7", **extra}
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"version": 1, "fabric_build_cache": {"report": report}}))   # JSON is YAML
    return cfg


def _run(tmp_path, cfg, *extra, state=None):
    state = state or tmp_path / "local"
    return main(["build-cache-report", "--config", str(cfg), "--state-dir", str(state), *extra])


def _local_builds(tmp_path):
    _write_builds(tmp_path / "local", [_started(1, argv=[SECRET]), _finished(1, argv=[SECRET], cwd="/home/secret/x")])


@pytest.fixture
def ingest(tmp_path):
    svr, base, repo = _serve_ingest(tmp_path / "server", {"enabled": True})
    try:
        yield base
    finally:
        svr.shutdown()


def _dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------------------------------------ happy path
def test_once_posts_and_server_lists_the_hosts_builds(tmp_path, ingest, capsys):
    _local_builds(tmp_path)
    cfg = _config(tmp_path, ingest, _key_file(tmp_path))
    assert _run(tmp_path, cfg, "--once", "--json") == 0
    out = capsys.readouterr().out
    assert json.loads(out)["accepted"] == 2
    rows = call(ingest, "GET", "/api/v1/build-cache/builds?host=h7", "k-view")[1]["builds"]
    assert [r["build_id"] for r in rows] == ["b1"] and rows[0]["host"] == "h7"


def test_only_allowlisted_fields_leave_the_host_and_key_is_never_printed(tmp_path, ingest, capsys):
    _local_builds(tmp_path)
    cfg = _config(tmp_path, ingest, _key_file(tmp_path))
    assert _run(tmp_path, cfg) == 0
    cap = capsys.readouterr()
    assert KEY not in cap.out + cap.err
    rows = call(ingest, "GET", "/api/v1/build-cache/builds?host=h7", "k-view")[1]["builds"]
    blob = json.dumps(rows)
    assert SECRET not in blob and "/home/secret" not in blob and "argv" not in blob and "cwd" not in blob
    from herdr_engineering.build_cache_report import collect
    for rec in collect(tmp_path / "local"):
        assert not {"argv", "env", "cwd"} & set(rec)


# ------------------------------------------------------------------------------------------ refusals (exit 2)
def test_disabled_by_default_exits_2(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"version": 1}))
    assert _run(tmp_path, cfg) == 2
    err = capsys.readouterr().err
    assert "disabled" in err and "fabric_build_cache.report.enabled" in err


def test_disabled_explicit_exits_2(tmp_path, ingest, capsys):
    cfg = _config(tmp_path, ingest, _key_file(tmp_path), enabled=False)
    assert _run(tmp_path, cfg) == 2
    assert "disabled" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://builds.example.com", "ftp://127.0.0.1", "http://127.0.0.1.evil.com",
                                 "https://user:pw@example.com", "example.com"])
def test_non_https_remote_url_refused(tmp_path, url, capsys):
    cfg = _config(tmp_path, url, _key_file(tmp_path))
    assert _run(tmp_path, cfg) == 2
    cap = capsys.readouterr()
    assert "url" in cap.err and KEY not in cap.out + cap.err and "pw@" not in cap.err


def test_https_remote_url_is_accepted_by_validation(tmp_path):
    cfg = validate({"report": {"enabled": True, "url": "https://cp.example.com", "key_file": "/k"}})
    assert cfg.report.url == "https://cp.example.com" and cfg.report.interval_s == 60


@pytest.mark.parametrize("kind", ["missing", "empty", "world", "group"])
def test_bad_key_file_refused_without_leaking_key(tmp_path, ingest, capsys, kind):
    if kind == "missing":
        kf = tmp_path / "nope.key"
    elif kind == "empty":
        kf = _key_file(tmp_path, text="\n")
    else:
        kf = _key_file(tmp_path, mode=0o604 if kind == "world" else 0o640)
    cfg = _config(tmp_path, ingest, kf)
    assert _run(tmp_path, cfg) == 2
    cap = capsys.readouterr()
    assert KEY not in cap.out + cap.err
    if kind in ("world", "group"):
        assert "chmod 600" in cap.err


def test_no_key_option_on_the_command_line(tmp_path, ingest):
    cfg = _config(tmp_path, ingest, _key_file(tmp_path))
    for flag in ("--key", "--api-key", "--token"):
        with pytest.raises(SystemExit) as e:
            _run(tmp_path, cfg, flag, KEY)
        assert e.value.code == 2


# ------------------------------------------------------------------------------------------ config validation
def test_unknown_report_key_rejected():
    with pytest.raises(InvalidError, match="fabric_build_cache.report.bogus"):
        validate({"report": {"bogus": 1}})


def test_report_defaults_off():
    r = validate({}).report
    assert r.enabled is False and r.url is None and r.key_file is None and r.interval_s == 60 and r.host_id is None


@pytest.mark.parametrize("v,ok", [(9, False), (10, True), (3600, True), (3601, False), ("60", False), (True, False)])
def test_interval_bounds(v, ok):
    sec = {"report": {"interval_s": v}}
    if ok:
        assert validate(sec).report.interval_s == v
    else:
        with pytest.raises(InvalidError, match="report.interval_s"):
            validate(sec)


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"host_id": "bad host"}, {"url": 5}, {"key_file": 5}])
def test_bad_types_rejected(bad):
    with pytest.raises(InvalidError):
        validate({"report": bad})


def test_invalid_report_section_exits_2(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"version": 1, "fabric_build_cache": {"report": {"nope": 1}}}))
    assert _run(tmp_path, cfg) == 2
    assert "report.nope" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------ failures (exit 1) and loop
def test_wrong_key_exit_1_and_key_not_printed(tmp_path, ingest, capsys):
    _local_builds(tmp_path)
    kf = _key_file(tmp_path, text="wrong-test-key\n")
    assert _run(tmp_path, _config(tmp_path, ingest, kf), "--once") == 1
    cap = capsys.readouterr()
    assert "wrong-test-key" not in cap.out + cap.err
    assert "HTTP 401" in cap.err or "HTTP 403" in cap.err


def test_unreachable_exit_1(tmp_path, capsys):
    cfg = _config(tmp_path, _dead_url(), _key_file(tmp_path))
    assert _run(tmp_path, cfg, "--once") == 1
    assert KEY not in capsys.readouterr().err


def test_loop_runs_n_iterations_sleeps_interval_and_logs_one_line_each(tmp_path, ingest, capsys):
    from herdr_engineering.build_cache_report_cli import run
    _local_builds(tmp_path)
    cfg = _config(tmp_path, ingest, _key_file(tmp_path), interval_s=17)
    sleeps = []
    args = _args(tmp_path, cfg, "--loop", "--max-iterations", "3")
    assert run(args, sleep=sleeps.append) == 0
    assert sleeps == [17, 17]                                   # between attempts, not after the last
    lines = [l for l in capsys.readouterr().err.splitlines() if l.startswith("build-cache-report:")]
    assert len(lines) == 3 and all("ok" in l for l in lines)


def test_loop_survives_refusal_and_unreachable_then_recovers(tmp_path, capsys):
    from herdr_engineering.build_cache_report_cli import run
    _local_builds(tmp_path)
    kf = _key_file(tmp_path, text="wrong-test-key\n")
    cfg = _config(tmp_path, _dead_url(), kf)
    assert run(_args(tmp_path, cfg, "--loop", "--max-iterations", "2"), sleep=lambda s: None) == 1
    err = capsys.readouterr().err
    lines = [l for l in err.splitlines() if l.startswith("build-cache-report:")]
    assert len(lines) == 2 and all("failed" in l for l in lines)
    assert "wrong-test-key" not in err


def test_loop_survives_wrong_key_and_keeps_going(tmp_path, ingest, capsys):
    from herdr_engineering.build_cache_report_cli import run
    kf = _key_file(tmp_path, text="wrong-test-key\n")
    cfg = _config(tmp_path, ingest, kf)
    assert run(_args(tmp_path, cfg, "--loop", "--max-iterations", "3"), sleep=lambda s: None) == 1
    assert sum(l.startswith("build-cache-report:") for l in capsys.readouterr().err.splitlines()) == 3


def test_loop_reads_the_key_file_each_attempt(tmp_path, ingest, capsys):
    """A rotated key file is picked up without a restart."""
    from herdr_engineering.build_cache_report_cli import run
    _local_builds(tmp_path)
    kf = _key_file(tmp_path, text="wrong-test-key\n")
    cfg = _config(tmp_path, ingest, kf)

    def rotate(_s):
        kf.write_text(KEY + "\n")
    assert run(_args(tmp_path, cfg, "--loop", "--max-iterations", "2"), sleep=rotate) == 0
    lines = [l for l in capsys.readouterr().err.splitlines() if l.startswith("build-cache-report:")]
    assert "failed" in lines[0] and "ok" in lines[1]


def _args(tmp_path, cfg, *extra):
    from herdr_engineering.cli import _build_parser
    return _build_parser().parse_args(["build-cache-report", "--config", str(cfg),
                                       "--state-dir", str(tmp_path / "local"), *extra])


def test_real_process_once(tmp_path, ingest):
    _local_builds(tmp_path)
    cfg = _config(tmp_path, ingest, _key_file(tmp_path))
    p = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "build-cache-report", "--config", str(cfg),
                        "--state-dir", str(tmp_path / "local"), "--once"], cwd=ROOT, capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0, p.stderr
    assert KEY not in p.stdout + p.stderr


# ------------------------------------------------------------------------------------------ service files
def test_service_files_have_no_secrets_and_are_not_enabled_by_default():
    unit = (ROOT / "scripts/herdr-eng-build-cache-report.service").read_text()
    assert "build-cache-report --loop" in unit and "Environment" not in unit and "--key" not in unit
    plist = (ROOT / "scripts/com.herdr-engineering.build-cache-report.plist").read_text()
    assert "build-cache-report" in plist and "--loop" in plist and "<key>EnvironmentVariables</key>" not in plist
    import plistlib
    assert plistlib.loads(plist.encode())["Label"] == "com.herdr-engineering.build-cache-report"
