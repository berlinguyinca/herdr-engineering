import json
import subprocess
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.service_catalog_query import (
    CatalogSnapshotExpired,
    ServiceCatalogPager,
    service_state,
)

NOW = 1800000000
STAMP = datetime.fromtimestamp(NOW, UTC).isoformat()


def row(id, **values):
    return dict(
        service_id=id,
        service_name=id,
        host_id="host",
        status="healthy",
        updated_at=STAMP,
        catalog={"scope": "herdr", "label": "Application"},
        **values,
    )


def page(pager, rows, **options):
    return pager.page(
        lambda: rows,
        **(
            dict(
                scope="herdr",
                status="current",
                include_archived=False,
                search="",
                host_id=None,
                group_by="project",
            )
            | options
        ),
    )


def test_snapshot_pages_have_exact_scope_counts_facets_and_complete_group_totals():
    rows = [
        row("a"),
        row("b"),
        {**row("c"), "status": "stopped"},
        {**row("d"), "lifecycle": {"archived": True}},
        {**row("local"), "catalog": {"scope": "unassigned"}},
    ]
    pager = ServiceCatalogPager(now=lambda: NOW)
    first = page(pager, rows, limit=1)
    assert [s["service_id"] for s in first["services"]] == ["a"]
    assert first["counts"] == dict(
        scope_total=4,
        current=2,
        unavailable=1,
        unknown=0,
        archived=1,
        history_total=2,
        matching_total=2,
        matching_groups=1,
    )
    assert first["facets"]["hosts"] == [{"value": "host", "count": 4}]
    assert first["groups"][0]["total"] == 2
    rows[1]["service_name"] = "changed"
    second = pager.page(
        lambda: pytest.fail("cursor must not reproject"),
        scope="herdr",
        status="current",
        include_archived=False,
        search="",
        host_id=None,
        group_by="project",
        limit=1,
        cursor=first["page"]["next_cursor"],
    )
    assert second["services"][0]["service_name"] == "b"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["page"]["next_cursor"] is None


def test_unicode_order_search_and_group_identity():
    rows = [row("é"), row("Z"), row("a"), {**row("e\u0301"), "service_name": "e\u0301"}]
    result = page(ServiceCatalogPager(now=lambda: NOW), rows)
    assert [s["service_id"] for s in result["services"]] == ["Z", "a", "e\u0301", "é"]
    assert result["sort"] == "group-name-id-nfc-codepoint-v1"
    service = {
        **row("x"),
        "owner": "Python",
        "port": 8123,
        "catalog": {
            "scope": "herdr",
            "project_id": "spectra",
            "repository_id": "github.com/lab/web",
            "commit_sha": "a" * 40,
            "source_port": 8080,
            "label": "Application",
        },
    }
    assert (
        page(ServiceCatalogPager(now=lambda: NOW), [service], search="PYTHON 8080 spectra")[
            "counts"
        ]["matching_total"]
        == 1
    )


def test_cursor_validation_expiry_eviction_and_budgets():
    clock = [NOW]
    pager = ServiceCatalogPager(now=lambda: clock[0], max_snapshots=1)
    first = page(pager, [row("a"), row("b")], limit=1)
    cursor = first["page"]["next_cursor"]
    with pytest.raises(ValueError):
        page(pager, [], limit=2, cursor=cursor)
    with pytest.raises(ValueError):
        page(pager, [], limit=1, cursor=cursor + "x")
    clock[0] += 30
    with pytest.raises(CatalogSnapshotExpired):
        page(pager, [], limit=1, cursor=cursor)
    clock[0] = NOW
    first = page(pager, [row("a"), row("b")], limit=1)
    page(pager, [row("c")])
    with pytest.raises(CatalogSnapshotExpired):
        page(pager, [], limit=1, cursor=first["page"]["next_cursor"])
    for options, rows in [({"max_rows": 1}, [row("a"), row("b")]), ({"max_bytes": 10}, [row("a")])]:
        with pytest.raises(ValueError, match="snapshot.*(budget|large)"):
            page(ServiceCatalogPager(now=lambda: NOW, **options), rows)


@pytest.mark.parametrize(
    "options",
    [
        {"scope": "bad"},
        {"status": "bad"},
        {"group_by": "bad"},
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"include_archived": "false"},
        {"search": 42},
    ],
)
def test_bad_queries(options):
    defaults = dict(
        scope="herdr",
        status="current",
        include_archived=False,
        search="",
        host_id=None,
        group_by="project",
    )
    defaults.update(options)
    with pytest.raises(ValueError):
        ServiceCatalogPager().page(lambda: [], **defaults)


def test_display_state_matches_actual_javascript_precedence():
    cases = [
        row("healthy"),
        {**row("failed"), "status": "failed"},
        {**row("future"), "updated_at": datetime.fromtimestamp(NOW + 6, UTC).isoformat()},
        {**row("missing"), "metadata_observation": {"state": "missing"}},
        {**row("archived"), "status": "failed", "lifecycle": {"archived": True}},
        {**row("unknown"), "lifecycle": {"state": "unknown"}},
        {**row("stopped"), "status": "stopped", "lifecycle": {"state": "unknown"}},
        {**row("old"), "updated_at": datetime.fromtimestamp(NOW - 901, UTC).isoformat()},
        {
            **row("metadata"),
            "metadata_observation": {
                "state": "observed",
                "observed_at": datetime.fromtimestamp(NOW - 91, UTC).isoformat(),
            },
        },
        {
            **row("system"),
            "catalog": {"scope": "system"},
            "metadata_observation": {"state": "observed", "observed_at": "bad"},
        },
        {**row("invalid"), "updated_at": "bad"},
    ]
    script = (
        "import {serviceState} from './packages/services-view.js'; const rows="
        + json.dumps(cases)
        + "; console.log(JSON.stringify(rows.map(r=>serviceState(r,"
        + str(NOW * 1000)
        + "))));"
    )
    expected = json.loads(
        subprocess.check_output(["node", "--input-type=module", "-e", script], text=True)
    )
    assert [service_state(r, NOW) for r in cases] == expected


def test_bulk_project_index_is_loaded_once_and_legacy_reads_stay_live(tmp_path, monkeypatch):
    from herdr_engineering.control_plane.project_links import load_project_index
    from herdr_engineering.control_plane.service_catalog import service_catalog

    mapping = tmp_path / "projects.json"
    mapping.write_text(json.dumps({"github.com/lab/web": "first"}))
    monkeypatch.setenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", str(mapping))
    index = load_project_index()
    mapping.write_text(json.dumps({"github.com/lab/web": "second"}))
    service = dict(repository_id="github.com/lab/web", commit_sha="a" * 40)
    assert service_catalog(service, project_index=index)["project_id"] == "first"
    assert service_catalog(service, project_index={})["project_id"] == "github.com/lab/web"
    assert service_catalog(service)["project_id"] == "second"


def test_row_budget_bounds_all_retained_snapshots_not_just_each_one():
    pager = ServiceCatalogPager(now=lambda: NOW, max_rows=3)
    first = page(pager, [row("a"), row("b")], limit=1)
    page(pager, [row("c"), row("d")], limit=1)
    with pytest.raises(CatalogSnapshotExpired):
        page(pager, [], limit=1, cursor=first["page"]["next_cursor"])


def test_filters_do_not_narrow_scope_counts_or_host_options():
    rows = [
        row("a"),
        {**row("b"), "host_id": "other", "status": "failed"},
        {**row("old"), "host_id": "oldhost", "lifecycle": {"archived": True}},
    ]
    result = page(ServiceCatalogPager(now=lambda: NOW), rows, search="a", host_id="host")
    assert result["counts"]["scope_total"] == 3
    assert result["counts"]["matching_total"] == 1
    assert result["counts"]["archived"] == 1
    assert [h["value"] for h in result["facets"]["hosts"]] == ["host", "oldhost", "other"]
    assert not page(ServiceCatalogPager(now=lambda: NOW), rows, status="archived")["services"]
    assert (
        page(ServiceCatalogPager(now=lambda: NOW), rows, status="archived", include_archived=True)[
            "services"
        ][0]["service_id"]
        == "old"
    )


def test_projection_runs_only_once_per_snapshot_and_pages_are_detached():
    calls = []

    def factory():
        calls.append(True)
        return [row("a"), row("b"), row("c")]

    pager = ServiceCatalogPager(now=lambda: NOW)
    options = dict(
        scope="herdr",
        status="current",
        include_archived=False,
        search="",
        host_id=None,
        group_by="project",
        limit=1,
    )
    first = pager.page(factory, **options)
    first["counts"]["current"] = 0
    second = pager.page(factory, cursor=first["page"]["next_cursor"], **options)
    assert second["counts"]["current"] == 3
    assert len(calls) == 1
    normalized = dict(options, search="  ", host_id="")
    third = pager.page(factory, cursor=second["page"]["next_cursor"], **normalized)
    assert third["services"][0]["service_id"] == "c"
    assert len(calls) == 1


def test_default_snapshot_capacity_is_sixteen():
    pager = ServiceCatalogPager(now=lambda: NOW)
    first = page(pager, [row("a"), row("b")], limit=1)
    for _ in range(15):
        page(pager, [row("c")])
    assert (
        page(pager, [], limit=1, cursor=first["page"]["next_cursor"])["services"][0]["service_id"]
        == "b"
    )
    page(pager, [row("d")])
    with pytest.raises(CatalogSnapshotExpired):
        page(pager, [], limit=1, cursor=first["page"]["next_cursor"])


def test_byte_budget_evicts_old_snapshots_without_truncating_rows():
    # Size one real snapshot, including all metadata, then allow one plus slack.
    probe = ServiceCatalogPager(now=lambda: NOW)
    page(probe, [row("a"), row("b")], limit=1)
    pager = ServiceCatalogPager(now=lambda: NOW, max_bytes=probe._bytes + 100)
    first = page(pager, [row("a"), row("b")], limit=1)
    second = page(pager, [row("c"), row("d")], limit=1)
    with pytest.raises(CatalogSnapshotExpired):
        page(pager, [], limit=1, cursor=first["page"]["next_cursor"])
    assert (
        page(pager, [], limit=1, cursor=second["page"]["next_cursor"])["services"][0]["service_id"]
        == "d"
    )


def test_state_precedence_matrix_against_javascript():
    cases = []
    for scope in ("herdr", "system", "unassigned"):
        for status in (
            "healthy",
            "running",
            "available",
            "failed",
            "stopped",
            "mystery",
            " HEALTHY ",
        ):
            for lifecycle in (
                {},
                {"state": "unknown"},
                {"state": "current"},
                {"state": "unavailable"},
                {"state": "archived"},
            ):
                for observation in (
                    None,
                    False,
                    {},
                    {"state": "missing"},
                    {"state": "observed", "observed_at": STAMP},
                    {"state": "observed", "observed_at": STAMP, "stale_after_seconds": True},
                ):
                    cases.append(
                        {
                            **row("matrix"),
                            "status": status,
                            "lifecycle": lifecycle,
                            "catalog": {"scope": scope, "metadata_observation": observation},
                        }
                    )
    script = (
        "import {serviceState} from './packages/services-view.js'; const rows="
        + json.dumps(cases)
        + "; console.log(JSON.stringify(rows.map(r=>serviceState(r,"
        + str(NOW * 1000)
        + "))));"
    )
    expected = json.loads(
        subprocess.check_output(["node", "--input-type=module"], input=script, text=True)
    )
    assert [service_state(service, NOW) for service in cases] == expected
