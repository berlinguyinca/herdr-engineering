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

