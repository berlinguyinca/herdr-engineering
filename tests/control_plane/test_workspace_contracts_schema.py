"""P01 contracts: JSON Schema fixtures, ids/aliases, ETag/CAS, graph mapping and consumer compatibility.

Every invalid fixture asserts WHICH keyword rejected it (a bare ValidationError would also pass for a typo
in the fixture). Library-only: nothing here touches a live route except the capability flag test."""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")   # declared in the dev extra; CI installs it

from herdr_engineering.control_plane import ids  # noqa: E402
from herdr_engineering.control_plane import workspace_contracts as wc  # noqa: E402
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.assistant import AssistantGrant  # noqa: E402
from herdr_engineering.control_plane.events import Event  # noqa: E402
from herdr_engineering.control_plane.graph import EDGE_TYPES, NODE_TYPES  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "workspace_v1"
ROOT = wc.load_schema()


def _validator(name):
    return jsonschema.Draft202012Validator(wc.schema_for(name, ROOT))


def _errors(name, doc):
    return {e.validator for e in _validator(name).iter_errors(doc)}


def _load(p):
    return json.loads(p.read_text())


VALID = sorted((FIX / "valid").glob("*.json"))
INVALID = sorted((FIX / "invalid").glob("*.json"))


def test_root_schema_is_valid_draft_2020_12_and_covers_every_kind():
    jsonschema.Draft202012Validator.check_schema(ROOT)
    assert ROOT["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(wc.ENTITY_KINDS) | {"edge", "event"} <= set(ROOT["$defs"])
    assert len(wc.ENTITY_KINDS) == 14 and len(wc.EDGE_KINDS) == 14
    assert set(ROOT["$defs"]["edge"]["properties"]["type"]["enum"]) == set(wc.EDGE_KINDS)


def test_fixture_corpus_is_complete():
    names = {p.stem for p in VALID}
    assert set(wc.ENTITY_KINDS) <= names and {f"edge_{t}" for t in wc.EDGE_KINDS} <= names and "event" in names
    assert len(INVALID) >= 12


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture_passes_schema_and_semantics(path):
    doc = _load(path)
    kind = "event" if path.stem == "event" else "edge" if path.stem.startswith("edge_") else path.stem
    _validator(kind).validate(doc)
    {"event": wc.validate_event, "edge": wc.validate_edge}.get(kind, wc.validate_entity)(doc)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_fails_on_the_expected_keyword(path):
    fx = _load(path)
    assert fx["expect_keyword"] in _errors(fx["schema"], fx["doc"])


@pytest.mark.parametrize("case", _load(FIX / "semantic_invalid.json"), ids=lambda c: c["why"])
def test_semantic_rejects(case):
    with pytest.raises(wc.ContractError):
        getattr(wc, case["fn"])(case["doc"])


def test_valid_fixture_is_not_vacuous():
    """Control for the reject tests: a known-good document passes the SAME validator that rejects its mutations."""
    doc = _load(FIX / "valid" / "project.json")
    assert _errors("project", doc) == set()
    assert "required" in _errors("project", {k: v for k, v in doc.items() if k != "scope"})


# ---- ids and aliases -------------------------------------------------------------------------
def test_urn_is_deterministic_per_native_id_and_ignores_display_names():
    a = wc.urn_for("room", "room_abc")
    assert a == wc.urn_for("room", "room_abc") != wc.urn_for("room", "room_abd") != wc.urn_for("team", "room_abc")
    assert wc.parse_urn(a)[0] == "room"
    assert wc.parse_urn(wc.new_urn("task"))[0] == "task"


@pytest.mark.parametrize("bad", ["", "urn:herdr:room:NOT-A-UUID", "urn:herdr:bogus:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12,
                                 " " + wc.urn_for("room", "x"), wc.urn_for("room", "x").upper(), None, 5])
def test_parse_urn_rejects_malformed(bad):
    with pytest.raises(wc.ContractError):
        wc.parse_urn(bad)


def test_real_native_ids_round_trip_through_urns_and_schema():
    rid, aid = ids.new_id("mission"), ids.new_id("artifact")
    for kind, native in (("mission", rid), ("artifact", aid)):
        u = wc.urn_for(kind, native)
        assert wc.parse_urn(u)[0] == kind
        assert _errors("urn", u) == set()


def test_alias_table_binds_once_and_resolves():
    t, u = wc.AliasTable(), wc.urn_for("issue", "gh:o/r#4")
    t.bind("t1", u, "github", "o/r#4")
    t.bind("t1", u, "github", "o/r#4")                                  # idempotent
    assert t.canonicalize("t1", "github:o/r#4") == u and t.canonicalize("t1", u) == u
    with pytest.raises(wc.AliasConflict):
        t.bind("t1", wc.urn_for("issue", "other"), "github", "o/r#4")
    t.bind("t2", wc.urn_for("issue", "other"), "github", "o/r#4")       # another tenant: independent
    with pytest.raises(wc.ContractError):
        t.canonicalize("t1", "github:unknown")
    with pytest.raises(wc.ContractError):
        t.canonicalize("t1", "urn:herdr:room:bad")


# ---- ETag / CAS ------------------------------------------------------------------------------
def _project():
    return _load(FIX / "valid" / "project.json")


def test_cas_update_bumps_version_and_requires_current_etag():
    cur = _project()
    new = wc.apply_update(cur, {"name": "renamed"}, if_match=cur["etag"], now=datetime(2026, 10, 29, tzinfo=UTC))
    assert new["version"] == 2 and new["name"] == "renamed" and new["etag"] != cur["etag"]
    assert new["id"] == cur["id"] and new["created_at"] == cur["created_at"]
    wc.validate_entity(new)
    with pytest.raises(wc.PreconditionFailed):
        wc.apply_update(new, {"name": "x"}, if_match=cur["etag"])      # stale writer loses
    with pytest.raises(wc.PreconditionRequired):
        wc.apply_update(new, {"name": "x"}, if_match=None)
    for bad in ({"id": "x"}, {"version": 9}, {"etag": "x"}):
        with pytest.raises(wc.ContractError):
            wc.apply_update(new, bad, if_match=new["etag"])
    with pytest.raises(wc.ContractError):
        wc.apply_update(new, {"scope": {**new["scope"], "tenant_id": "other"}}, if_match=new["etag"])


def test_artifact_version_and_tombstoned_docs_are_immutable():
    av = _load(FIX / "valid" / "artifact_version.json")
    with pytest.raises(wc.ImmutableEntity):
        wc.apply_update(av, {"display_name": "x"}, if_match=av["etag"])
    dead = {**_project(), "tombstone": {"deleted_at": "2026-10-28T12:00:00Z", "reason": "policy"}}
    with pytest.raises(wc.ImmutableEntity):
        wc.apply_update(dead, {"name": "x"}, if_match=dead.get("etag"))


# ---- graph mapping (reuse, additive only) ----------------------------------------------------
def test_mapping_is_truthful_about_what_graph_py_has_today():
    for kind, (g, status) in wc.GRAPH_NODE_MAP.items():
        assert (g in NODE_TYPES) == (status == "exists"), kind
    for edge, (g, status) in wc.GRAPH_EDGE_MAP.items():
        assert (g in EDGE_TYPES) == (status == "exists"), edge
    assert set(wc.GRAPH_NODE_MAP) == set(wc.ENTITY_KINDS) and set(wc.GRAPH_EDGE_MAP) == set(wc.EDGE_KINDS)
    assert wc.ADDED_NODE_TYPES == {"project", "team", "artifact_version", "external_action"}
    assert wc.ADDED_EDGE_TYPES == {"contains", "derived_from", "discussed_in", "approved_by", "notified_via"}


def test_live_graph_vocabulary_is_unchanged_by_p01():
    assert not (wc.ADDED_NODE_TYPES & set(NODE_TYPES)) and not (wc.ADDED_EDGE_TYPES & set(EDGE_TYPES))
    assert set(NODE_TYPES) <= wc.EXTENDED_NODE_TYPES and set(EDGE_TYPES) <= wc.EXTENDED_EDGE_TYPES
    # aliases land on existing edge kinds, never a twin of them
    assert wc.GRAPH_EDGE_MAP["depends_on"][0] == "dependency" and "depends_on" not in EDGE_TYPES


def test_edge_endpoint_matrix_covers_every_edge_and_fixtures_obey_it():
    assert set(wc.EDGE_ENDPOINTS) == set(wc.EDGE_KINDS)
    for p in VALID:
        if p.stem.startswith("edge_"):
            wc.validate_edge(_load(p))


# ---- consumer compatibility ------------------------------------------------------------------
def test_existing_artifact_report_schema_still_loads_and_is_untouched():
    jsonschema.Draft202012Validator.check_schema(json.loads((wc.SCHEMA_PATH.parent / "artifact-report-v1.schema.json").read_text()))


def test_events_envelope_v2_projects_onto_the_event_contract():
    ev = Event(event_id="ev_1", schema_version=2, entity_type="task", entity_id="tsk_1", event_type="task.updated",
               source="hcn", source_sequence=5, actor={"kind": "user", "id": "alice"}, visibility_scope="room",
               room_id="room_1", correlation_id="co-1", causation_id="ev_0")
    doc = wc.event_from_envelope(ev, tenant_id="t1", entity_urns=[wc.urn_for("task", "tsk_1")])
    _validator("event").validate(doc)
    wc.validate_event(doc)
    assert "trace_id" not in doc                       # events.py has no trace_id column: documented P02 gap
    v1 = Event(event_id="ev_2", entity_id="e", event_type="x")      # a v1 event (no v2 fields) still projects
    _validator("event").validate(wc.event_from_envelope(v1, tenant_id="t1"))


def test_assistant_grant_adapts_to_account_grant():
    g = AssistantGrant("g1", "t1", "alice", "ast", "send_message", "gmail:me", {"preauthorized": True},
                       expires_at=datetime(2030, 1, 1, tzinfo=UTC))
    a = wc.account_grant_from_assistant(g)
    assert (a.user_id, a.tool_id, a.scope, a.preauthorized) == ("alice", "send_message", "gmail:me", True)
    assert a.active(datetime(2026, 1, 1, tzinfo=UTC))


# ---- capability flag -------------------------------------------------------------------------
def test_flag_is_yaml_only_strict_and_default_off():
    assert wc.FLAG == "unified_workspace_v1"
    assert not wc.enabled(None) and not wc.enabled({}) and not wc.enabled({"unified_workspace": {}})
    assert not wc.enabled({"unified_workspace": {"enabled": "true"}}) and not wc.enabled({"unified_workspace": True})
    assert wc.enabled({"unified_workspace": {"enabled": True}})


def _health(**kw):
    svr = ControlPlaneServer(ControlPlaneRepo(), **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}/api/v1/health") as r:
            return json.loads(r.read())
    finally:
        svr.shutdown()


def test_capability_absent_by_default_and_present_when_enabled():
    off = _health()
    assert "unified_workspace_v1" not in off["capabilities"]
    assert "unified_workspace_v1" not in _health(unified_workspace_config={"enabled": False})["capabilities"]
    on = _health(unified_workspace_config={"enabled": True})
    assert "unified_workspace_v1" in on["capabilities"] and "ci_v1" in on["capabilities"]   # additive: nothing removed
