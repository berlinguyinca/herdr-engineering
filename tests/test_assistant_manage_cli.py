"""`herdr-eng assistant create|link|...` through the real parser against a real control plane."""
import json

import pytest

from herdr_engineering import cli
from herdr_engineering.assistant_manage_cli import resolve_ref
from tests.control_plane.test_assistant_dispatch import world  # noqa: F401
from tests.control_plane.test_assistant_followups import KEYS

ROLE = {"id": "procedure:secretary-system", "version": "1.0.0",
        "digest": "sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120"}


@pytest.fixture()
def env(world, monkeypatch, capsys):  # noqa: F811
    h, svc, svr = world
    svc.config["delegation"]["targets"]["pi-secretary"] = {"role_ref": ROLE, "requires": ["os:linux"]}
    monkeypatch.setenv("HERDR_API_KEY", KEYS["alice"])
    url = f"http://127.0.0.1:{svr.port}"

    def run(*argv, expect=0):
        capsys.readouterr()
        code = cli.main(["assistant", *argv, "--control-plane", url])
        out = capsys.readouterr().out
        assert code == expect, out
        return out
    return run, svc, h


def ok(out):
    body = json.loads(out)
    assert body["schema_version"] == 1 and body["ok"] is True
    return body["result"]


def test_types_create_and_replay_are_idempotent(env):
    run, svc, _ = env
    assert {t["type"] for t in ok(run("types"))["types"]} >= {"rakazo", "pi-secretary"}
    first = ok(run("create", "Fleet PA", "--type", "rakazo"))["assistant"]
    again = ok(run("create", "Fleet PA", "--type", "rakazo"))["assistant"]
    assert first["assistant_id"] == again["assistant_id"] and len(svc.store.assistants) == 1
    assert first["type"] == "rakazo" and first["parent_id"] is None


def test_create_under_a_parent_by_name_then_list_inspect_link_unlink_archive(env):
    run, svc, _ = env
    parent = ok(run("create", "Fleet PA", "--type", "rakazo"))["assistant"]
    child = ok(run("create", "Secretary", "--type", "pi-secretary", "--parent", "Fleet PA"))["assistant"]
    assert child["parent_id"] == parent["assistant_id"] and child["status"] == "needs_setup"
    under = ok(run("list", "--parent", "Fleet PA"))["assistants"]
    assert [a["assistant_id"] for a in under] == [child["assistant_id"]]
    assert [a["assistant_id"] for a in ok(run("list", "--type", "pi-secretary"))["assistants"]] == [child["assistant_id"]]
    assert ok(run("inspect", "Secretary"))["assistant"]["assistant_id"] == child["assistant_id"]
    mail = ok(run("create", "Mail assistant", "--type", "rakazo"))["assistant"]
    linked = ok(run("link", "Mail assistant", "--parent", "Fleet PA"))["assistant"]
    assert linked["parent_id"] == parent["assistant_id"]
    assert ok(run("unlink", "Mail assistant"))["assistant"]["parent_id"] is None
    assert ok(run("archive", mail["assistant_id"]))["archived"] == mail["assistant_id"]
    assert svc.store.assistants[mail["assistant_id"]].enabled is False


def test_ambiguous_names_unknown_refs_and_bad_rules_fail_with_an_envelope(env):
    run, svc, _ = env
    a = ok(run("create", "Twin", "--type", "rakazo", "--idempotency-key", "one"))["assistant"]
    b = ok(run("create", "Twin", "--type", "rakazo", "--idempotency-key", "two"))["assistant"]
    err = json.loads(run("inspect", "Twin", expect=1))
    assert err["ok"] is False and a["assistant_id"] in err["error"]["message"] and b["assistant_id"] in err["error"]["message"]
    assert json.loads(run("inspect", "nobody", expect=1))["error"]["code"] == "invalid_request"
    assert json.loads(run("create", "X", "--type", "nope", expect=1))["error"]["status"] == 400
    spec = ok(run("create", "S", "--type", "pi-secretary"))["assistant"]
    assert json.loads(run("link", a["assistant_id"], "--parent", spec["assistant_id"], expect=1))["ok"] is False


def test_link_and_create_never_create_grants_and_the_cli_has_no_identity_flags(env, capsys):
    run, svc, _ = env
    ok(run("create", "Fleet PA", "--type", "rakazo"))
    ok(run("create", "Secretary", "--type", "pi-secretary", "--parent", "Fleet PA"))
    assert not svc.store.grants and not svc.store.approvals
    for flag in ("--user", "--tenant", "--approved"):
        assert cli.main(["assistant", "list", flag, "x"]) != 0
        refused = json.loads(capsys.readouterr().out)
        assert refused["ok"] is False and refused["error"]["code"] == "argument_error"


def test_graph_json_matches_the_route_and_tree_is_readable_text(env):
    run, svc, h = env
    ok(run("create", "Fleet PA", "--type", "rakazo"))
    ok(run("create", "Secretary", "--type", "pi-secretary", "--parent", "Fleet PA"))
    graph = ok(run("graph"))
    assert graph == h.call("alice", "GET", "/api/v1/assistant-graph")[1]
    text = run("graph", "--tree")
    lines = text.splitlines()
    assert lines[0] == "You" and any("Fleet PA" in x for x in lines)
    fleet = next(i for i, x in enumerate(lines) if "Fleet PA" in x)
    assert "Secretary" in lines[fleet + 1] and lines[fleet + 1].startswith("    -")    # nested under its parent
    assert "Specialists" in lines


def test_the_api_key_is_never_printed_and_a_bad_key_is_an_error(env, monkeypatch):
    run, svc, _ = env
    monkeypatch.setenv("HERDR_API_KEY", "not-a-real-key")
    out = run("list", expect=1)
    assert "not-a-real-key" not in out and json.loads(out)["ok"] is False


def test_resolve_ref_prefers_active_assistants_for_a_name():
    rows = [{"assistant_id": "asst_old", "name": "PA", "enabled": False},
            {"assistant_id": "asst_new", "name": "PA", "enabled": True}]
    assert resolve_ref(rows, "PA")["assistant_id"] == "asst_new"
    assert resolve_ref(rows, "asst_old")["assistant_id"] == "asst_old"


def test_setup_verb_is_registered_and_posts_to_the_assistants_setup_route():
    import argparse

    from herdr_engineering import assistant_manage_cli as m
    root = argparse.ArgumentParser()
    m.register_management(root.add_subparsers(dest="verb"))
    args = root.parse_args(["setup", "Secretary"])
    assert args.assistant_manage == "setup" and args.ref == "Secretary" and args.func is m.run_command
