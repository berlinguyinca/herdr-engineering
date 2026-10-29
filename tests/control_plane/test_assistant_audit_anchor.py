"""Anchoring the assistant audit head off the event store, and detecting truncation / rollback / rewrite.

Entry points: ``herdr-eng control-plane assistant-audit anchor|verify`` (cli.main) against a live control plane over HTTP,
reading the anchor path from the fleet YAML. No environment variables."""
import json
import threading

import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering.control_plane import audit_anchor as aa
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_assistant_followups import KEYS, Http, pid


@pytest.fixture()
def world(acknowledged_sink, tmp_path):
    anchor = tmp_path / "anchor" / "audit.anchor"
    keyfile = tmp_path / "alice.key"
    keyfile.write_text(KEYS["alice"] + "\n")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({"version": 1, "hosts": {}, "sections": {"control_plane": {
        "roles": ["control-plane"], "values": {"personal_assistant": {"audit": {"anchor_file": str(anchor)}}}}}}))
    fleet.chmod(0o600)
    section = {"enabled": True, "tenancy": {"principals": {pid("alice"): "acme", pid("bea"): "globex"}}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                             api_keys={k: "operator" for k in KEYS.values()})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    h = Http(svr.port)

    def run(action, capsys, key=keyfile):
        capsys.readouterr()
        rc = cli.main(["control-plane", "assistant-audit", action, "--fleet-config", str(fleet),
                       "--control-plane", f"http://127.0.0.1:{svr.port}", "--api-key-file", str(key)])
        out = capsys.readouterr()
        return rc, (json.loads(out.out) if out.out.strip() else {}), out.err

    def activity(n=1, who="alice"):
        for _ in range(n):
            assert h.call(who, "POST", "/api/v1/assistants", {"name": "A"})[0] == 201
    yield h, svc, run, activity, anchor, tmp_path
    svr.shutdown()


def test_anchor_appends_the_head_and_verify_passes_while_the_chain_only_grows(world, capsys):
    h, svc, run, activity, anchor, _ = world
    activity(2)
    rc, out, _ = run("anchor", capsys)
    head = svc.store.audit_head("acme")
    assert rc == 0 and out["anchored"]["seq"] == head["seq"] and out["anchored"]["sha256"] == head["sha256"]
    assert oct(anchor.stat().st_mode & 0o777) == "0o600"
    assert run("verify", capsys)[0] == 0
    activity(3)                                                              # growth is fine
    rc, out, _ = run("verify", capsys)
    assert rc == 0 and out["status"] == "ok" and out["head"]["seq"] > out["anchored"]["seq"]
    assert run("anchor", capsys)[0] == 0 and run("anchor", capsys)[1]["anchored"].get("unchanged")
    assert len(anchor.read_text().splitlines()) == 2


def test_truncating_the_stored_chain_is_detected_even_though_the_remainder_verifies(world, capsys):
    h, svc, run, activity, anchor, _ = world
    activity(4)
    assert run("anchor", capsys)[0] == 0
    anchored = svc.store.audit_head("acme")["seq"]
    svc.store._audit[:] = [e for e in svc.store._audit if not (e["tenant_id"] == "acme" and e["seq"] > anchored - 2)]
    assert svc.store.verify_audit("acme")                                    # the shortened chain is internally valid
    rc, out, _ = run("verify", capsys)
    assert rc == 1 and out["status"] == "rolled_back" and out["head"]["seq"] == anchored - 2
    rc, _, err = run("anchor", capsys)                                       # and a rollback is never anchored over
    assert rc == 1 and "behind the last anchor" in err and len(anchor.read_text().splitlines()) == 1


def test_a_rewritten_history_that_is_re_hashed_is_reported_as_diverged(world, capsys):
    h, svc, run, activity, anchor, _ = world
    activity(3)
    assert run("anchor", capsys)[0] == 0
    prev = ""
    for e in (x for x in svc.store._audit if x["tenant_id"] == "acme"):      # edit entry 2 and re-sign the whole chain
        if e["seq"] == 2:
            e["detail"] = "rewritten"
        e["prev_sha256"] = prev
        e["sha256"] = svc.store._audit_digest(e)
        prev = e["sha256"]
    assert svc.store.verify_audit("acme")
    rc, out, _ = run("verify", capsys)
    assert rc == 1 and out["status"] == "diverged"


def test_a_broken_chain_and_an_edited_anchor_file_are_both_reported(world, capsys):
    h, svc, run, activity, anchor, _ = world
    activity(2)
    assert run("anchor", capsys)[0] == 0
    activity(1)
    assert run("anchor", capsys)[0] == 0
    rows = [json.loads(x) for x in anchor.read_text().splitlines()]
    rows[0]["seq"] = 1                                                       # lower the first anchor
    anchor.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rc, out, _ = run("verify", capsys)
    assert rc == 1 and out["status"] == "anchor_tampered"
    anchor.unlink()
    rc, out, _ = run("verify", capsys)
    assert rc == 1 and out["status"] == "no_anchor"
    assert run("anchor", capsys)[0] == 0
    next(e for e in svc.store._audit if e["tenant_id"] == "acme" and e["seq"] == 1)["detail"] = "edited"
    assert run("verify", capsys)[1]["status"] == "chain_broken"


def test_each_tenant_anchors_and_verifies_only_its_own_chain(world, capsys):
    h, svc, run, activity, anchor, tmp_path = world
    activity(2)
    activity(1, "bea")
    bea_key = tmp_path / "bea.key"
    bea_key.write_text(KEYS["bea"])
    assert run("anchor", capsys)[0] == 0 and run("anchor", capsys, bea_key)[0] == 0
    tenants = {json.loads(x)["tenant_id"] for x in anchor.read_text().splitlines()}
    assert tenants == {"acme", "globex"}
    svc.store._audit[:] = [e for e in svc.store._audit if e["tenant_id"] != "acme" or e["seq"] < 2]
    assert run("verify", capsys)[1]["status"] == "rolled_back"
    assert run("verify", capsys, bea_key)[1]["status"] == "ok"


def test_the_anchor_path_comes_from_yaml_only_and_a_symlink_is_refused(world, capsys, tmp_path):
    h, svc, run, activity, anchor, _ = world
    activity(1)
    head = {"tenant_id": "acme", "head": svc.store.audit_head("acme")}
    target = tmp_path / "elsewhere"
    target.write_text("")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(aa.AnchorError, match="symlink"):
        aa.anchor_head(link, head)
    with pytest.raises(aa.AnchorError, match="not set"):
        aa.anchor_head("", head)
    with pytest.raises(aa.AnchorError, match="no audit head"):
        aa.anchor_head(anchor, {"tenant_id": "acme", "head": {}})


def test_cli_without_anchor_file_in_yaml_exits_2(world, capsys, tmp_path):
    h, svc, run, *_ = world
    bare = tmp_path / "bare.yaml"
    bare.write_text(yaml.safe_dump({"version": 1, "hosts": {}, "sections": {"control_plane": {"roles": ["control-plane"],
                                                                                "values": {}}}}))
    bare.chmod(0o600)
    rc = cli.main(["control-plane", "assistant-audit", "anchor", "--fleet-config", str(bare),
                   "--control-plane", "http://127.0.0.1:1", "--api-key-file", str(tmp_path / "alice.key")])
    assert rc == 2 and "anchor_file is not set" in capsys.readouterr().err
