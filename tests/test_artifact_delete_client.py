"""`herdr-eng artifacts delete|restore` against the real control-plane server and a real PostgreSQL registry."""
import json

import pytest

from herdr_engineering.cli import main
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.test_artifact_delete_api import api

registry_pg = _registry_pg_module.registry_pg      # the module-scoped PostgreSQL fixture


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("TEST_OWNER_KEY", "owner-key")
    monkeypatch.setenv("TEST_MEMBER_KEY", "member-key")


def cli(env, *argv, key="TEST_OWNER_KEY"):
    return main(["artifacts", *argv, "--control-plane", env.base, "--api-key-env", key])


def lines(capsys):
    out = capsys.readouterr()
    return [json.loads(line) for line in out.out.splitlines()], out.err


def test_delete_then_restore_prints_one_json_object_per_id(registry_pg, capsys):
    with api(registry_pg) as env:
        a, ka = env.seed()
        assert cli(env, "delete", a, "art-missing", "--reason", "tidy", "--hold-days", "5") == 0
        rows, _ = lines(capsys)
        assert [(r["artifact_id"], r["result"]) for r in rows] == [(a, "tombstoned"), ("art-missing", "not_found")]
        assert env.blobs.head(ka)
        assert cli(env, "restore", a) == 0
        assert [r["result"] for r in lines(capsys)[0]] == ["restored"]


def test_purge_now_needs_yes_and_never_reaches_the_server_without_it(registry_pg, capsys):
    with api(registry_pg) as env:
        a, ka = env.seed()
        assert cli(env, "delete", a, "--purge-now") == 2
        assert "--yes" in lines(capsys)[1] and env.lifecycle(a) == [] and env.blobs.head(ka)
        assert cli(env, "delete", a, "--yes") == 0                    # --yes alone only tombstones
        assert [r["result"] for r in lines(capsys)[0]] == ["tombstoned"] and env.blobs.head(ka)
        assert cli(env, "delete", a, "--purge-now", "--yes", "--reason", "gone") == 0
        row = lines(capsys)[0][0]
        assert row["result"] == "deleted" and row["objects_deleted"] == 1 and not env.blobs.head(ka)


def test_flag_combinations_and_ids_are_checked_before_any_request(registry_pg, capsys):
    with api(registry_pg) as env:
        a, _ = env.seed()
        assert cli(env, "delete", a, "--hold-days", "0") == 2
        assert cli(env, "delete", a, "--purge-now", "--yes", "--hold-days", "3") == 2
        assert cli(env, "delete", "bad/id") == 2
        assert env.lifecycle(a) == []


def test_a_non_owner_key_gets_a_plain_error_and_nothing_changes(registry_pg, capsys):
    with api(registry_pg) as env:
        a, ka = env.seed()
        assert cli(env, "delete", a, key="TEST_MEMBER_KEY") == 1
        out = capsys.readouterr()
        assert out.out == "" and "HTTP 404" in out.err and "member-key" not in out.err
        assert env.lifecycle(a) == []


def test_more_than_one_batch_is_sent_for_many_ids(registry_pg, capsys):
    with api(registry_pg) as env:
        ids = [f"art-none-{n}" for n in range(250)]
        assert cli(env, "delete", *ids) == 0
        rows, _ = lines(capsys)
        assert len(rows) == 250 and {r["result"] for r in rows} == {"not_found"}


def test_an_unreachable_server_is_one_error_without_the_key(registry_pg, capsys, monkeypatch):
    monkeypatch.setenv("TEST_OWNER_KEY", "super-secret-owner-key")
    assert main(["artifacts", "delete", "a1", "--control-plane", "http://127.0.0.1:1",
                 "--api-key-env", "TEST_OWNER_KEY"]) == 1
    assert "super-secret-owner-key" not in capsys.readouterr().err


def test_the_override_flag_reaches_restore_and_a_reused_key_is_reported(registry_pg, capsys):
    with api(registry_pg) as env:
        a, ka = env.seed()
        env.adapter("tombstone_artifact", a, actor="o", reason="legal", legal_hold=True)
        assert cli(env, "restore", a) == 0
        row = lines(capsys)[0][0]
        assert row["result"] == "refused" and row["reason"] == "legal_hold"
        assert cli(env, "restore", a, "--override-legal-hold") == 0
        assert lines(capsys)[0][0]["result"] == "restored"
        assert cli(env, "delete", a, "--request-key", "K") == 0
        capsys.readouterr()
        assert cli(env, "delete", a, "--purge-now", "--yes", "--request-key", "K") == 0
        assert lines(capsys)[0][0]["reason"] == "request_key_reused" and env.blobs.head(ka)
