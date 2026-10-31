# ruff: noqa: F811  (pytest fixtures are imported by name)
"""Plan 3, Task 9: the worker's collaborator-bridge wiring (``cli._collab_artifacts_settings`` / ``_cmd_cp_worker``).

YAML only: nothing here sets or reads an environment variable. The real PostgreSQL is the module-scoped test server."""
import argparse
import asyncio
import inspect
import json
import time
import urllib.error

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane import worker
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits
from herdr_engineering.control_plane.collab_intents import CollabIntentApplier
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture
from tests.test_central_config_servers import make_file


def section(**collab_artifacts):
    return {"collab_artifacts": collab_artifacts}


# ------------------------------------------------------------------------------------------------ settings

@pytest.mark.parametrize("cp", [{}, section(), section(enabled=False), section(enabled="true"), section(enabled=1),
                                section(enabled=None), {"collab_artifacts": None}])
def test_the_bridge_is_off_unless_enabled_is_exactly_true(cp):
    assert cli._collab_artifacts_settings(cp).enabled is False


def test_enabled_true_builds_limits_from_the_yaml_keys():
    s = cli._collab_artifacts_settings(section(enabled=True, daily_bytes_per_agent=123, apply_concurrency=2))
    assert s.enabled is True
    assert s.limits == BridgeLimits(daily_bytes_per_agent=123, apply_concurrency=2)
    assert (str(s.mirror_dir), str(s.outbox_dir), str(s.spool_dir)) == ("/collab-mirror", "/collab-outbox",
                                                                        "/collab-spool")


def test_directories_are_settings_and_are_not_limits():
    s = cli._collab_artifacts_settings(section(enabled=True, mirror_dir="/m", outbox_dir="/o", spool_dir="/s"))
    assert (str(s.mirror_dir), str(s.outbox_dir), str(s.spool_dir)) == ("/m", "/o", "/s")
    assert s.limits == BridgeLimits()


@pytest.mark.parametrize("bad", [{"daily_bytes_per_agent": 0}, {"daily_bytes_per_agent": -1},
                                 {"commits_per_minute": True}, {"max_file_bytes": "9"}, {"nonsense": 1},
                                 {"spool_dir": 7}])
def test_an_invalid_section_fails_with_a_clear_message(bad):
    with pytest.raises(SystemExit) as err:
        cli._collab_artifacts_settings(section(enabled=True, **bad))
    assert "collab_artifacts" in str(err.value) and next(iter(bad)) in str(err.value)


def test_a_section_that_is_not_a_mapping_fails():
    with pytest.raises(SystemExit, match="mapping"):
        cli._collab_artifacts_settings({"collab_artifacts": ["enabled"]})


def test_a_disabled_section_is_not_validated():
    assert cli._collab_artifacts_settings(section(enabled=False, daily_bytes_per_agent=0)).enabled is False


# ------------------------------------------------------------------------------------------------ the built loop

def build(tmp_path, lineage=None, **cfg):
    s = cli._collab_artifacts_settings(section(
        enabled=True, mirror_dir=str(tmp_path / "m"), outbox_dir=str(tmp_path / "o"), spool_dir=str(tmp_path / "sp"),
        **cfg))
    blobs = FakeBlobs()
    (tmp_path / "o").mkdir(exist_ok=True)

    async def connect():
        raise AssertionError("not connected in this test")
    return s, blobs, cli._build_collab_bridge_loop(s, rustfs=blobs, connect=connect, lineage=lineage)


def test_commit_is_registered_and_the_pieces_share_one_gate_one_limits_one_clock(tmp_path):
    async def go():
        s, blobs, loop = build(tmp_path, daily_bytes_per_agent=123)
        applier, mirror = loop.applier, loop.mirror
        assert "commit" in applier._kinds                                   # else every commit is "unknown intent kind"
        assert applier.mirror is mirror                                     # the post-commit force-refresh
        assert loop.gate is applier.gate                                    # one ArtifactGraphGate
        assert applier.limits is mirror.limits and applier.limits.daily_bytes_per_agent == 123
        assert applier.clock is mirror.clock
        assert applier.rustfs is blobs and mirror.rustfs is blobs
        assert str(applier.spool_dir) == str(tmp_path / "sp")               # a setting, not an environment variable
        assert str(applier.outbox) == str(tmp_path / "o") and str(mirror.root) == str(tmp_path / "m")
        assert dict(applier.extras) == {}                                   # context_extras={}
        mirror.close()
    asyncio.run(go())


def test_lineage_hook_is_passed_through_and_none_when_absent(tmp_path):
    async def go():
        _, _, loop = build(tmp_path)
        assert loop.applier.lineage is None
        loop.mirror.close()

        def hook(version):
            return {}
        _, _, loop = build(tmp_path, lineage=hook)
        assert loop.applier.lineage is hook
        loop.mirror.close()
    asyncio.run(go())


def test_lineage_poster_comes_from_the_artifact_worker_lineage_section(tmp_path):
    key = tmp_path / "key"
    key.write_text("placeholder\n")
    assert cli._collab_lineage_hook({}) is None
    assert cli._collab_lineage_hook({"lineage": None}) is None
    hook = cli._collab_lineage_hook({"lineage": {"api_url": "http://cp.test", "key_file": str(key)}})
    assert callable(hook)
    with pytest.raises(SystemExit, match="lineage"):
        cli._collab_lineage_hook({"lineage": {"api_url": "ftp://x", "key_file": str(key)}})


@pytest.mark.parametrize("exc", [urllib.error.HTTPError("http://x", 502, "bad gateway", {}, None),
                                 urllib.error.URLError("refused"), TimeoutError("slow")])
def test_the_applier_tolerates_the_lineage_posters_network_errors(exc, caplog):
    async def go():
        applier = CollabIntentApplier.__new__(CollabIntentApplier)
        import collections
        calls = []

        def lineage(version):
            calls.append(version["version_id"])
            raise exc
        applier.lineage = lineage
        applier._lineage_due = collections.deque([("ict_1", "a1", {"version_id": "v1"}),
                                                  ("ict_2", "a1", {"version_id": "v2"})])
        await applier._run_lineage()
        assert calls == ["v1", "v2"]                       # the second is still tried after the first failed
    caplog.set_level("WARNING")
    asyncio.run(go())
    assert type(exc).__name__ in caplog.text


# ------------------------------------------------------------------------------------------------ end to end config

def test_yaml_limits_reach_the_applier_and_grants_json(registry_pg, tmp_path):
    async def go():
        import asyncpg
        s = cli._collab_artifacts_settings(section(
            enabled=True, daily_bytes_per_agent=123, mirror_dir=str(tmp_path / "m"), outbox_dir=str(tmp_path / "o"),
            spool_dir=str(tmp_path / "sp")))
        (tmp_path / "o").mkdir()

        async def connect():
            return await asyncpg.connect(registry_pg["dsn"])
        loop = cli._build_collab_bridge_loop(s, rustfs=FakeBlobs(), connect=connect, lineage=None)
        loop.interval, loop.reconnect_delay = 0.05, 0.05
        assert loop.applier.limits.daily_bytes_per_agent == 123
        stop = asyncio.Event()
        task = asyncio.create_task(loop.run(stop))
        try:
            deadline = time.monotonic() + 20
            while not (tmp_path / "m" / "grants.json").exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            doc = json.loads((tmp_path / "m" / "grants.json").read_text())
            assert doc["limits"]["daily_bytes_per_agent"] == 123
        finally:
            stop.set()
            await asyncio.wait_for(task, 20)
    asyncio.run(go())


# ------------------------------------------------------------------------------------------------ _cmd_cp_worker

class SpyLoop:
    """Stands in for the loop only to count starts; everything it would touch is real."""
    started = 0

    def __init__(self, *a, **k):
        self.mirror = k.get("mirror") or (a[1] if len(a) > 1 else None)
        self.applier = a[2] if len(a) > 2 else None

    async def run(self, stop):
        type(self).started += 1
        await stop.wait()


def worker_args(path, **kw):
    return argparse.Namespace(interval=0.05, once=False, migrations=None, fleet_config=str(path), host_id="h", **kw)


def run_worker_cmd(monkeypatch, registry_pg, tmp_path, *, cp_extra):
    SpyLoop.started = 0
    seen = {"passes": 0}
    path = make_file(tmp_path, pg_dsn=registry_pg["dsn"], **cp_extra)

    async def fake_run_worker(conn, *, stop=None, **kw):       # the long-running worker pass, ended after 0.2 s
        seen["passes"] += 1
        await asyncio.sleep(0.2)
        seen["started_beside"] = SpyLoop.started                # the loop is already running next to the worker
    monkeypatch.setattr(worker, "CollabBridgeLoop", SpyLoop)
    monkeypatch.setattr(worker, "run_worker", fake_run_worker)
    monkeypatch.setattr(cli, "_maybe_rustfs_client", lambda cp=None: FakeBlobs())
    return cli._cmd_cp_worker(worker_args(path)), seen


def _once(path):
    a = worker_args(path)
    a.once = True
    return a


def test_worker_starts_the_loop_beside_run_worker_when_enabled(monkeypatch, registry_pg, tmp_path):
    rc, seen = run_worker_cmd(monkeypatch, registry_pg, tmp_path,
                              cp_extra={"collab_artifacts": {"enabled": True, "mirror_dir": str(tmp_path / "m"),
                                                              "outbox_dir": str(tmp_path / "o"),
                                                              "spool_dir": str(tmp_path / "s")}})
    assert rc == 0 and seen["passes"] == 1 and seen["started_beside"] == 1 and SpyLoop.started == 1


@pytest.mark.parametrize("cp_extra", [{}, {"collab_artifacts": {"enabled": False}},
                                      {"collab_artifacts": {"enabled": "true"}}])
def test_worker_starts_no_loop_when_disabled_or_absent(monkeypatch, registry_pg, tmp_path, cp_extra):
    rc, seen = run_worker_cmd(monkeypatch, registry_pg, tmp_path, cp_extra=cp_extra)
    assert rc == 0 and seen["passes"] == 1 and seen["started_beside"] == 0 and SpyLoop.started == 0


def test_once_never_starts_the_loop(monkeypatch, registry_pg, tmp_path, capsys):
    SpyLoop.started = 0
    path = make_file(tmp_path, pg_dsn=registry_pg["dsn"], collab_artifacts={"enabled": True})
    monkeypatch.setattr(worker, "CollabBridgeLoop", SpyLoop)
    monkeypatch.setattr(cli, "_maybe_rustfs_client", lambda cp=None: FakeBlobs())
    assert cli._cmd_cp_worker(_once(path)) == 0
    assert SpyLoop.started == 0 and json.loads(capsys.readouterr().out)["ok"] is True


def test_an_invalid_limit_fails_the_worker_start_before_any_connection(monkeypatch, registry_pg, tmp_path):
    path = make_file(tmp_path, pg_dsn=registry_pg["dsn"],
                     collab_artifacts={"enabled": True, "daily_bytes_per_agent": 0})

    async def no_connect(dsn):
        raise AssertionError("connected before the section was validated")
    monkeypatch.setattr(cli, "_cp_connect", no_connect)
    with pytest.raises(SystemExit, match="daily_bytes_per_agent"):
        cli._cmd_cp_worker(worker_args(path))
    with pytest.raises(SystemExit, match="daily_bytes_per_agent"):         # also in --once: the start is refused
        cli._cmd_cp_worker(_once(path))


def test_enabled_without_rustfs_fails_clearly(monkeypatch, registry_pg, tmp_path):
    path = make_file(tmp_path, pg_dsn=registry_pg["dsn"], collab_artifacts={"enabled": True})
    monkeypatch.setattr(cli, "_maybe_rustfs_client", lambda cp=None: None)
    with pytest.raises(SystemExit, match="RustFS"):
        cli._cmd_cp_worker(worker_args(path))


# ------------------------------------------------------------------------------------------------ no environment

def test_the_new_code_reads_no_environment_variable():
    for fn in (cli._collab_artifacts_settings, cli._build_collab_bridge_loop, cli._collab_lineage_hook,
               cli._wire_collab_artifacts_bridge, cli._worker_control_plane_section, cli._cmd_cp_worker):
        src = inspect.getsource(fn)
        assert "environ" not in src and "getenv" not in src, fn.__name__
