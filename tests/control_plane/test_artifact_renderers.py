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
        registry.render("presentation", "source", {})


def test_visualization_and_diagram_profiles_preserve_safe_source_renditions():
    registry = RendererRegistry()
    data = registry.render("visualization", '{"b": 1, "a": 2}', {})
    assert data.outputs[0]["media_type"] == "application/json"
    assert '"a": 2' in data.outputs[0]["content"]
    diagram = registry.render("diagram", "graph TD; A-->B", {})
    assert "data-diagram-format=\"mermaid\"" in diagram.outputs[0]["content"]



def test_unsupported_profile_carries_machine_readable_code():
    with pytest.raises(ArtifactRendererError) as caught:
        RendererRegistry().render("presentation", "source", {})
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
