import json
import re
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
    assert "full descriptor" in preview  # wording changed with the polished omission note
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


# --- polished preview: structure, themes, honesty ---------------------------------------------


def _render(report=None, **kwargs):
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    return render_report_preview(report or descriptor(), **kwargs)


def test_preview_has_hero_cards_chips_tables_and_toc_without_debug_text():
    page = _render()
    assert "Artifact report preview" not in page
    assert "Preview coverage" not in page
    assert "Evidence files are separate downloads" not in page
    assert page.count("<h1") == 1 and "<h1" in page.split("<main")[1]
    assert '<header class="hero">' in page and "Method comparison" in page
    assert 'class="card finding"' in page
    assert 'class="chip"' in page
    assert page.count("<table") == 2  # evidence + separate downloads
    assert "<th" in page and "bbbbbbbbbbbb" in page and "b" * 64 not in page  # short sha only
    assert 'class="card figure"' in page and 'class="callout"' in page
    assert 'class="btn"' in page and "Compare conditions before comparing values." in page
    assert '<nav class="toc"' in page and 'href="#sec-findings"' in page
    assert "<pre" not in page
    assert "Descriptive comparison" in page and "baseline" in page


def test_preview_palettes_tokens_layout_and_print():
    page = _render()
    assert "color-scheme:light dark" in page
    assert "@media (prefers-color-scheme:dark)" in page
    assert "#17181c" in page and "#10a37f" in page  # theme.css values, light and dark
    assert "max-width:860px" in page
    assert "overflow-x:auto" in page and "@media print" in page
    assert "Inter" in page and "JetBrains Mono" in page
    assert "<script" not in page and "@import" not in page and "http:" not in page and "https:" not in page and "url(" not in page


def test_csp_is_unchanged():
    csp = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
    assert f'<meta http-equiv="Content-Security-Policy" content="{csp}">' in _render()


def test_hostile_strings_are_escaped_everywhere():
    report = descriptor()
    hostile = '<script>alert(1)</script>"\'><img src=x onerror=alert(2)>'
    report["title"] = hostile
    report["summary"] = hostile
    report["questions"][0]["text"] = hostile
    report["evidence"][0]["title"] = hostile
    report["findings"][0]["title"] = hostile
    report["figures"][0]["caption"] = hostile
    report["figures"][0]["reading_guide"] = hostile
    report["assets"][0]["name"] = hostile
    report["analysis"] = {"method": hostile, "configuration": {hostile: hostile}}
    report["limitations"] = [hostile]
    page = _render(report)
    assert "<script" not in page and "<img" not in page and "onerror=alert(2)>" not in page
    document = _Document()
    document.feed(page)
    assert not document.stack
    assert "script" not in document.tags and "img" not in document.tags


def test_byte_cap_respected_on_oversized_descriptor_and_valid_html_kept():
    report = descriptor()
    report["findings"] = [dict(report["findings"][0], id=f"f{i}", statement="x " * 600)
                          for i in range(200)]
    report["limitations"] = ["limit " * 100 for _ in range(100)]
    for cap in (4096, 20_000, 256 * 1024):
        page = _render(report, max_bytes=cap)
        assert len(page.encode()) <= cap
        document = _Document()
        document.feed(page)
        assert not document.stack


def test_omission_note_only_when_something_was_omitted():
    assert "omitted" not in _render()
    report = descriptor()
    report["findings"] = [dict(report["findings"][0], id=f"f{i}", statement="x " * 400)
                          for i in range(100)]
    page = _render(report, max_bytes=8192)
    assert "Some content was omitted" in page and 'href="report"' in page
    assert "100 findings" in page
    shortened = descriptor()
    shortened["summary"] = "word " * 1000
    note = _render(shortened)
    assert "Some content was omitted" in note and "shortened" in note


def test_analysis_is_a_bounded_key_value_list():
    report = descriptor()
    report["analysis"] = {"method": "m", "configuration": {
        "a_nested": {"baseline": "current"}, **{f"k{i}": i for i in range(200)}}}
    page = _render(report)
    assert "<details" in page and "<dl" in page
    assert "configuration.a_nested.baseline" in page
    assert "Some content was omitted" in page


def test_polished_preview_survives_the_real_share_sanitiser():
    """The public share path pipes the preview through nh3; the styling must come out intact."""
    from herdr_engineering.control_plane import share_render as sr
    from herdr_engineering.control_plane.artifact_share_api import _report_preview

    public = _report_preview(descriptor())
    out = sr.sanitize_html(public, resolve=lambda _context, url: url)
    for needle in (":root,:host{color-scheme:light dark;", "--accent:#10a37f", "--bg:#17181c",
                   "@media (prefers-color-scheme:dark)", ":host([data-theme=\"dark\"])", "@media (max-width:640px)", "@media print",
                   "var(--surface)", "overflow-x:auto", "max-width:860px"):
        assert needle in out, needle
    for needle in ('class="rp"', 'class="hero"', 'class="card finding"', 'class="card figure"',
                   'class="chip"', 'class="tablewrap"', 'class="callout"', 'class="toc"', "<table",
                   "<details", "<dl", "Compare conditions before comparing values."):
        assert needle in out, needle
    # public copies carry no dead links: asset and descriptor anchors are plain text
    assert 'href="#"' not in out and 'href="report"' not in out and "Download figure" not in out
    assert 'href="#sec-findings"' in out  # in-page contents links are kept


def test_question_id_chips_never_wrap_or_collapse_into_blobs():
    page = _render()
    rule = re.search(r"\.qlist \.chip,\.chips \.chip\{([^}]*)\}", page).group(1)
    assert "white-space:nowrap" in rule and "overflow-wrap:normal" in rule
    assert "grid-template-columns:max-content" in page
    assert "border-radius:999px" in re.search(r"\.chip\{([^}]*)\}", page).group(1)


def test_theme_override_selectors_are_in_the_stylesheet():
    page = _render()
    assert ':root[data-theme="dark"]' in page and ':host([data-theme="dark"])' in page
    assert ':root:not([data-theme="light"])' in page and ':host(:not([data-theme="light"]))' in page
    # none of the override machinery needs a script or an attribute the sanitiser strips from the markup
    assert "<script" not in page and 'data-theme="' not in page.split("</style>")[1]


def test_table_cells_do_not_break_words_character_by_character():
    page = _render()
    assert "overflow-wrap:anywhere" not in page and "overflow-wrap:break-word" in page
    assert "td .chip{white-space:nowrap}" in page


def test_public_preview_has_no_dangling_report_links_when_referenced_evidence_is_omitted():
    """Chips for omitted evidence fall back to href="report"; the public copy must not keep that dead link."""
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview
    from herdr_engineering.control_plane.artifact_share_api import _report_preview
    from herdr_engineering.control_plane import share_render as sr

    report = descriptor()
    report["evidence"] = [{"id": f"e{i}", "title": f"Evidence {i}", "description": "d " * 900, "asset_ids": ["data"]}
                          for i in range(40)]
    report["findings"][0]["evidence_ids"] = ["e39", "e0"]
    # the cap-limited renderer really leaves the chip pointing at the descriptor...
    small = render_report_preview(report, max_bytes=16 * 1024)
    assert '<a class="chip" href="report">' in small
    # ...so the public copy at the same cap must turn it into plain text, before and after the sanitiser
    public = _report_preview(report, max_bytes=16 * 1024)
    for page in (public.decode(), sr.sanitize_html(public, resolve=lambda _context, url: url)):
        assert 'href="report"' not in page and 'href="#"' not in page
        assert "e39" in page  # the id stays visible as plain text


@pytest.mark.parametrize("char", ['"', "\U0001F9EA", "<", "&", "x"])
def test_minimum_cap_holds_for_the_longest_hostile_title(char):
    report = descriptor()
    report["title"] = char * 256
    page = _render(report, max_bytes=4096)
    assert len(page.encode()) <= 4096
    document = _Document()
    document.feed(page)
    assert not document.stack
    assert page.count("<h1>") == 1 and "<script" not in page
