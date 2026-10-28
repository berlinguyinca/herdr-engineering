"""Route-level rules: default off, key-mapped viewers, no header trust, review/pause controls."""
from datetime import UTC, datetime

from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
KEY = {"kind": "operator", "id": "key:abc", "authenticated_by": "api_key"}
OPEN = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
FULL = {"enabled": True, "ingest": True, "derive": True, "suggest": True,
        "viewers": {"key:abc": {"org_id": "org1", "projects": ["p1"]}}}


def routes(section):
    r = IntelligenceRoutes(pi.IntelligenceStore(clock=lambda: T0), section_fn=lambda: section)
    return r


def seed(r):
    cfg = pi.config(FULL)
    for pid, label in (("p1", "internal"), ("p2", "internal")):
        r.store.append_event({"org_id": "org1", "source_system": "gh", "source_instance": "i", "source_id": pid,
                              "event_type": "pr", "occurred_at": T0.isoformat(), "object_ref": f"pr:{pid}",
                              "project_ids": [pid], "title": f"t-{pid}", "security_label": label}, cfg=cfg)


def test_disabled_by_default_is_404_everywhere():
    r = routes({})
    assert r.get(["status"], {}, KEY, "operator")[0] == 404
    assert r.post(["sources", "gh", "pause"], {}, KEY, "operator")[0] == 404


def test_viewer_scope_comes_from_key_mapping_only():
    r = routes(FULL); seed(r)
    st, body = r.get(["projects", "p1", "timeline"], {}, KEY, "operator")
    assert st == 200 and body["total"] == 1
    assert r.get(["projects", "p2", "timeline"], {}, KEY, "operator") == (200, {"items": [], "total": 0})
    other = {"kind": "operator", "id": "key:zzz", "authenticated_by": "api_key"}
    assert r.get(["projects", "p1", "timeline"], {}, other, "operator")[0] == 404  # unmapped key sees nothing
    assert r.get(["projects", "p1", "timeline"], {}, KEY, "agent")[0] == 404       # write-only roles cannot read
    assert r.get(["projects", "p1", "timeline"], {}, KEY, None)[0] == 404


def test_open_loopback_operator_needs_a_configured_org():
    sec = dict(FULL, viewers={})
    r = routes(sec); seed(r)
    assert r.get(["status"], {}, OPEN, "operator")[0] == 404
    r2 = routes(dict(sec, default_org_id="org1")); seed(r2)
    assert r2.get(["projects", "p2", "timeline"], {}, OPEN, "operator")[1]["total"] == 1


def test_bad_inputs_are_400_not_500():
    r = routes(FULL)
    assert r.get(["projects", "p1", "timeline"], {"as_of": "yesterday"}, KEY, "operator")[0] == 400
    assert r.get(["projects", "p1", "timeline"], {"limit": "x"}, KEY, "operator")[0] == 400
    assert r.get(["relationships"], {}, KEY, "operator")[0] == 400


def test_review_and_pause_controls():
    r = routes(FULL)
    rel = r.store.add_relationship({"org_id": "org1", "project_ids": ["p1"], "from_ref": "a", "to_ref": "b",
        "predicate": "mentions", "derivation": "inferred", "confidence": 0.5, "extractor_version": "v1",
        "evidence_refs": [{"source_ref": "s", "anchor_type": "object", "anchor_value": "1"}]},
        cfg=pi.config(FULL), actor="x")
    body = {"accept": True, "expected_version": 1}
    assert r.post(["relationships", rel.id, "review"], body, KEY, "viewer")[0] == 404  # operator only
    assert r.post(["relationships", rel.id, "review"], {"accept": True}, KEY, "operator")[0] == 400
    assert r.post(["relationships", rel.id, "review"], dict(body, expected_version=7), KEY, "operator")[0] == 409
    assert r.post(["relationships", rel.id, "review"], body, KEY, "operator")[1]["review_state"] == "confirmed"
    assert routes(dict(FULL, suggest=False)).post(["relationships", "x", "review"], body, KEY, "operator")[0] == 409
    assert r.post(["sources", "gh", "pause"], {}, KEY, "operator")[0] == 404          # project-scoped: pausing is org-wide
    admin = {"kind": "operator", "id": "key:adm", "authenticated_by": "api_key"}
    r._section = lambda: dict(FULL, viewers=dict(FULL["viewers"], **{"key:adm": {"org_id": "org1", "projects": "*"}}))
    assert r.post(["sources", "gh", "pause"], {}, admin, "operator")[1] == {"source": "gh", "paused": True}
    try:
        r.store.append_event({"org_id": "org1", "source_system": "gh", "source_instance": "i", "source_id": "n",
                              "event_type": "pr", "occurred_at": T0.isoformat(), "object_ref": "pr:n"}, cfg=pi.config(FULL))
        raise AssertionError("paused source accepted an event")
    except pi.IngestionPaused:
        pass


def test_hot_kill_switch_takes_effect_on_the_next_request():
    sec = dict(FULL)
    r = IntelligenceRoutes(pi.IntelligenceStore(clock=lambda: T0), section_fn=lambda: sec)
    assert r.get(["status"], {}, KEY, "operator")[0] == 200
    sec["enabled"] = False
    assert r.get(["status"], {}, KEY, "operator")[0] == 404


def test_malformed_viewer_scope_sees_nothing_and_store_failure_is_503():
    bad = dict(FULL, viewers={"key:abc": {"org_id": "org1", "projects": "p1"}})   # a bare string is not a list
    assert routes(bad).get(["status"], {}, KEY, "operator")[0] == 404

    class Down(pi.IntelligenceStore):
        def records(self, *a, **k):
            raise pi.StoreUnavailable("db")
    r = IntelligenceRoutes(Down(), section_fn=lambda: FULL)
    assert r.get(["projects"], {}, KEY, "operator")[0] == 503


def test_store_is_built_lazily_and_never_when_disabled():
    built = []
    r = IntelligenceRoutes(store_fn=lambda: built.append(1) or pi.IntelligenceStore(), section_fn=lambda: {})
    assert r.get(["status"], {}, KEY, "operator")[0] == 404 and built == []
    r2 = IntelligenceRoutes(store_fn=lambda: built.append(1) or pi.IntelligenceStore(), section_fn=lambda: FULL)
    assert r2.get(["projects"], {}, KEY, "operator")[0] == 200 and built == [1]


def test_status_metrics_are_operator_only_counters():
    r = routes(FULL)
    r.metrics_fn = lambda: {"bridge": {"accepted": 3}, "queue_depth": 0, "incidents": {"FAILED": 1}}
    viewer_key = dict(KEY)
    st, body = r.get(["status"], {}, viewer_key, "operator")
    assert st == 200 and body["metrics"]["bridge"]["accepted"] == 3
    assert "metrics" not in r.get(["status"], {}, viewer_key, "viewer")[1]


def test_reconcile_route_is_operator_org_wide_and_needs_the_github_client():
    from types import SimpleNamespace as NS
    sec = dict(FULL, default_org_id="org1", webhook_projects={"o/r": ["p1"]},
               viewers={"key:abc": {"org_id": "org1", "projects": "*"}})
    r = routes(sec)
    assert r.post(["webhooks", "reconcile"], {}, KEY, "operator")[0] == 409          # no GitHub client
    r.github_app_fn = lambda: NS(open_prs=lambda repo: [NS(number=1, head_sha="c" * 40, author_login="u", base_ref="main")])
    st, body = r.post(["webhooks", "reconcile"], {}, KEY, "operator")
    assert st == 200 and body["recorded"] == 1
    assert r.post(["webhooks", "reconcile"], {}, KEY, "viewer")[0] == 404
    scoped = routes(dict(sec, viewers={"key:abc": {"org_id": "org1", "projects": ["p1"]}}))
    scoped.github_app_fn = r.github_app_fn
    assert scoped.post(["webhooks", "reconcile"], {}, KEY, "operator")[0] == 404
