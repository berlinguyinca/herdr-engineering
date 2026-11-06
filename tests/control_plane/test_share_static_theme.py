"""The public share sign-in chrome uses the herdr-design-system token values, light and dark."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "herdr_engineering/control_plane/share_static/share.css").read_text()
THEME = (ROOT / "packages/herdr-design-system/theme.css").read_text()


def test_share_css_tokens_match_the_design_system():
    for value in ("#f7f7f8", "#d9d9e3", "#353740", "#5a5a6e", "#0a7258", "#17181c", "#1f2026", "#3a3b45",
                  "#e6e6ea", "#a4a4b3", "#2fc59c"):
        assert value in CSS and value in THEME, value
    assert '"Inter"' in CSS and "prefers-color-scheme: dark" in CSS and "var(--radius)" in CSS
