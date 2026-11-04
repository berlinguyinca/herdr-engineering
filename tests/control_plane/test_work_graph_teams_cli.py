# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`herdr-eng teams ...` against a real control plane and a real Postgres."""
from __future__ import annotations

import json
import threading

import pytest

from herdr_engineering import cli

from .test_collab_tasks import svc  # noqa: F401
from .test_work_graph_api import call, serve, world  # noqa: F401
from .wg_pg import wg, wg_pg  # noqa: F401


@pytest.fixture
def plane(world, wg, wg_pg, monkeypatch):
    wg.run(wg.pool.execute("DELETE FROM fabric_events"))
    svr = serve(unified_workspace_config={"enabled": True}, github_pg_dsn=wg_pg["dsn"])
    for _ in range(100):
        if svr.work_graph.available():
            break
        threading.Event().wait(0.1)
    assert svr.work_graph.available()
    monkeypatch.setenv("TEAMS_CLI_KEY", "op-key")
    yield world, f"http://127.0.0.1:{svr.port}"
    svr.shutdown()


def run(url, *argv, key_env="TEAMS_CLI_KEY"):
    return cli.main(["teams", *argv, "--control-plane", url, "--api-key-env", key_env])


def test_create_list_rename_archive_through_the_cli(plane, capsys):
    world, url = plane
    assert run(url, "create", "CLI crew", "--room", world.room) == 0
    out = capsys.readouterr().out
    assert "Created" in out and "CLI crew" in out and "#/team-map?root=team:team-" in out
    assert run(url, "create", "CLI crew", "--room", world.room) == 0
    assert "Already exists:" in capsys.readouterr().out

    assert run(url, "list", "--json") == 0
    items = json.loads(capsys.readouterr().out)["items"]
    team = next(i for i in items if i["display_name"] == "CLI crew")

    assert run(url, "rename", team["native_id"], "CLI crew 2") == 0
    assert "Renamed" in capsys.readouterr().out
    assert run(url, "rename", team["native_id"], "CLI crew 2") == 0
    assert "Unchanged:" in capsys.readouterr().out
    assert run(url, "archive", team["native_id"]) == 0
    assert "Archived" in capsys.readouterr().out

    assert run(url, "list") == 0
    assert "CLI crew 2" not in capsys.readouterr().out
    assert run(url, "list", "--status", "archived") == 0
    assert "CLI crew 2" in capsys.readouterr().out


def test_errors_are_one_line_and_never_echo_the_key(plane, capsys, monkeypatch):
    world, url = plane
    assert run(url, "create", "x", "--room", "no-such-room") == 1
    err = capsys.readouterr().err
    assert err.startswith("herdr-eng: not found") and "Traceback" not in err
    assert run(url, "create", "") == 1
    assert "name" in capsys.readouterr().err
    assert run(url, "archive", "team-missing") == 1
    capsys.readouterr()
    # a viewer key is refused with the same plain message and the key is not printed
    monkeypatch.setenv("TEAMS_VIEW_KEY", "view-key")
    assert run(url, "create", "x", key_env="TEAMS_VIEW_KEY") == 1
    out = capsys.readouterr()
    assert "view-key" not in out.out + out.err


def test_the_work_graph_off_is_said_plainly(capsys, monkeypatch):
    svr = serve()
    try:
        monkeypatch.setenv("TEAMS_CLI_KEY", "op-key")
        assert run(f"http://127.0.0.1:{svr.port}", "list") == 1
        assert "Work Graph is off" in capsys.readouterr().err
    finally:
        svr.shutdown()
