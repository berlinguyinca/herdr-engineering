"""Judge key registration, status/check routes and the ``judge_status`` tool (Phase 3, Task 7).

No mocks: the CLI runs as a real subprocess in a throwaway HOME, the "judge" is a raw socket answering the documented
System One shape (or a closed port, a real connect failure), and the control plane + hub are real servers.  Every key
is a random token generated here and asserted absent from all output."""
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.mcp import Hub
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.judge_support import (
    SAFE,
    WireListener,
    closed_port,
    judge_cfg,
    make_runtime,
    satisfy_gate,
    success_body,
    write_key,
)
from tests.control_plane.test_assistant_hub_phase2_owner_routes import (
    KEYS,
    ROLES,
    action_request,
    call,
    mcp,
    new_worker,
    owner,
    pid,
)

REPO = Path(__file__).resolve().parents[2]


def fp(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def new_key() -> str:
    return "tk-" + secrets.token_urlsafe(24)


def env_for(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "PYTHONPATH": os.pathsep.join([str(REPO), *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]])}


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture()
def keydir(tmp_path: Path) -> Path:
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    return d


def run_register(home: Path, tmp_path: Path, argv: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "control-plane", "assistant-judge", "register", *argv],
                          input=stdin, env=env_for(home), capture_output=True, text=True, timeout=60, cwd=str(tmp_path))


def reg_args(port: int, path: Path, *extra: str) -> list[str]:
    return ["--endpoint", f"http://127.0.0.1:{port}", "--key-file", str(path), "--key-stdin", *extra]


@pytest.fixture()
def jev():
    made: list[WireListener] = []

    def make(**kw) -> WireListener:
        li = WireListener(success_body(SAFE), **kw)
        made.append(li)
        return li
    yield make
    for li in made:
        li.close()


def no_leak(done: subprocess.CompletedProcess[str], *secrets_: str) -> None:
    for s in secrets_:
        assert s not in done.stdout + done.stderr


# ------------------------------------------------------------------------------------------------- register

def test_register_writes_0600_file_prints_fingerprint_only(home, tmp_path, keydir, jev):
    li = jev(models=["laya-auto", "other"])
    key, path = new_key(), keydir / "jev.key"
    argv = reg_args(li.port, path)
    assert key not in " ".join(argv)
    done = run_register(home, tmp_path, argv, key + "\n")
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text().strip() == key
    assert not Path(str(path) + ".new").exists()
    assert done.stdout.splitlines()[0] == f"registered: fingerprint={fp(key)} model=laya-auto models_seen=2"
    assert "api_key_file" in done.stdout and "backend_confirmed" in done.stdout
    no_leak(done, key)
    assert li.hits == 1 and f"Bearer {key}".encode() in li.seen[0]          # health-checked with the NEW key
    assert list(home.iterdir()) == []                                       # nothing written into HOME / no YAML edit


def test_register_unreachable_leaves_nothing(home, tmp_path, keydir):
    key, path = new_key(), keydir / "jev.key"
    done = run_register(home, tmp_path, reg_args(closed_port(), path), key)
    assert done.returncode == 1
    assert len(done.stderr.strip().splitlines()) == 1 and "connect" in done.stderr
    assert not path.exists() and not Path(str(path) + ".new").exists()
    no_leak(done, key)


def test_register_model_not_listed_prints_first_ten_ids(home, tmp_path, keydir, jev):
    ids = [f"canonical-{i:02d}" for i in range(12)]
    li = jev(models=ids)
    key, path = new_key(), keydir / "jev.key"
    done = run_register(home, tmp_path, reg_args(li.port, path), key)
    assert done.returncode == 1 and "model_not_listed" in done.stderr
    assert all(i in done.stderr for i in ids[:10]) and ids[10] not in done.stderr
    assert not path.exists() and not Path(str(path) + ".new").exists()
    no_leak(done, key)


def test_register_model_flag_selects_the_listed_id(home, tmp_path, keydir, jev):
    li = jev(models=["canonical-a", "canonical-b"])
    key, path = new_key(), keydir / "jev.key"
    done = run_register(home, tmp_path, reg_args(li.port, path, "--model", "canonical-b"), key)
    assert done.returncode == 0, done.stderr
    assert "model=canonical-b" in done.stdout and path.exists()


def test_rotation_replaces_only_after_health_check(home, tmp_path, keydir, jev):
    old, new, path = new_key(), new_key(), keydir / "jev.key"
    path.write_text(old + "\n")
    path.chmod(0o600)
    li = jev(models=["laya-auto"], require_key=new)
    done = run_register(home, tmp_path, reg_args(li.port, path), new)
    assert done.returncode == 0, done.stderr
    assert path.read_text().strip() == new and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not Path(str(path) + ".new").exists()
    no_leak(done, old, new)


def test_rotation_failure_keeps_old_file(home, tmp_path, keydir, jev):
    old, new, path = new_key(), new_key(), keydir / "jev.key"
    path.write_text(old + "\n")
    path.chmod(0o600)
    li = jev(models=["laya-auto"], require_key=old)           # the gateway rejects the new key
    done = run_register(home, tmp_path, reg_args(li.port, path), new)
    assert done.returncode == 1 and "auth" in done.stderr
    assert path.read_text().strip() == old and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not Path(str(path) + ".new").exists()
    no_leak(done, old, new)


def test_register_refuses_symlink_target(home, tmp_path, keydir, jev):
    li = jev(models=["laya-auto"])
    real = keydir / "real.key"
    real.write_text("untouched\n")
    real.chmod(0o600)
    link = keydir / "jev.key"
    link.symlink_to(real)
    key = new_key()
    done = run_register(home, tmp_path, reg_args(li.port, link), key)
    assert done.returncode == 1 and "symlink" in done.stderr
    assert real.read_text() == "untouched\n" and li.hits == 0
    no_leak(done, key)


def test_register_refuses_world_writable_parent(home, tmp_path, jev):
    li = jev(models=["laya-auto"])
    bad = tmp_path / "open"
    bad.mkdir()
    bad.chmod(0o777)
    key = new_key()
    done = run_register(home, tmp_path, reg_args(li.port, bad / "jev.key"), key)
    assert done.returncode == 1 and "unsafe_dir" in done.stderr
    assert list(bad.iterdir()) == [] and li.hits == 0


def test_register_refuses_stale_new_file_and_relative_path(home, tmp_path, keydir, jev):
    li = jev(models=["laya-auto"])
    path = keydir / "jev.key"
    stale = Path(str(path) + ".new")
    stale.write_text("stale\n")
    done = run_register(home, tmp_path, reg_args(li.port, path), new_key())
    assert done.returncode == 1 and "tmp_exists" in done.stderr and stale.read_text() == "stale\n" and not path.exists()
    done = run_register(home, tmp_path, reg_args(li.port, Path("rel.key")), new_key())
    assert done.returncode == 1 and "bad_path" in done.stderr
    assert li.hits == 0


def test_register_usage_errors_exit_2(home, tmp_path, keydir, jev):
    li = jev(models=["laya-auto"])
    path = keydir / "jev.key"
    assert run_register(home, tmp_path, reg_args(li.port, path), "").returncode == 2           # empty stdin
    assert run_register(home, tmp_path, reg_args(li.port, path), "has space\n").returncode == 2
    no_stdin = ["--endpoint", f"http://127.0.0.1:{li.port}", "--key-file", str(path)]
    assert run_register(home, tmp_path, no_stdin, new_key()).returncode == 2                  # --key-stdin is required
    leaked = run_register(home, tmp_path, [*no_stdin, "--key", "abc"], "")
    assert leaked.returncode == 2 and "abc" not in leaked.stdout                              # there is no argv key option
    assert not path.exists() and li.hits == 0


# ------------------------------------------------------------------------------------------------- routes + tool

@pytest.fixture()
def world(acknowledged_sink, tmp_path):
    long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    section = {"enabled": True, "tenancy": {"principals": {pid("alice"): "acme", pid("bea"): "globex", pid("carol"): "acme"}},
               "grants": {"shadow_started_at": long_ago},
               "hub": {"enabled": True, "host": "100.64.0.9", "port": 9483, "allowed_networks": ["127.0.0.0/8"]}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    li = WireListener(success_body(SAFE), models=["laya-auto"])
    key_file = write_key(tmp_path)
    cfg = judge_cfg(f"http://127.0.0.1:{li.port}/private/path", key_file)
    rt = make_runtime(svc.store, cfg, svc.now)
    hub = HubServer(Hub(registry=registry, service=svc, store=svc.store, judge=rt), host="127.0.0.1", port=0,
                    allowed_networks=["127.0.0.0/8"], allowed_hosts=[])
    hub.start()
    hub.wait_until_ready()
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc, api_keys=ROLES,
                             assistant_workers=registry, assistant_judge=rt)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    w = type("W", (), {})()
    w.base, w.hub_base, w.svc, w.rt, w.li, w.cfg, w.key_file = (
        f"http://127.0.0.1:{svr.port}", f"http://127.0.0.1:{hub.port}", svc, rt, li, cfg, key_file)
    yield w
    svr.shutdown()
    hub.shutdown()
    rt.recorder.stop()
    li.close()


STATUS = "/api/v1/assistant-judges"
CHECK = "/api/v1/assistant-judges/check"


def test_status_shape_and_no_secrets(world):
    status, out = owner(world.base, "GET", STATUS)
    assert status == 200
    assert set(out) == {"enabled", "model", "judge_version", "backend_confirmed", "endpoint_host", "key_fingerprint",
                        "recorder", "calibration", "gate"}
    assert out["enabled"] is True and out["model"] == "laya-auto" and out["backend_confirmed"] is True
    assert out["endpoint_host"] == "127.0.0.1" and out["recorder"] == {"queue": 0, "dropped": 0}
    key = world.key_file.read_text().strip()
    assert out["key_fingerprint"] == fp(key)
    assert set(out["gate"]) == {"local_reversible", "remote_reversible", "outward"}
    assert out["gate"]["outward"] == {"satisfied": False, "code": "insufficient_labels"}
    text = json.dumps(out)
    assert key not in text and str(world.key_file) not in text and "private" not in text and "http" not in text


def test_fingerprint_follows_rotation_and_is_empty_without_a_file(world):
    new = new_key()
    world.key_file.write_text(new + "\n")
    assert owner(world.base, "GET", STATUS)[1]["key_fingerprint"] == fp(new)
    world.key_file.chmod(0o644)                     # unusable file: no fingerprint, no error
    assert owner(world.base, "GET", STATUS)[1]["key_fingerprint"] == ""
    world.key_file.unlink()
    assert owner(world.base, "GET", STATUS)[1]["key_fingerprint"] == ""


@pytest.mark.parametrize("method,path", [("GET", STATUS), ("POST", CHECK)])
def test_routes_operator_only(world, method, path):
    body = {} if method == "POST" else None
    assert call(world.base, method, path, body)[0] == 401
    assert owner(world.base, method, path, body, who="viewer")[0] == 403
    wid, cred, device = new_worker(world.base, world.hub_base)
    for headers in ({"Authorization": f"Worker {cred}", "X-Herdr-Device": device}, {"X-API-Key": cred}):
        assert call(world.base, method, path, body, headers)[0] in (401, 403)
    assert owner(world.base, method, path, body)[0] == 200


def test_check_cross_origin_refused_and_unknown_paths_404(world):
    status, _ = call(world.base, "POST", CHECK, {}, {"X-API-Key": KEYS["alice"], "Origin": "http://evil.example"})
    assert status == 403
    assert owner(world.base, "GET", STATUS + "/nope")[0] == 404
    assert owner(world.base, "POST", STATUS, {})[0] == 404
    assert owner(world.base, "DELETE", STATUS)[0] == 404
    assert owner(world.base, "POST", CHECK, {"x": 1})[0] == 400


def test_check_runs_one_harmless_consult_and_records_nothing(world):
    wid, cred, device = new_worker(world.base, world.hub_base, scopes=["ask", "report", "read", "act"])
    grants_before = world.svc.store.list_action_grants("acme", pid("alice"))
    status, out = owner(world.base, "POST", CHECK, {})
    assert status == 200 and out["ok"] is True and out["code"] == "" and isinstance(out["latency_ms"], int)
    assert world.li.hits == 1
    sent = world.li.seen[0].split(b"\r\n\r\n", 1)[1]
    assert b"shell.read_only" in sent and b"local_reversible" in sent
    assert world.svc.store.list_judge_records("acme", pid("alice")) == []
    assert world.svc.store.list_action_grants("acme", pid("alice")) == grants_before
    assert world.rt.recorder.submitted == []
    assert str(world.key_file) not in json.dumps(out)


def test_check_failures_are_codes_not_bodies(world):
    world.cfg["endpoint"] = f"http://127.0.0.1:{closed_port()}"
    status, out = owner(world.base, "POST", CHECK, {})
    assert status == 200 and out["ok"] is False and out["code"] == "connect"
    world.cfg["enabled"] = False
    out = owner(world.base, "POST", CHECK, {})[1]
    assert out["ok"] is False and out["code"] == "judge_disabled" and world.li.hits == 0


def test_status_is_scoped_to_tenant_and_user(world):
    satisfy_gate(world.svc.store, world.rt, "remote_reversible", tenant="acme", user=pid("alice"))
    mine = owner(world.base, "GET", STATUS)[1]
    assert mine["calibration"]["classes"]["remote_reversible"]["labeled"] == 210
    assert mine["gate"]["remote_reversible"] == {"satisfied": True, "code": ""}
    for who in ("carol", "bea"):                       # same tenant other user, other tenant
        theirs = owner(world.base, "GET", STATUS, who=who)[1]
        assert theirs["calibration"]["classes"] == {}
        assert theirs["gate"]["remote_reversible"] == {"satisfied": False, "code": "insufficient_labels"}


def test_service_disabled_503(acknowledged_sink):
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), api_keys=ROLES)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        base = f"http://127.0.0.1:{svr.port}"
        assert owner(base, "GET", STATUS)[0] == 503
        assert owner(base, "POST", CHECK, {})[0] == 503
    finally:
        svr.shutdown()


def test_status_without_a_runtime_reports_config_only(acknowledged_sink, tmp_path):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True, "tenancy": {
        "principals": {pid("alice"): "acme"}}}}))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc, api_keys=ROLES)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        base = f"http://127.0.0.1:{svr.port}"
        out = owner(base, "GET", STATUS)[1]
        assert out["enabled"] is False and out["recorder"] == {"queue": 0, "dropped": 0}
        assert owner(base, "POST", CHECK, {})[1]["code"] == "judge_disabled"
    finally:
        svr.shutdown()


def run_cli(capsys, world, *argv):
    capsys.readouterr()
    keyfile = world.key_file.parent / "owner.key"
    keyfile.write_text(KEYS["alice"])
    rc = cli.main(["control-plane", "assistant-judge", *argv, "--control-plane", world.base, "--api-key-file", str(keyfile)])
    out = capsys.readouterr()
    assert KEYS["alice"] not in out.out + out.err
    return rc, out.out, out.err


def test_cli_status_and_check_against_a_real_server(world, capsys):
    satisfy_gate(world.svc.store, world.rt, "remote_reversible", tenant="acme", user=pid("alice"))
    rc, out, err = run_cli(capsys, world, "status")
    assert rc == 0, err
    for needle in ("enabled", "laya-auto", "backend_confirmed", "127.0.0.1", "remote_reversible", "210"):
        assert needle in out
    assert world.key_file.read_text().strip() not in out and str(world.key_file) not in out
    rc, out, err = run_cli(capsys, world, "check")
    assert rc == 0 and "ok" in out, err
    world.cfg["endpoint"] = f"http://127.0.0.1:{closed_port()}"
    rc, out, err = run_cli(capsys, world, "check")
    assert rc == 1 and "connect" in out + err


def test_cli_status_needs_a_key_file(world, capsys):
    capsys.readouterr()
    assert cli.main(["control-plane", "assistant-judge", "status", "--control-plane", world.base]) == 2


def judge_tool(world, cred, device):
    status, body = mcp(world.hub_base, cred, device, "tools/call", {"name": "judge_status", "arguments": {}})
    assert status == 200
    return body["result"]


def test_judge_status_tool_is_read_scoped_and_aggregate_only(world):
    satisfy_gate(world.svc.store, world.rt, "remote_reversible", tenant="acme", user=pid("alice"))
    reader = new_worker(world.base, world.hub_base, scopes=["read"], device="d-read")
    tools = mcp(world.hub_base, reader[1], reader[2], "tools/list")[1]["result"]["tools"]
    assert "judge_status" in {t["name"] for t in tools}
    result = judge_tool(world, reader[1], reader[2])
    assert result["isError"] is False
    text = result["content"][0]["text"]
    doc = json.loads(text[text.index("{"):])
    assert set(doc) == {"enabled", "mode", "judge_version", "labeled_by_class", "gate"}
    assert doc["enabled"] is True and doc["mode"] == "shadow" and doc["judge_version"]
    assert doc["labeled_by_class"] == {"local_reversible": 0, "remote_reversible": 210, "outward": 0}
    assert doc["gate"] == {"local_reversible": False, "remote_reversible": True, "outward": False}
    assert world.key_file.read_text().strip() not in text and str(world.key_file) not in text and "127.0.0.1" not in text

    asker = new_worker(world.base, world.hub_base, scopes=["ask"], device="d-ask")
    assert "judge_status" not in {t["name"] for t in mcp(world.hub_base, asker[1], asker[2], "tools/list")[1]["result"]["tools"]}
    denied = judge_tool(world, asker[1], asker[2])
    assert denied["isError"] is True and "scope" in denied["content"][0]["text"]


def test_judge_status_tool_shows_no_per_request_data(world):
    worker = new_worker(world.base, world.hub_base, scopes=["read", "act"], device="d-act")
    out = action_request((world.hub_base), worker, "frobnicate --all --token=hunter2-CANARY")
    result = judge_tool(world, worker[1], worker[2])
    text = result["content"][0]["text"]
    assert out["request_id"] not in text and "frobnicate" not in text and "hunter2" not in text
    assert "probab" not in text and "destructive" not in text and "request" not in text


def test_judge_status_tool_without_a_runtime_is_disabled():
    from tests.control_plane.test_assistant_hub_mcp import call as tool_call
    from tests.control_plane.test_assistant_hub_mcp import enrol, make_hub
    hub = make_hub()                                  # no judge runtime
    result = tool_call(hub, enrol(hub, scopes=("read",)), "judge_status")
    text = result["content"][0]["text"]
    doc = json.loads(text[text.index("{"):])
    assert doc["enabled"] is False and doc["gate"] == {"local_reversible": False, "remote_reversible": False, "outward": False}


def _wire(tmp_path: Path, judge: dict | None):
    from types import SimpleNamespace

    from herdr_engineering.cli import _wire_assistant_hub
    section: dict = {"enabled": True, "hub": {"enabled": True, "host": "127.0.0.1", "port": closed_port(),
                                               "allowed_networks": ["127.0.0.0/8"]}}
    if judge is not None:
        section["judge"] = judge
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    svr = SimpleNamespace(assistant=svc, assistant_workers=None, assistant_judge="unset")
    return svr, _wire_assistant_hub(svr)


def test_cli_wiring_starts_recorder_only_when_enabled_and_stops_it(tmp_path):
    svr, hub = _wire(tmp_path, None)                       # judge disabled by default: no thread at all
    try:
        assert hub is not None and svr.assistant_judge.recorder.is_running() is False
    finally:
        hub.shutdown()
    svr, hub = _wire(tmp_path, {"enabled": True, "endpoint": "http://127.0.0.1:9", "api_key_file": str(write_key(tmp_path))})
    try:
        assert svr.assistant_judge.recorder.is_running() is True
    finally:
        hub.shutdown()
        svr.assistant_judge.recorder.stop()
    assert svr.assistant_judge.recorder.is_running() is False


# ------------------------------------------------------------------------------------------------- integration

@pytest.mark.integration
@pytest.mark.skipif(not (os.environ.get("HERDR_JEV_TEST_URL") and os.environ.get("HERDR_JEV_TEST_KEY_FILE")),
                    reason="needs a real System One mock: set HERDR_JEV_TEST_URL and HERDR_JEV_TEST_KEY_FILE")
def test_integration_register_against_real_system_one(home, tmp_path, keydir):
    url, key = os.environ["HERDR_JEV_TEST_URL"], Path(os.environ["HERDR_JEV_TEST_KEY_FILE"]).read_text().strip()
    path = keydir / "jev.key"
    argv = ["--endpoint", url, "--key-file", str(path), "--key-stdin"]
    model = os.environ.get("HERDR_JEV_TEST_MODEL")
    if model:
        argv += ["--model", model]
    done = run_register(home, tmp_path, argv, key)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"registered: fingerprint={fp(key)} model=")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    no_leak(done, key)
