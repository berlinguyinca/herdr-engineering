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
    result = RendererRegistry().render("diagram", "sequenceDiagram\n A->>B: hi", {})
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
