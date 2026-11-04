"""Hindsight status checks for `herdr-eng doctor`: every outcome from injected docker/http/clock, tmp HOME only.
The invariants: unreachable is `unknown` (never ok), learned facts are warn/fail, no secret is read or shown."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from herdr_engineering import doctor
from herdr_engineering import memory_hindsight_status as ms

NOW = 1_800_000_000.0


def proc(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class Docker:
    def __init__(self, **by_name):
        self.by_name = by_name
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "systemctl":
            return self.by_name.get("systemctl", proc(0, "active\n"))
        return self.by_name.get(cmd[-1], proc(0, "running false healthy\n"))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".local/state/herdr-engineering/memory").mkdir(parents=True)
    (h / ".local/state/herdr-engineering/memory/deployed.json").write_text(
        json.dumps({"image_tag": "abc", "port": 8888}))
    d = h / ".local/share/herdr-memory/backups"
    d.mkdir(parents=True)
    f = d / "memory-20261101T000000.000000Z.dump"
    f.write_text("dump")
    os.utime(f, (NOW - 3600, NOW - 3600))
    return h


def run_checks(home, docker=None, http=None, which=lambda n: "/usr/bin/" + n, now=NOW, fleet=lambda k, d=None: None):
    return {c["name"]: c for c in ms.check_memory_backend(
        home, run=docker or Docker(), which=which, http_get=http or (lambda u: 200), now=lambda: now,
        fleet_value=fleet)}


def test_a_host_without_the_backend_or_a_client_config_reports_nothing(tmp_path):
    assert ms.check_memory_backend(tmp_path, run=Docker(), which=lambda n: "x", http_get=lambda u: 200) == []


def test_all_healthy_is_all_ok(home):
    c = run_checks(home)
    assert {k: v["status"] for k, v in c.items()} == {
        "memory_deploy": "ok", "memory_container:herdr-hindsight-db": "ok", "memory_container:herdr-hindsight": "ok",
        "memory_health": "ok", "memory_backup": "ok", "memory_backup_timer": "ok"}


def test_docker_unavailable_is_unknown_not_ok(home):
    c = run_checks(home, which=lambda n: None)
    assert c["memory_container:herdr-hindsight"]["status"] == "unknown"
    assert c["memory_container:herdr-hindsight-db"]["status"] == "unknown"
    assert c["memory_backup_timer"]["status"] == "unknown"


def test_daemon_not_reachable_is_unknown_but_missing_container_is_a_fail(home):
    c = run_checks(home, Docker(**{"herdr-hindsight": proc(1, "", "permission denied while trying to connect"),
                                   "herdr-hindsight-db": proc(1, "", "Error: No such object: herdr-hindsight-db")}))
    assert c["memory_container:herdr-hindsight"]["status"] == "unknown"
    assert c["memory_container:herdr-hindsight-db"]["status"] == "fail"


@pytest.mark.parametrize("out,status", [("running false healthy", "ok"), ("running false starting", "warn"),
                                        ("running false unhealthy", "fail"), ("exited false none", "fail"),
                                        ("running true healthy", "fail"), ("running false none", "unknown"),
                                        ("garbage", "unknown")])
def test_container_states(home, out, status):
    assert run_checks(home, Docker(**{"herdr-hindsight": proc(0, out)}))["memory_container:herdr-hindsight"]["status"] == status


def test_docker_timeout_is_unknown(home):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert run_checks(home, boom)["memory_container:herdr-hindsight"]["status"] == "unknown"


def test_health_unreachable_is_unknown_and_non_200_is_fail(home):
    def down(url):
        raise OSError("URLError")
    assert run_checks(home, http=down)["memory_health"]["status"] == "unknown"
    assert run_checks(home, http=lambda u: 503)["memory_health"]["status"] == "fail"
    seen = []
    run_checks(home, http=lambda u: seen.append(u) or 200)
    assert seen == ["http://127.0.0.1:8888/health"]               # loopback only


@pytest.mark.parametrize("age_h,status", [(1, "ok"), (35, "ok"), (37, "warn"), (24 * 8, "fail")])
def test_backup_age_thresholds(home, age_h, status):
    c = run_checks(home, now=NOW - 3600 + age_h * 3600)
    assert c["memory_backup"]["status"] == status
    assert c["memory_backup"]["data"]["age_seconds"] == age_h * 3600


def test_no_backup_yet_is_a_warning_and_unreadable_dir_is_unknown(home):
    for f in (home / ".local/share/herdr-memory/backups").iterdir():
        f.unlink()
    assert run_checks(home)["memory_backup"]["status"] == "warn"
    d = home / ".local/share/herdr-memory/backups"
    d.rmdir()
    assert run_checks(home)["memory_backup"]["status"] == "warn"
    d.parent.chmod(0)
    try:
        assert run_checks(home)["memory_backup"]["status"] == "unknown"
    finally:
        d.parent.chmod(0o755)


def test_timer_states(home):
    assert run_checks(home, Docker(systemctl=proc(3, "inactive\n")))["memory_backup_timer"]["status"] == "warn"
    assert run_checks(home, Docker(systemctl=proc(1, "")))["memory_backup_timer"]["status"] == "unknown"


def test_unreadable_marker_is_unknown(home):
    (home / ".local/state/herdr-engineering/memory/deployed.json").write_text("{not json")
    assert run_checks(home)["memory_deploy"]["status"] == "unknown"
    assert "memory_health" not in run_checks(home)


# ---- client side ------------------------------------------------------------------------------------------------

def client_home(tmp_path, url):
    h = tmp_path / "client"
    (h / ".config/herdr-memory").mkdir(parents=True)
    (h / ".config/herdr-memory/config.json").write_text(json.dumps({"url": url}))
    return h


def test_client_endpoint_https_ok_unreachable_unknown(tmp_path):
    h = client_home(tmp_path, "https://memory.dev.lan")
    assert run_checks(h)["memory_endpoint"]["status"] == "ok"

    def down(url):
        raise OSError("x")
    assert run_checks(h, http=down)["memory_endpoint"]["status"] == "unknown"
    assert run_checks(h, http=lambda u: 502)["memory_endpoint"]["status"] == "fail"


@pytest.mark.parametrize("url", ["http://memory.dev.lan", "http://100.64.0.7:8888", "ftp://x", "https://u:p@x.test"])
def test_client_plain_http_off_loopback_is_a_fail_and_is_never_probed(tmp_path, url):
    h = client_home(tmp_path, url)
    probed = []
    c = run_checks(h, http=lambda u: probed.append(u) or 200)
    assert c["memory_endpoint"]["status"] == "fail" and not probed


def test_loopback_http_is_accepted(tmp_path):
    assert ms.valid_endpoint("http://127.0.0.1:8888/") == "http://127.0.0.1:8888"
    assert ms.valid_endpoint("http://localhost:8888") == "http://localhost:8888"
    assert ms.valid_endpoint("http://[::1]:8888") == "http://[::1]:8888"
    assert ms.valid_endpoint("http://memory.dev.lan") is None


def test_discover_endpoint_prefers_the_users_config_then_the_fleet_slice(tmp_path):
    h = client_home(tmp_path, "https://mine.example.test")
    assert ms.discover_endpoint(h, lambda k, d=None: "https://fleet.example.test") == {
        "url": "https://mine.example.test", "source": "herdr-memory-config", "problem": None}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ms.discover_endpoint(empty, lambda k, d=None: "https://memory.dev.lan") == {
        "url": "https://memory.dev.lan", "source": "fleet", "problem": None}
    assert ms.discover_endpoint(empty, lambda k, d=None: None)["url"] is None
    assert ms.discover_endpoint(empty, lambda k, d=None: "http://memory.dev.lan")["problem"]


def test_discover_endpoint_reads_the_cached_fleet_slice_without_network(tmp_path):
    h = tmp_path / "enrolled"
    (h / ".config/herdr-engineering").mkdir(parents=True)
    (h / ".config/herdr-engineering/host.json").write_text(json.dumps(
        {"control_plane_url": "https://cp.example.test", "host_id": "bender", "token": "t" * 20}))
    (h / ".cache/herdr-engineering").mkdir(parents=True)
    (h / ".cache/herdr-engineering/fleet-config.json").write_text(json.dumps(
        {"host_id": "bender", "revision": "r", "values": {"memory": {"url": "https://memory.dev.lan"}}}))
    assert ms.discover_endpoint(h)["url"] == "https://memory.dev.lan"
    (h / ".cache/herdr-engineering/fleet-config.json").unlink()
    assert ms.discover_endpoint(h)["url"] is None


# ---- doctor wiring ----------------------------------------------------------------------------------------------

def test_doctor_includes_memory_checks_and_never_crashes_on_them(monkeypatch):
    monkeypatch.setattr(ms, "check_memory_backend", lambda: [
        {"name": "memory_health", "status": "unknown", "detail": "loopback /health unreachable", "data": {}}])
    rep = doctor.run_doctor(config={}).to_dict()
    chk = next(c for c in rep["checks"] if c["name"] == "memory_health")
    assert chk["status"] == "unknown"
    monkeypatch.setattr(ms, "check_memory_backend", lambda: 1 / 0)
    rep = doctor.run_doctor(config={}).to_dict()
    assert next(c for c in rep["checks"] if c["name"] == "memory_hindsight")["status"] == "warn"
