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


def test_csp_only_gained_data_images():
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'")
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


def _note(page: str) -> str | None:
    found = re.search(r'<aside class="notice" role="note">(.*?)</aside>', page)
    return found.group(1) if found else None


def test_note_is_present_exactly_when_something_was_omitted_or_shortened():
    assert _note(_render()) is None  # nothing left out

    omitted = descriptor()
    omitted["findings"] = [dict(omitted["findings"][0], id=f"f{i}", statement="x " * 400) for i in range(100)]
    note = _note(_render(omitted, max_bytes=8192))
    assert note and "shortened or omitted" in note and 'href="report"' in note and "of 100 findings" in note

    for field in ("summary", "limitations"):  # kept but cut short: still disclosed, nothing omitted
        shortened = descriptor()
        shortened[field] = "word " * 1000 if field == "summary" else ["word " * 1000]
        note = _note(_render(shortened))
        assert note and "shortened or omitted" in note
        assert "Shown:" not in note


def test_text_cut_only_inside_dropped_entries_adds_nothing_beyond_the_omission_counts():
    report = descriptor()
    # f0 is short and kept; the 99 long ones are dropped. Their cut text must not claim a kept entry was shortened.
    report["findings"] = [dict(report["findings"][0], id="f0")] + [
        dict(report["findings"][0], id=f"f{i}", statement="y" * 5000) for i in range(1, 100)]
    page = _render(report, max_bytes=16384)
    note = _note(page)
    assert note and "of 100 findings" in note
    assert "text shortened" not in page and "Some text was shortened" not in note
    assert 'id="finding-f0"' in page


def test_analysis_is_a_bounded_key_value_list():
    report = descriptor()
    report["analysis"] = {"method": "m", "configuration": {
        "a_nested": {"baseline": "current"}, **{f"k{i}": i for i in range(200)}}}
    page = _render(report)
    assert "<details" in page and "<dl" in page
    assert "configuration.a_nested.baseline" in page
    assert "shortened or omitted" in page


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
    from herdr_engineering.control_plane import share_render as sr
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview
    from herdr_engineering.control_plane.artifact_share_api import _report_preview

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


def _luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    digits = digits if len(digits) == 6 else "".join(char * 2 for char in digits)
    linear = [(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
              for v in (int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):([^;}]+)", block))


def _palettes(page: str) -> tuple[dict[str, str], dict[str, str]]:
    light = _tokens(re.search(r":root,:host\{([^}]*)\}", page).group(1))
    dark = _tokens(re.search(r':root\[data-theme="dark"\],:host\(\[data-theme="dark"\]\)\{([^}]*)\}', page).group(1))
    return light, dark


# (text token, background token): every text/background pairing the stylesheet actually draws
_TEXT_PAIRS = [("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("text", "accent-weak"),
               ("text", "warn-bg"), ("muted", "bg"), ("muted", "surface"), ("muted", "surface-2"),
               ("link", "bg"), ("link", "surface"), ("link", "surface-2"), ("link", "accent-weak"),
               ("on-accent", "btn"), ("warn", "warn-bg")]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_both_palettes_meet_wcag_aa_for_every_text_pairing(scheme):
    light, dark = _palettes(_render())
    palette = light if scheme == "light" else {**light, **dark}
    failures = {(fg, bg): round(_contrast(palette[fg], palette[bg]), 2) for fg, bg in _TEXT_PAIRS
                if _contrast(palette[fg], palette[bg]) < 4.5}
    assert not failures


def test_contrast_helper_matches_known_wcag_values():
    assert round(_contrast("#000", "#fff"), 1) == 21.0
    assert round(_contrast("#fff", "#fff"), 1) == 1.0
    assert round(_contrast("#777", "#fff"), 2) == 4.48  # the classic just-failing grey


def test_card_shadow_is_a_token_with_a_dark_value_and_buttons_use_the_button_token():
    page = _render()
    light, dark = _palettes(page)
    assert light["shadow"] != dark["shadow"] and "box-shadow:var(--shadow)" in page
    assert "background:var(--btn)" in page and "color:var(--on-accent)" in page


# ------------------------------------------------------------------------------------------------ inline figures
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
HOSTILE_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script>'
               b'<style>@import url(http://evil.example/x.css);</style>'
               b'<foreignObject><iframe src="http://evil.example"/></foreignObject>'
               b'<image href="http://evil.example/p.png"/><use href="http://evil.example/s.svg#a"/></svg>')


def _img_src(page):
    found = re.findall(r'<img class="fig-img" alt="([^"]*)" src="([^"]*)">', page)
    return found


def test_figure_is_embedded_as_an_opaque_data_uri_above_its_caption_and_keeps_the_download_link():
    import base64

    page = _render(figure_images={"figure": ("image/svg+xml", HOSTILE_SVG)})
    ((alt, src),) = _img_src(page)
    assert alt == "Duration chart"
    assert re.fullmatch(r"data:image/svg\+xml;base64,[A-Za-z0-9+/=]+", src)
    assert base64.b64decode(src.split(",", 1)[1]) == HOSTILE_SVG
    for needle in ("<svg", "<script", "onload", "foreignObject", "evil.example", "@import", "javascript:"):
        assert needle not in page, needle
    assert page.index("fig-img\" alt") < page.index("<p>Units: seconds.</p>")
    assert 'download>Download figure</a>' in page
    assert _note(page) is None  # nothing was dropped


def test_raster_types_are_embedded_and_other_types_are_not():
    for media in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        assert len(_img_src(_render(figure_images={"figure": (media, PNG)}))) == 1, media
    for media in ("text/html", "application/octet-stream", "image/svg+xml; charset=utf-8", "text/csv"):
        page = _render(figure_images={"figure": (media, b"<html>x</html>")})
        assert "<img" not in page and "data:" not in page.split("</style>", 1)[1], media


def test_absent_images_leave_the_caption_and_link_and_only_a_failed_attempt_is_disclosed():
    page = _render()  # the caller supplied nothing: nothing was dropped
    assert "<img" not in page and "Units: seconds." in page and "Download figure" in page
    assert _note(page) is None
    for images in ({}, {"data": ("image/png", PNG)}):  # tried, but the figure's own asset has no bytes
        page = _render(figure_images=images)
        assert "<img" not in page and "Units: seconds." in page and "Download figure" in page
        assert "shortened or omitted" in _note(page)


def test_an_oversize_image_degrades_to_caption_and_link_and_is_disclosed():
    big = b"\x00" * 120_000  # 160k chars of base64: over the per-figure cap
    page = _render(figure_images={"figure": ("image/png", big)})
    assert "<img" not in page and "Units: seconds." in page and "Download figure" in page
    assert "shortened or omitted" in _note(page)


def test_images_are_given_up_before_any_text_entry_and_the_cap_still_holds():
    report = descriptor()
    picture = ("image/png", b"\x00" * 60_000)  # about 80k chars
    roomy = _render(report, figure_images={"figure": picture})
    assert len(_img_src(roomy)) == 1
    tight = _render(report, figure_images={"figure": picture}, max_bytes=40_000)
    assert "<img" not in tight and len(tight.encode()) <= 40_000
    assert "Collect comparable observations." in tight and "Mixed evidence" in tight
    assert "Download figure" in tight and "shortened or omitted" in _note(tight)
    plain = _render(report, max_bytes=40_000)
    assert "<img" not in plain and _note(plain) is None


def test_the_smallest_cap_holds_with_hostile_figures_and_images():
    report = descriptor()
    report["figures"] = [dict(report["figures"][0], id="chart" if i == 0 else f"c{i}", title="T" * 3000, caption="C" * 5000)
                         for i in range(30)]
    page = _render(report, max_bytes=4096, figure_images={"figure": ("image/png", PNG)})
    assert len(page.encode()) <= 4096


def test_image_markup_is_not_injectable_through_the_figure_title():
    report = descriptor()
    report["figures"][0]["title"] = '"><script>alert(1)</script>'
    page = _render(report, figure_images={"figure": ("image/png", PNG)})
    assert "<script" not in page and "&lt;script" in page


# --- a section whose budget fits none of its entries is hidden, but never silently ---------------

_SECTIONS = {"questions": "Questions", "findings": "Findings", "figures": "Figures", "evidence": "Evidence",
             "downloads": "Separate downloads", "analysis": "Analysis", "limitations": "Limitations",
             "next-checks": "Next checks"}


def _evidence_heavy():
    report = descriptor()
    report["evidence"] = [{"id": f"e{i}", "title": f"Evidence {i}", "description": "d " * 900, "asset_ids": ["data"]}
                          for i in range(3)]
    report["findings"][0]["evidence_ids"] = ["e0", "e2"]
    return report


def _check_no_empty_or_dangling_sections(page: str):
    document = _Document()
    document.feed(page)
    assert not document.stack
    toc = re.search(r'<nav class="toc".*?</nav>', page).group()
    for anchor, title in _SECTIONS.items():
        has_heading = f'<section id="sec-{anchor}">' in page
        assert has_heading == (f'href="#sec-{anchor}"' in toc)  # a chip exactly when the section is there
        if has_heading:
            body = re.search(rf'<section id="sec-{anchor}">(.*?)</section>', page, re.S).group(1)
            assert re.search(r'<(tr><td|tr id|li|article|dt)', body), f"{anchor} has a heading but no entry"
    # (a heading's own permalink points at its own section; the contents chips are the only other section links)
    assert 'href="#sec-' not in re.sub(r'<a class="anchor"[^>]*>#</a>', "", page.replace(toc, "")) and not [
        link for link in document.links if link.startswith("#") and link[1:] not in document.ids]


@pytest.mark.parametrize("cap", [3000, 6000, 12000, 40000])
def test_a_section_with_no_room_is_hidden_with_its_chip_and_the_note_stays_truthful(cap):
    from herdr_engineering.control_plane.artifact_report_render import render_report_preview

    report = _evidence_heavy()
    report["title"] = "T"
    page = render_report_preview(report, max_bytes=max(cap, 4096))
    assert len(page.encode()) <= max(cap, 4096)
    _check_no_empty_or_dangling_sections(page)
    shown = len(re.findall(r'<tr id="evidence-', page))
    if shown < 3:
        note = _note(page)
        assert note and f"{shown} of 3 evidence entries" in note and 'href="report"' in note
    if '<section id="sec-evidence">' not in page:
        assert "0 of 3 evidence entries" in _note(page)
        assert "<th>Evidence</th>" not in page


def test_hidden_evidence_section_is_actually_exercised_and_a_full_render_is_unchanged():
    page = _render(_evidence_heavy(), max_bytes=12000)
    assert '<section id="sec-evidence">' not in page and "0 of 3 evidence entries" in _note(page)
    full = _render()
    assert all(f'<section id="sec-{a}">' in full for a in _SECTIONS)
    assert _note(full) is None


def test_public_share_path_has_no_dangling_links_when_a_section_is_hidden():
    from herdr_engineering.control_plane import share_render as sr
    from herdr_engineering.control_plane.artifact_share_api import _report_preview

    for cap in (6000, 12000, 40000):
        public = _report_preview(_evidence_heavy(), max_bytes=cap)
        for page in (public.decode(), sr.sanitize_html(public, resolve=lambda _context, url: url)):
            assert 'href="report"' not in page and 'href="#"' not in page
            document = _Document()
            document.feed(page)
            assert not [link for link in document.links if link.startswith("#") and link[1:] not in document.ids]


# --- print / save-as-PDF -----------------------------------------------------------------------------------------


def test_print_stylesheet_forces_light_hides_chrome_and_controls_page_breaks():
    page = _render()
    css = page.split("<style>")[1].split("</style>")[0]
    printed = css[css.index("@media print"):]
    # light variables re-declared for every theme selector, after the dark rules (so they win in any theme)
    assert css.rindex("@media print") > css.index("--bg:#17181c")
    for selector in (":root[data-theme]", ":host([data-theme])", ":root:not([data-theme=\"light\"])"):
        assert selector in printed
    assert "--bg:#fff" in printed and "--text:#000" in printed and "color-scheme:light" in printed
    # no chrome: contents pills, download buttons, the opt-in Rich view panel
    assert ".toc,.btn,.anchor,.howto{display:none}" in printed and ".rp>section:not([id]){display:none!important}" in printed
    # page-break control around findings and figures; a table row and its header
    assert "break-inside:avoid" in printed and ".finding" in printed and ".figure" in printed
    assert "break-after:avoid" in printed and "thead{display:table-header-group}" in printed
    # closed details print open; external URLs are spelled out; tall figures shrink to a page
    assert "details::details-content" in printed and 'a[href^="http"]::after' in printed
    assert "max-height:180mm" in printed


def test_print_light_override_survives_the_tight_core_stylesheet_and_the_share_sanitiser():
    from herdr_engineering.control_plane import share_render as sr
    from herdr_engineering.control_plane.artifact_share_api import _report_preview

    small = _render(dict(descriptor(), findings=[], figures=[], evidence=[], assets=[], analysis={}), max_bytes=4096)
    assert "@media print" in small and "--bg:#fff" in small  # a dark screen theme never reaches paper, even then
    out = sr.sanitize_html(_report_preview(descriptor()), resolve=lambda _context, url: url)
    assert "details::details-content" in out and "break-inside:avoid" in out and "table-header-group" in out


# --- finding anchors -------------------------------------------------------------------------------------------------


def _anchor_links(page: str) -> list[tuple[str, str]]:
    return re.findall(r'<a class="anchor" href="#([^"]+)" title="([^"]*)" aria-label="[^"]*" aria-describedby="[^"]*">#</a>', page)


def test_every_section_finding_and_figure_heading_has_a_permalink_to_an_existing_unique_id():
    from urllib.parse import unquote

    page = _render()
    ids = re.findall(r' id="([^"]+)"', page)
    assert len(ids) == len(set(ids)), "ids are unique"
    targets = [unquote(href) for href, _ in _anchor_links(page)]
    assert set(targets) >= {f"sec-{key}" for key in _SECTIONS} | {"finding-f1", "figure-chart"}
    assert all(target in ids for target in targets)
    assert len(targets) == len(set(targets))
    described = re.findall(r'aria-describedby="([^"]+)"', page)
    assert described and all(name in ids for name in described), "each permalink is described by an existing heading"
    # the link sits inside its own heading, so the visible '#' is next to what it names
    assert re.search(r'<h3 id="h-finding-f1">Mixed evidence<a class="anchor" href="#finding-f1"', page)
    assert re.search(r'<h2 id="h-sec-findings">Findings<a class="anchor" href="#sec-findings"', page)
    assert ".anchor:focus-visible{outline" in page and ".card:target" in page and "min-width:24px" in page
    assert ".toc,.btn,.anchor,.howto{display:none}" in page, "permalinks and legend are screen-only"


def test_anchor_ids_are_stable_across_renders_and_follow_the_descriptor_id_not_the_title():
    first, again = _render(), _render()
    assert first == again
    retitled = descriptor()
    retitled["findings"][0]["title"] = "A completely different title"
    changed = _render(retitled)
    assert 'id="finding-f1"' in changed and 'href="#finding-f1"' in changed
    assert [h for h, _ in _anchor_links(first)] == [h for h, _ in _anchor_links(changed)]


def test_permalinks_escape_hostile_titles_and_percent_encode_unusual_ids():
    from urllib.parse import unquote

    hostile = descriptor()
    title = '"><img src=x onerror=alert(1)><script>alert(2)</script> \' & </h3>'
    hostile["findings"][0].update(id="f:1.a", title=title)
    hostile["figures"][0]["title"] = title
    page = _render(hostile)
    assert "<script>alert" not in page and "<img src=x" not in page and 'onerror=alert(1)>' not in page.replace("&gt;", "")
    document = _Document()
    document.feed(page)
    assert not document.stack
    assert 'id="finding-f:1.a"' in page and 'href="#finding-f%3A1.a"' in page
    assert unquote("finding-f%3A1.a") in document.ids
    assert 'aria-label="Link to this finding"' in page and 'Link to finding: ' not in page


def test_a_tight_budget_stays_plain_without_permalinks():
    small = _render(dict(descriptor(), findings=[], figures=[], evidence=[], assets=[], analysis={}), max_bytes=12000)
    assert 'class="anchor"' not in small and 'href="#sec-' in small  # contents chips stay; no per-heading links


def test_permalinks_and_ids_survive_the_public_share_sanitiser():
    from herdr_engineering.control_plane import share_render as sr
    from herdr_engineering.control_plane.artifact_share_api import _report_preview

    out = sr.sanitize_html(_report_preview(descriptor()), resolve=sr.share_resolver("preview.html", {"preview.html"}))
    assert 'id="finding-f1"' in out and 'class="anchor"' in out and 'href="#finding-f1"' in out


def test_dashboard_static_copy_keeps_the_ids_and_anchor_class_the_client_wires_up():
    """The dashboard resolver drops relative hrefs (a fragment would drive the hash router), so the client rebuilds each
    permalink's link from the nearest id: that needs the id, the class and the title to survive, nothing wider."""
    from herdr_engineering.control_plane import artifact_render_api as api

    page = _render(asset_url=lambda asset: "#").encode()
    html = api._envelope("html", page, api.dashboard_resolver(), None)["html"]
    assert 'id="finding-f1"' in html and 'id="sec-findings"' in html
    tags = re.findall(r'<a class="anchor"[^>]*>#</a>', html)
    assert tags and all("href=" not in tag for tag in tags)
    assert any('title="Link to this finding"' in tag and 'aria-label="Link to this finding"' in tag and 'aria-describedby="h-finding-f1"' in tag
               for tag in tags)


@pytest.mark.parametrize("title", ["x onerror=alert(1) y", "javascript:alert(1)", "a <b> & \"q\" title"])
def test_a_script_looking_title_never_fails_the_dashboard_or_share_render(title):
    """The sanitiser aborts a whole render on a ``title`` attribute that looks like a script, so a permalink must never
    copy free text into ``title`` (only into ``aria-label``); a finding titled ``onerror=`` still renders."""
    from herdr_engineering.control_plane import artifact_render_api as api

    base = descriptor()
    page = _render(dict(base, findings=[dict(base["findings"][0], title=title)] + base["findings"][1:]), asset_url=lambda asset: "#").encode()
    html = api._envelope("html", page, api.dashboard_resolver(), None)["html"]
    assert 'class="anchor"' in html and 'id="finding-f1"' in html


def _legend(page: str) -> str:
    match = re.search(r'<details class="howto">.*?</details>', page, re.S)
    assert match, "the report has a How to read legend"
    return match.group()


def test_how_to_read_legend_is_collapsed_first_and_explains_only_labels_the_report_shows():
    page = _render()
    legend = _legend(page)
    assert "<details class=\"howto\">" in page and " open" not in legend.split(">", 1)[0], "collapsed by default"
    assert "<summary>How to read this report</summary>" in legend
    assert page.index('class="howto"') < page.index('<section id="sec-findings"'), "it sits at the top, before the findings"
    terms = re.findall(r"<dt>(.*?)</dt>", legend)
    body = page.replace(legend, "")
    # every explained label is one this renderer printed (derived, not invented)
    for label in ("Question", "Evidence", "Figure", "Limits", "Next checks", "How to read", "Method and configuration"):
        assert any(label in term for term in terms), f"{label} is explained"
        assert label in body, f"{label} really appears in the report"
    assert "#" in terms and "Finding" in terms


def test_how_to_read_legend_leaves_out_terms_the_report_does_not_use():
    base = descriptor()
    bare = dict(base, figures=[], analysis={}, findings=[dict(base["findings"][0], figure_ids=[], limitations=[], next_steps=[])])
    legend = _legend(_render(bare))
    for absent in ("Figure", "Limits", "Next checks", "How to read", "Method and configuration"):
        assert f"<dt>{absent}" not in legend and f" · {absent}" not in legend, f"{absent} is not explained"
    assert "<dt>Finding</dt>" in legend
    nothing_to_explain = _render(dict(base, findings=[], figures=[], evidence=[], assets=[], analysis={}))
    assert 'class="howto"' not in nothing_to_explain


def test_the_legend_says_there_is_no_severity_or_confidence_because_the_descriptor_has_none():
    from herdr_engineering.control_plane.artifact_report_descriptor import (
        ReportDescriptorError,
        validate_report_descriptor,
    )

    base = descriptor()
    for field in ("severity", "confidence"):
        rated = dict(base, findings=[dict(base["findings"][0], **{field: "high"})])
        with pytest.raises(ReportDescriptorError):
            validate_report_descriptor(rated)
    assert "<dt>Severity, confidence</dt><dd>Not used" in _legend(_render())


def test_the_legend_is_static_and_never_copies_report_text():
    first = _legend(_render())
    retitled = descriptor()
    retitled["findings"][0]["title"] = '<script>alert(1)</script> "x" & y'
    retitled["title"] = "Another report"
    assert _legend(_render(retitled)) == first


def test_the_legend_is_screen_only_stays_off_tight_budgets_and_survives_the_sanitiser():
    from herdr_engineering.control_plane import artifact_render_api as api

    page = _render()
    assert ".toc,.btn,.anchor,.howto{display:none}" in page, "hidden in print"
    assert ".howto summary:focus-visible{outline" in page
    assert "howto" not in _render(max_bytes=12000).split("</style>")[1], "a tight budget stays plain"
    html = api._envelope("html", _render(asset_url=lambda asset: "#").encode(), api.dashboard_resolver(), None)["html"]
    legend = _legend(html)
    assert "<summary>How to read this report</summary>" in legend and "<dl>" in legend and " open" not in legend.split(">", 1)[0]
