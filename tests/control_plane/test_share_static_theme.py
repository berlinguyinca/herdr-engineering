"""The public share pages use the herdr-design-system light token values."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "herdr_engineering/control_plane/share_static/share.css").read_text()
THEME = (ROOT / "packages/herdr-design-system/theme.css").read_text()


def test_share_css_tokens_match_the_design_system():
    """Public share pages are light only: the dashboard's light values reach them through the generated tokens.css."""
    tokens = (ROOT / "herdr_engineering/control_plane/share_static/tokens.css").read_text()
    for value in ("#f7f7f8", "#d9d9e3", "#353740", "#5a5a6e", "#0a7258"):
        assert value in tokens and value in THEME, value
    assert "Inter" in tokens and "var(--radius" in CSS and "prefers-color-scheme: dark" not in CSS + tokens
