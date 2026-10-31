import json
from html.parser import HTMLParser

import pytest

from herdr_engineering.control_plane.artifact_renderers import RendererRegistry


def descriptor():
    return {
        "schema_version": 1,
        "title": "Method comparison",
        "summary": "An exploratory comparison, with uncertainty retained.",
        "questions": [{"id": "q1", "text": "What changed?"}],
        "evidence": [{"id": "e1", "title": "Observed durations", "description": "Thirty observations.",
                      "asset_ids": ["data"]}],
        "findings": [{"id": "f1", "title": "Mixed evidence", "statement": "The comparison is inconclusive.",
                      "question_ids": ["q1"], "evidence_ids": ["e1"], "figure_ids": ["chart"],
                      "limitations": ["Different conditions."], "next_steps": ["Collect comparable observations."]}],
        "figures": [{"id": "chart", "title": "Duration chart", "asset_id": "figure", "caption": "Units: seconds.",
                     "reading_guide": "Compare conditions before comparing values."}],
        "assets": [
            {"id": "data", "name": "evidence.csv", "role": "data", "media_type": "text/csv",
             "sha256": "a" * 64, "size_bytes": 200 * 1024 * 1024},
            {"id": "figure", "name": "chart.svg", "role": "figure", "media_type": "image/svg+xml",
             "sha256": "b" * 64, "size_bytes": 2048},
        ],
        "analysis": {"method": "Descriptive comparison", "configuration": {"baseline": "current"}},
        "limitations": ["No independent validation."],
        "next_steps": ["Evaluate a frozen candidate."],
    }


class _Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.links = []
        self.tags = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag not in {"meta", "br", "hr", "img", "input", "link"}:
            self.stack.append(tag)
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if "href" in attributes:
            self.links.append(attributes["href"])

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def test_preview_uses_references_without_loading_or_embedding_large_assets():
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    calls = []

    def asset_url(asset):
        calls.append(asset["id"])
        return f"/api/private/assets/{asset['id']}"

    preview = render_report_preview(descriptor(), asset_url=asset_url)
    document = _Document()
    document.feed(preview)
    assert not document.stack
    assert set(calls) == {"data", "figure"}
    assert "/api/private/assets/data" in document.links
    assert "/api/private/assets/figure" in document.links
    assert "209715200" in preview
    assert "Collect comparable observations." in preview
    assert "Compare conditions before comparing values." in preview
    assert "#evidence-e1" in document.links
    assert len(preview.encode()) < 20_000
    assert "script" not in document.tags
    assert "img" not in document.tags
    assert "iframe" not in document.tags


def test_preview_follows_the_viewer_colour_scheme_without_scripts_or_remote_styles():
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    page = render_report_preview(descriptor())
    assert "color-scheme:light dark" in page
    assert "prefers-color-scheme:dark" in page
    assert "background:var(--bg)" in page
    assert "<script" not in page and "@import" not in page


def test_preview_escapes_report_text_and_rejects_active_or_remote_asset_links():
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    report = descriptor()
    report["title"] = '<script>alert("unsafe")</script>'
    report["findings"][0]["statement"] = '<img src="https://invalid" onerror="alert(1)">'
    preview = render_report_preview(report)
    assert "<script>" not in preview
    assert "&lt;script&gt;" in preview
    assert "<img" not in preview
    assert 'href="assets/data"' in preview
    for link in ("javascript:alert(1)", "https://external.invalid/a", "//external.invalid/a", "data:text/html,x", "../secret", "/a\nHeader:x", "/a%0aHeader:x"):
        with pytest.raises(ValueError, match="asset URL"):
            render_report_preview(report, asset_url=lambda _asset, link=link: link)


def test_preview_budget_preserves_valid_html_and_discloses_every_omission():
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    report = descriptor()
    report["findings"] = [dict(report["findings"][0], id=f"f{index}", title=f"Unfavorable case {index}",
                               statement="A contrary observation. " * 50) for index in range(100)]
    preview = render_report_preview(report, max_bytes=4096)
    assert len(preview.encode()) <= 4096
    document = _Document()
    document.feed(preview)
    assert not document.stack
    assert "100 findings" in preview
    assert "omitted" in preview
    assert 'href="report"' in preview
    assert "Full descriptor" in preview
    for invalid in (0, 100, True, 1.5):
        with pytest.raises(ValueError, match="max_bytes"):
            render_report_preview(report, max_bytes=invalid)


def test_omitted_reference_targets_lead_to_full_descriptor_instead_of_broken_anchors():
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    report = descriptor()
    report["evidence"][0]["description"] = "Long observation. " * 200
    document = _Document()
    document.feed(render_report_preview(report, max_bytes=4096))
    assert not document.stack
    assert all(link[1:] in document.ids for link in document.links if link.startswith("#"))


def test_report_renderer_preserves_report_profile_and_validated_editable_descriptor():
    report = descriptor()
    registry = RendererRegistry()
    for source, request in ((json.dumps(report), {}), ("", {"report_descriptor": report})):
        result = registry.render("report", source, request)
        assert result.profile == "report"
        assert result.report_descriptor == report
        assert json.loads(result.source) == report
        assert result.outputs[0]["name"] == "preview.html"
        assert result.outputs[0]["media_type"] == "text/html"
        assert result.validation["status"] == "passed"
        assert "report-descriptor" in result.validation["checks"]
        assert "evidence_checked" not in result.validation
        assert "reviewed" not in result.validation


def test_plain_markdown_reports_remain_reports_and_invalid_descriptors_fail_closed():
    registry = RendererRegistry()
    legacy = registry.render("report", "# Existing report", {})
    assert legacy.profile == "report"
    assert legacy.report_descriptor is None
    assert "<h1>Existing report</h1>" in legacy.outputs[0]["content"]
    with pytest.raises(ValueError):
        registry.render("report", '{"schema_version": 1}', {})
    with pytest.raises(ValueError):
        registry.render("report", "", {"report_descriptor": {"schema_version": 1}})


def test_conflicting_editable_source_and_descriptor_are_rejected():
    report = descriptor()
    different = dict(report, title="Different report")
    with pytest.raises(ValueError, match="disagree"):
        RendererRegistry().render("report", json.dumps(different), {"report_descriptor": report})


def test_stable_identifiers_with_colons_have_resolvable_fragment_links():
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    report = descriptor()
    report["evidence"][0]["id"] = "observed:1"
    report["findings"][0]["evidence_ids"] = ["observed:1"]
    preview = render_report_preview(report)
    assert 'id="evidence-observed:1"' in preview
    assert 'href="#evidence-observed%3A1"' in preview
