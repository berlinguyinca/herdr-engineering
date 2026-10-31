"""Automatic placement: capabilities and resources decide, Slurm participates only when configured, nothing grants."""
import json

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane import placement as pl
from herdr_engineering.control_plane import slurm_placement as sp
from tests.control_plane.test_assistant_dispatch import world  # noqa: F401
from tests.control_plane.test_assistant_followups import KEYS
from tests.control_plane.test_assistant_runtime import attest, attestation, env, operator, package  # noqa: F401
from tests.control_plane.test_collab import BASE, make_room

PROFILE = "task-restricted-v1"
ROLE = {"id": "procedure:secretary-system", "version": "1.0.0",
        "digest": "sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120"}
ROLE_KEY = f"{ROLE['id']}@{ROLE['version']}:{ROLE['digest']}"


def cand(cid, kind="host", **over):
    base = {"id": cid, "kind": kind, "backend": kind, "label": cid.split(":", 1)[1], "status": "ready", "capabilities": ["os:linux"],
            "free": {"cpus": 8, "memory_mb": 16384, "gpus": 0}, "attested_at": 100.0,
            "profiles": [PROFILE], "role_bindings": [ROLE_KEY], "queue_wait_s": 0}
    return {**base, **over}


# ------------------------------------------------------------------------------ pure resolver
def test_capability_vocabulary_is_closed_and_desktop_implies_display():
    assert pl.normalize_capabilities(["desktop", "internet"]) == ["desktop", "display", "internet"]
    with pytest.raises(pl.PlacementError):
        pl.normalize_capabilities(["displya"])
    with pytest.raises(pl.PlacementError):
        pl.normalize_capabilities("display")
    with pytest.raises(pl.PlacementError):
        pl.normalize_resources({"disk": 1})
    with pytest.raises(pl.PlacementError):
        pl.normalize_resources({"cpus": True})


def test_a_caller_can_only_add_requirements_never_relax_a_types_minimum():
    assert pl.requirements(["display"], ["internet"]) == ["display", "internet"]
    assert pl.requirements(["display"], []) == ["display"]
    assert pl.requirements(["display"], None) == ["display"]


def test_hosts_that_can_start_now_win_over_a_cluster_then_freshest_then_id():
    out = pl.place([cand("slurm:c:p", "slurm"), cand("host:b", attested_at=100), cand("host:a", attested_at=200)],
                   needs=["os:linux"], resources={"cpus": 1})
    assert out["chosen"] == "host:a"                                 # freshest host; the cluster only after hosts
    assert [c["id"] for c in out["candidates"]] == ["host:a", "host:b", "slurm:c:p"]
    only = pl.place([cand("slurm:c:p", "slurm")], needs=[], resources={})
    assert only["chosen"] == "slurm:c:p"
    tie = pl.place([cand("host:z"), cand("host:m")], needs=[], resources={})
    assert tie["chosen"] == "host:m"                                 # equal everything: id order, deterministic


def test_every_rejection_names_its_reason_and_nothing_is_chosen_when_none_qualify():
    out = pl.place([cand("host:old", status="stale"), cand("host:small", free={"cpus": 1, "memory_mb": 1, "gpus": 0}),
                    cand("host:nocap", capabilities=["os:linux"]), cand("host:noprof", profiles=[]),
                    cand("host:norole", role_bindings=[])],
                   needs=["display"], resources={"cpus": 2}, profile=PROFILE, role=ROLE_KEY)
    assert out["chosen"] is None
    why = {r["id"]: " ".join(r["reasons"]) for r in out["rejected"]}
    assert "not ready" in why["host:old"] and "not enough cpus" in why["host:small"]
    assert "missing display" in why["host:nocap"]
    assert "no sandbox profile" in why["host:noprof"] and "role not bound" in why["host:norole"]


def test_a_host_attesting_internet_changes_no_profile_and_grants_nothing():
    candidate = cand("host:net", capabilities=["os:linux", "internet", "display"])
    before = dict(candidate)
    out = pl.place([candidate], needs=["internet"], resources={})
    assert out["chosen"] == "host:net" and candidate == before       # placement reads, never mutates or widens
    assert pl.place([cand("host:noprof2", capabilities=["internet"], profiles=[])], needs=["internet"], resources={},
                    profile=PROFILE)["chosen"] is None                # a capability cannot stand in for a profile


# ------------------------------------------------------------------------------------- slurm
SINFO = """gpu*|up|idle|2|32|256000|gpu:a100:4(S:0-1)
gpu*|up|mix|3|32|256000|gpu:a100:4(S:0-1)
gpu*|up|alloc|5|32|256000|gpu:a100:4(S:0-1)
low|up|idle|10|64|512000|(null)
dead|down|idle|4|16|64000|(null)
garbage line
"""


def cluster(**over):
    return {"id": "hive", "label": "Hive", "login_host": "login1.example", "assistant_use": True,
            "partitions": {"low": {"capabilities": ["os:linux"], "profiles": [PROFILE], "role_bindings": [ROLE_KEY]},
                           "gpu": {"capabilities": ["os:linux"], "profiles": [PROFILE]}}, **over}


def test_slurm_config_ignores_unconfigured_clusters_and_rejects_bad_input():
    assert sp.slurm_config(None) == []
    assert sp.slurm_config([cluster(assistant_use=False)]) == []     # not configured for assistant use: invisible
    assert sp.slurm_config([cluster()])[0]["partitions"]["low"]["capabilities"] == ["os:linux"]
    for bad in ({"login_host": "-oProxyCommand=x"}, {"login_host": "a b"}, {"id": "Bad Id"}):
        with pytest.raises(ValueError):
            sp.slurm_config([cluster(**bad)])
    with pytest.raises(ValueError):
        sp.slurm_config([cluster(partitions={"p": {"capabilities": ["internett"]}})])
    with pytest.raises(ValueError):
        sp.slurm_config([cluster(), cluster()])                      # duplicate id


def test_sinfo_counts_only_fully_idle_nodes_and_reads_gpus():
    info = sp.parse_sinfo(SINFO)
    assert info["gpu"] == {"up": True, "idle_nodes": 2, "cpus": 32, "memory_mb": 256000, "gpus_per_node": 4}
    assert info["low"]["idle_nodes"] == 10 and info["dead"]["up"] is False
    assert "garbage line" not in info


def test_probe_reports_idle_capacity_caches_and_treats_failure_as_unavailable():
    calls, clock = [], [0.0]
    probe = sp.SlurmProbe(sp.slurm_config([cluster()]), runner=lambda host: calls.append(host) or SINFO,
                          ttl=60, clock=lambda: clock[0])
    rows = {c["id"]: c for c in probe.candidates()}
    low, gpu = rows["slurm:hive:low"], rows["slurm:hive:gpu"]
    assert low["status"] == "ready" and low["free"] == {"cpus": 640, "memory_mb": 5_120_000, "gpus": 0}
    assert gpu["free"] == {"cpus": 64, "memory_mb": 512000, "gpus": 8} and "gpu" in gpu["capabilities"]
    assert low["kind"] == "slurm" and low["profiles"] == [PROFILE]
    probe.candidates()
    assert calls == ["login1.example"]                               # cached inside the ttl
    clock[0] = 61
    probe.candidates()
    assert len(calls) == 2

    def down(_host):
        raise OSError("ssh failed")
    broken = sp.SlurmProbe(sp.slurm_config([cluster()]), runner=down)
    assert {c["status"] for c in broken.candidates()} == {"unavailable"}   # never raises to the caller


def test_a_cluster_without_a_declared_profile_never_carries_a_specialist():
    bare = cluster(partitions={"low": {"capabilities": ["os:linux"]}})
    rows = sp.SlurmProbe(sp.slurm_config([bare]), runner=lambda h: SINFO).candidates()
    out = pl.place(rows, needs=[], resources={}, profile=PROFILE)
    assert out["chosen"] is None and "no sandbox profile" in out["rejected"][0]["reasons"][-1]


# ------------------------------------------------------------------------ host attestation
def test_attestation_carries_capabilities_and_resources_into_candidates(env):  # noqa: F811
    _, room = make_room(env.api)
    record = package(env, room)
    body = attestation(env, record)
    body["capabilities"], body["resources"] = ["display", "internet", "os:linux"], {"cpus": 8, "memory_mb": 16384}
    status, made = operator(env, "POST", ["fleet", "assistant-runtime-hosts"], body)
    assert status == 201 and made["host"]["capabilities"] == ["display", "internet", "os:linux"]
    (c,) = env.svc.assistant_runtime_host_candidates()
    assert c["id"] == "host:local-linux" and c["capabilities"] == ["display", "internet", "os:linux"]
    assert c["free"]["cpus"] == 8 and c["profiles"] == [PROFILE] and "nonce" not in json.dumps(c)
    for bad in ({**body, "capabilities": ["displya"], "nonce": "nonce-aaaaaaaaaaaaaaaa"},
                {**body, "resources": {"disk": 1}, "nonce": "nonce-bbbbbbbbbbbbbbbb"}):
        assert operator(env, "POST", ["fleet", "assistant-runtime-hosts"], bad)[0] == 400


def test_a_runner_that_predates_capabilities_still_attests_and_reports_none(env):  # noqa: F811
    _, room = make_room(env.api)
    attest(env, package(env, room))
    (c,) = env.svc.assistant_runtime_host_candidates()
    assert c["capabilities"] == [] and c["free"] == {}
    assert pl.place([c], needs=["display"], resources={})["chosen"] is None   # unknown is never assumed to match


# ------------------------------------------------------------------- route, graph and CLI
@pytest.fixture()
def hz(world):  # noqa: F811
    h, svc, svr = world
    svc.config["delegation"]["targets"].update({
        "pi-secretary": {"role_ref": ROLE, "requires": ["os:linux"], "resources": {"cpus": 1}},
        "scout": {"role_ref": None, "requires": ["display", "internet"], "resources": {"cpus": 2}}})
    svc.host_candidates = lambda: [
        cand("host:bender", capabilities=["os:linux", "internet", "display"], attested_at=100),
        cand("host:minimac", capabilities=["os:macos", "internet", "display", "desktop"], attested_at=200,
             role_bindings=[])]
    return h, svc, svr


def place(h, kind, require=None):
    query = f"type={kind}" + (f"&require={require}" if require else "")
    return h.call("alice", "GET", f"/api/v1/assistant-placement?{query}")


def test_route_places_by_requirements_and_reports_why_others_were_rejected(hz):
    h, _, _ = hz
    st, out = place(h, "pi-secretary")
    assert st == 200 and out["chosen"] == "host:bender"              # the mac host is not linux and lacks the role
    assert {r["id"]: r["reasons"] for r in out["rejected"]} == {
        "host:minimac": ["missing os:linux", "role not bound"]}
    st, scout = place(h, "scout")
    assert st == 200 and scout["chosen"] == "host:minimac"           # no role needed: both qualify, freshest wins
    assert [c["id"] for c in scout["candidates"]] == ["host:minimac", "host:bender"] and scout["rejected"] == []
    assert scout["needs"] == ["display", "internet"] and scout["resources"] == {"cpus": 2}


def test_extra_requirements_only_tighten_and_unknown_ones_are_refused(hz):
    h, _, _ = hz
    assert place(h, "scout", "vm")[1]["chosen"] is None              # nobody attests vm
    assert place(h, "scout", "desktop")[1]["chosen"] == "host:minimac"
    assert place(h, "scout", "displya")[0] == 400
    assert place(h, "rakazo")[0] == 400 and place(h, "nope")[0] == 400


def test_slurm_is_used_when_configured_and_no_host_can_carry_the_agent(hz):
    h, svc, _ = hz
    svc.host_candidates = lambda: []
    svc.slurm_probe = sp.SlurmProbe(sp.slurm_config([cluster()]), runner=lambda host: SINFO)
    st, out = place(h, "pi-secretary")
    assert st == 200 and out["chosen"] == "slurm:hive:low"           # only `low` binds the role
    assert {r["id"] for r in out["rejected"]} == {"slurm:hive:gpu"}
    assert place(h, "scout")[1]["chosen"] is None                    # a cluster never offers display or internet


def test_placement_is_read_only_and_scoped_to_an_authenticated_caller(hz):
    h, svc, _ = hz
    before = (len(svc.store.audit), len(svc.store.delegations), len(svc.store.grants))
    place(h, "pi-secretary")
    assert (len(svc.store.audit), len(svc.store.delegations), len(svc.store.grants)) == before
    import urllib.error
    import urllib.request
    req = urllib.request.Request(h.base + "/api/v1/assistant-placement?type=scout")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code in (401, 403)


def make(h, name, **body):
    return h.call("alice", "POST", "/api/v1/assistants", {"name": name, **body})[1]


def test_graph_shows_where_a_specialist_would_run_and_why_it_cannot(hz):
    h, svc, _ = hz
    parent = make(h, "Fleet PA")
    sec = make(h, "Secretary", type="pi-secretary", parent_id=parent["assistant_id"])
    scout = make(h, "Scout", type="scout")
    _, g = h.call("alice", "GET", "/api/v1/assistant-graph")
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes[sec["assistant_id"]]["placement"] == {"state": "planned", "host": "host:bender"}
    assert nodes["host:bender"]["kind"] == "host" and "display" in nodes["host:bender"]["capabilities"]
    assert (sec["assistant_id"], "host:bender", "placed_on") in {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    assert "placement" not in nodes[parent["assistant_id"]]          # a rakazo assistant is not placed
    svc.host_candidates = lambda: [cand("host:bender", capabilities=["os:linux"])]
    _, g = h.call("alice", "GET", "/api/v1/assistant-graph")
    state = {n["id"]: n for n in g["nodes"]}[scout["assistant_id"]]["placement"]
    assert state["state"] == "unplaceable" and "missing display" in " ".join(state["reasons"])
    assert state["needs"] == ["display", "internet"]


def test_cli_place_and_the_tree_show_where_agents_run(hz, monkeypatch, capsys):
    h, svc, svr = hz
    monkeypatch.setenv("HERDR_API_KEY", KEYS["alice"])
    url = f"http://127.0.0.1:{svr.port}"
    make(h, "Fleet PA")
    make(h, "Secretary", type="pi-secretary")
    capsys.readouterr()
    assert cli.main(["assistant", "place", "--type", "scout", "--require", "desktop", "--control-plane", url]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] and body["result"]["chosen"] == "host:minimac"
    assert cli.main(["assistant", "place", "--type", "scout", "--require", "bogus", "--control-plane", url]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["status"] == 400
    assert cli.main(["assistant", "graph", "--tree", "--control-plane", url]) == 0
    text = capsys.readouterr().out
    assert "pi-secretary, needs_setup, on bender" in text              # shown by the host's label
    assert "Where agents run" in text and "- bender [host]: ready" in text
