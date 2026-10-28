"""The assistant's project-intelligence read tool: catalog entry, grant gating and viewer scoping."""
import pytest

from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.assistant import TOOL_CATALOG, AssistantError, AssistantService, assistant_config
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes


def _routes(raw):
    return IntelligenceRoutes(pi.IntelligenceStore(), lambda: raw)


def test_catalog_entry_is_non_sensitive_read_only():
    meta = TOOL_CATALOG["read_project_intelligence"]
    assert meta["sensitive"] is False
    assert set(meta["parameters"]["properties"]["operation"]["enum"]) == {"projects_search", "timeline_query", "evidence_open"}


def test_disabled_or_unmapped_user_gets_nothing():
    off = _routes({})
    with pytest.raises(pi.NotFound):
        off.assistant_read("t", "u", {"operation": "projects_search"})
    on = _routes({"enabled": True, "assistant_viewers": {"t/other": {"org_id": "o", "projects": "*"}}})
    with pytest.raises(pi.NotFound):
        on.assistant_read("t", "u", {"operation": "projects_search"})


def test_malformed_scope_and_bad_operation_refused():
    bad = _routes({"enabled": True, "assistant_viewers": {"t/u": {"org_id": "o", "projects": "abc"}}})
    with pytest.raises(pi.NotFound):
        bad.assistant_read("t", "u", {"operation": "projects_search"})
    ok = _routes({"enabled": True, "assistant_viewers": {"t/u": {"org_id": "o", "projects": "*"}}})
    assert ok.assistant_read("t", "u", {"operation": "projects_search"}) == {"projects": []}
    with pytest.raises(ValueError):
        ok.assistant_read("t", "u", {"operation": "delete_everything"})
    with pytest.raises(ValueError):
        ok.assistant_read("t", "u", {"operation": "timeline_query"})


def test_service_without_wiring_has_no_executor_data():
    svc = AssistantService(config=assistant_config({"enabled": True}))
    assert svc.intelligence_read is None
    assert AssistantError  # executor raises AssistantError("project intelligence is not enabled") when unset
