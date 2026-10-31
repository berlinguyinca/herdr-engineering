import pytest

from herdr_engineering.control_plane.artifact_renderers import (
    ArtifactRendererError,
    RendererRegistry,
)


def test_markdown_renderer_preserves_source_and_produces_safe_html_output():
    registry = RendererRegistry()
    result = registry.render("document", "# Hello\n\n**world**", {"filename": "readme.md"})
    assert result.profile == "document"
    assert result.source == "# Hello\n\n**world**"
    assert "<h1>Hello</h1>" in result.outputs[0]["content"]
    assert "<strong>world</strong>" in result.outputs[0]["content"]
    assert result.validation["status"] == "passed"


def test_web_renderer_requires_html_and_preserves_editable_source():
    registry = RendererRegistry()
    result = registry.render("web", "<html><body>Hello</body></html>", {})
    assert result.outputs[0]["media_type"] == "text/html"
    with pytest.raises(ArtifactRendererError, match="HTML"):
        registry.render("web", "not html", {})


def test_unknown_profile_returns_structured_unsupported_result():
    registry = RendererRegistry()
    with pytest.raises(ArtifactRendererError, match="unsupported"):
        registry.render("spreadsheet", "source", {})


def test_visualization_and_diagram_profiles_preserve_safe_source_renditions():
    registry = RendererRegistry()
    data = registry.render("visualization", '{"b": 1, "a": 2}', {})
    assert data.outputs[0]["media_type"] == "application/json"
    assert '"a": 2' in data.outputs[0]["content"]
    diagram = registry.render("diagram", "graph TD; A-->B", {})
    assert "data-diagram-format=\"mermaid\"" in diagram.outputs[0]["content"]



def test_unsupported_profile_carries_machine_readable_code():
    with pytest.raises(ArtifactRendererError) as caught:
        RendererRegistry().render("spreadsheet", "source", {})
    assert caught.value.code == "unsupported_format"
    assert caught.value.to_dict()["code"] == "unsupported_format"


def test_markdown_renders_lists_tables_code_links_and_stays_inert():
    source = ("## Findings\n- one with `code`\n- [link](https://example.com)\n\n"
              "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\n<b>raw</b>\n```\n\n"
              "1. first\n2. second\n\n#hashtag line\n\n"
              "[bad](javascript:alert(1)) <script>alert(1)</script>")
    page = RendererRegistry().render("document", source, {}).outputs[0]["content"]
    assert "<h2>Findings</h2>" in page
    assert "<ul><li>one with <code>code</code></li>" in page
    assert '<a href="https://example.com" rel="noopener noreferrer">link</a>' in page
    assert "<table>" in page and "<th>a</th>" in page and "<td>2</td>" in page
    assert "<pre><code>&lt;b&gt;raw&lt;/b&gt;</code></pre>" in page
    assert "<ol><li>first</li>" in page
    assert "<p>#hashtag line</p>" in page
    assert 'href="javascript' not in page and "<script>" not in page


def test_web_validation_reports_active_and_external_content():
    html_source = ("<html><body><script>1</script>"
                   "<img src='https://cdn.example/x.png'></body></html>")
    findings = RendererRegistry().render("web", html_source, {}).validation["findings"]
    codes = {finding["code"] for finding in findings}
    assert codes == {"active_content", "external_resource"}


def test_visualization_with_chart_spec_emits_valid_inert_svg():
    from xml.dom import minidom

    spec = '{"type":"bar","title":"t <b>","data":[{"k":"a","v":1},{"k":"b","v":-3}]}'
    result = RendererRegistry().render("visualization", spec, {})
    assert [o["name"] for o in result.outputs] == ["chart.svg", "data.json"]
    svg = result.outputs[0]["content"]
    minidom.parseString(svg)
    assert "<b>" not in svg and "&lt;b&gt;" in svg and "<script" not in svg
    with pytest.raises(ArtifactRendererError, match="invalid chart spec"):
        RendererRegistry().render("visualization", '{"type":"pie","data":[]}', {})


def test_visualization_without_spec_reports_finding_and_keeps_data():
    result = RendererRegistry().render("visualization", '{"a": 1}', {})
    assert [o["name"] for o in result.outputs] == ["data.json"]
    assert result.validation["findings"][0]["code"] == "no_chart_spec"


def test_flowchart_diagram_renders_svg_and_keeps_source():
    from xml.dom import minidom

    source = "graph TD; A[Start] --> B{Ok?}; B -->|yes| C((Done)); B -.->|no| A"
    result = RendererRegistry().render("diagram", source, {})
    assert result.outputs[0]["name"] == "diagram.html"
    svg = next(o for o in result.outputs if o["name"] == "diagram.svg")["content"]
    minidom.parseString(svg)
    assert svg.count("<g>") == 3 and "marker-end" in svg
    assert 'data-diagram-format="mermaid"' in result.outputs[0]["content"]
    assert "flowchart-svg" in result.validation["checks"]


def test_unsupported_mermaid_type_falls_back_to_source_with_finding():
    result = RendererRegistry().render("diagram", "stateDiagram-v2\n [*] --> A", {})
    assert [o["name"] for o in result.outputs] == ["diagram.html"]
    assert result.validation["findings"][0]["code"] == "not_rendered"


def test_flowchart_cycles_and_oversize_do_not_hang():
    cyclic = "graph LR\nA --> B\nB --> C\nC --> A"
    assert any(o["name"] == "diagram.svg"
               for o in RendererRegistry().render("diagram", cyclic, {}).outputs)
    big = "graph TD\n" + "\n".join(f"N{i} --> N{i + 1}" for i in range(80))
    result = RendererRegistry().render("diagram", big, {})
    assert result.validation["findings"][0]["code"] == "not_rendered"


def test_presentation_splits_slides_outside_code_fences():
    source = "# One\n\n---\n\n## Two\n```\n---\nnot a split\n```\n---\n# Three <script>"
    result = RendererRegistry().render("presentation", source, {})
    deck = result.outputs[0]["content"]
    assert deck.count("<section>") == 3 and result.validation["slide_count"] == 3
    assert "not a split" in deck and "<script>" not in deck


def test_deck_type_scale_is_applied_after_the_page_rules():
    deck = RendererRegistry().render("presentation", "# One\n\n---\n\n## Two", {}).outputs[0]["content"]
    assert deck.count("<style>") == 1
    page_body = deck.index("body{margin:0;font:16px")
    deck_body = deck.index("body{margin:0;font:clamp(")
    assert deck_body > page_body                       # a later rule wins; the deck CSS used to come first
    assert "h1{font-size:2.6em" in deck and "color-scheme:light dark" in deck


# ------------------------------------------------- v2: series, dark mode, sequences

def _render(profile, source):
    return RendererRegistry().render(profile, source, {})


def _svg_of(result, name):
    return next(o for o in result.outputs if o["name"] == name)["content"]


def _assert_dark_mode_svg(svg):
    from xml.dom import minidom

    minidom.parseString(svg)
    assert "<script" not in svg
    assert "@media (prefers-color-scheme: dark)" in svg and "--bg:" in svg and "<style>" in svg
    assert 'fill="#fff' not in svg and "fill:#1f2937" not in svg.split("</style>", 1)[1]
    assert "<rect width=\"100%\"" not in svg


def test_every_generated_svg_has_dark_mode_style_and_no_fixed_background():
    chart = _render("visualization", '{"type":"line","data":[{"k":"a","v":1},{"k":"b","v":2}]}')
    _assert_dark_mode_svg(_svg_of(chart, "chart.svg"))
    flow = _render("diagram", "graph LR; A-->|x|B; B-->A")
    _assert_dark_mode_svg(_svg_of(flow, "diagram.svg"))
    seq = _render("diagram", "sequenceDiagram\nA->>B: hi")
    _assert_dark_mode_svg(_svg_of(seq, "diagram.svg"))
    # inline copy inside diagram.html carries the same stylesheet
    assert "prefers-color-scheme: dark" in seq.outputs[0]["content"]


def test_multi_series_bar_chart_has_legend_union_labels_and_skips_missing():
    spec = {"type": "bar", "title": "t", "series": [
        {"name": "North <b>", "data": [{"k": "a", "v": 1}, {"k": "b", "v": 2}]},
        {"name": "South", "data": [{"k": "b", "v": 3}, {"k": "c", "v": 4}]}]}
    import json
    svg = _svg_of(_render("visualization", json.dumps(spec)), "chart.svg")
    from xml.dom import minidom
    doc = minidom.parseString(svg)
    assert "&lt;b&gt;" in svg and "<b>" not in svg
    labels = [t.firstChild.data for t in doc.getElementsByTagName("text")
              if t.getAttribute("text-anchor") == "middle" and t.firstChild.data in {"a", "b", "c"}]
    assert labels == ["a", "b", "c"]  # union, first-seen order
    bars = [r for r in doc.getElementsByTagName("rect") if r.getAttribute("class").startswith("f") and r.getElementsByTagName("title")]
    assert len(bars) == 4  # a/south and c/north are skipped, not drawn as zero
    assert {r.getAttribute("class") for r in bars} == {"f0", "f1"}
    assert svg.count('width="12" height="12"') == 2  # legend swatches


def test_multi_series_line_chart_breaks_at_gaps_and_cycles_palette():
    import json

    from herdr_engineering.control_plane.artifact_visual_renderers import PALETTE

    series = [{"name": f"s{i}", "data": [{"k": "a", "v": i}, {"k": "b", "v": i + 1}]} for i in range(8)]
    series[0]["data"].append({"k": "c", "v": 5})
    series[1]["data"] = [{"k": "a", "v": 1}, {"k": "c", "v": 2}]  # gap at b
    svg = _svg_of(_render("visualization", json.dumps({"type": "line", "series": series})), "chart.svg")
    from xml.dom import minidom
    doc = minidom.parseString(svg)
    assert len(doc.getElementsByTagName("polyline")) == 7  # series 1 has two isolated points
    assert svg.count("class=\"k7\"") == 1 and len(PALETTE) >= 8


def test_series_cap_and_bad_series_are_rejected_and_single_data_still_works():
    import json
    nine = [{"name": str(i), "data": [{"k": "a", "v": 1}]} for i in range(9)]
    for bad in (nine, [], "x", [1], [{"name": "n"}]):
        with pytest.raises(ArtifactRendererError, match="invalid chart spec"):
            _render("visualization", json.dumps({"type": "bar", "series": bad}))
    svg = _svg_of(_render("visualization", '{"type":"bar","data":[{"k":"a","v":1}]}'), "chart.svg")
    assert 'width="12" height="12"' not in svg  # no legend for the legacy single series


def test_sequence_diagram_renders_participants_arrows_and_notes():
    from xml.dom import minidom

    source = ("%% comment\nsequenceDiagram\nautonumber\nparticipant A as Alice <b>\nactor B as Bob\n"
              "A->>B: solid\nB-->>A: dashed\nA->B: open\nB-->A: open dashed\nA-xB: lost\n"
              "Note over A,B: shared\nNote left of A: left\nNote right of B: right\n"
              "activate A\nA->>A: self\ndeactivate A\nC->>A: <script>x</script>")
    result = _render("diagram", source)
    assert "sequence-svg" in result.validation["checks"] and "findings" not in result.validation
    svg = _svg_of(result, "diagram.svg")
    doc = minidom.parseString(svg)
    assert "<script" not in svg and "<b>" not in svg and "&lt;b&gt;" in svg
    assert svg.count('stroke-dasharray="6 4"') == 2
    assert svg.count("hz-arrow)") == 4  # ->>, -->>, self ->>, C->>A
    assert len(doc.getElementsByTagName("line")) >= 3 + 6  # lifelines + messages
    assert len(doc.getElementsByTagName("g")) == 6  # A,B,C top and bottom
    assert svg.count('class="note"') == 3
    assert 'data-diagram-format="mermaid"' in result.outputs[0]["content"]


def test_sequence_unsupported_constructs_fall_back_naming_the_construct():
    for construct in ("loop", "alt", "opt", "par", "critical", "rect"):
        result = _render("diagram", f"sequenceDiagram\nA->>B: hi\n{construct} stuff\nB->>A: x\nend")
        assert [o["name"] for o in result.outputs] == ["diagram.html"]
        finding = result.validation["findings"][0]
        assert finding["code"] == "not_rendered" and construct in finding["detail"]
        assert 'data-diagram-format="mermaid"' in result.outputs[0]["content"]


def test_sequence_caps_fall_back_and_limits_are_inclusive():
    def build(participants, messages):
        lines = ["sequenceDiagram"] + [f"participant P{i}" for i in range(participants)]
        lines += [f"P0->>P1: m{i}" for i in range(messages)]
        return "\n".join(lines)

    ok = _render("diagram", build(20, 200))
    assert any(o["name"] == "diagram.svg" for o in ok.outputs)
    for source in (build(21, 5), build(2, 201)):
        result = _render("diagram", source)
        assert [o["name"] for o in result.outputs] == ["diagram.html"]
        assert result.validation["findings"][0]["code"] == "not_rendered"


def test_diagram_dispatch_uses_first_non_comment_line_and_flowcharts_are_unaffected():
    seq = _render("diagram", "%% graph TD\n\nsequenceDiagram\nA->>B: hi")
    assert "sequence-svg" in seq.validation["checks"]
    flow = _render("diagram", "%% sequenceDiagram\nflowchart LR\nA-->B")
    assert "flowchart-svg" in flow.validation["checks"]
    cyc = _render("diagram", "graph TD\nA-->B\nB-->A")
    assert "flowchart-svg" in cyc.validation["checks"]
    assert _render("diagram", "pie\n\"a\": 1").validation["findings"][0]["code"] == "not_rendered"


def test_diagram_page_declares_color_scheme_and_still_carries_source():
    rendered = RendererRegistry().render("diagram", "graph TD; A-->B", {}).outputs[0]["content"]
    fallback = RendererRegistry().render("diagram", "stateDiagram-v2\n A --> B", {}).outputs[0]["content"]
    for page in (rendered, fallback):
        assert page.startswith("<!doctype html>") and 'name="color-scheme"' in page
        assert 'data-diagram-format="mermaid"' in page and "<script" not in page


def test_sequence_async_arrows_render_with_arrowheads():
    from xml.dom import minidom

    result = RendererRegistry().render("diagram", "sequenceDiagram\nA-)B: fire\nB--)A: later", {})
    svg = next(o for o in result.outputs if o["name"] == "diagram.svg")["content"]
    minidom.parseString(svg)
    assert "marker-end" in svg and "stroke-dasharray" in svg
    assert "findings" not in result.validation
