"""Public-side design tokens, bundled Inter and light-only CSS (real sockets, real files)."""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

from herdr_engineering.control_plane import share_public
from tests.control_plane.test_member_web import ORIGIN, req, stack  # noqa: F401  (the real MemberWeb fixture)
from tests.control_plane.test_share_lab import lab_config
from tests.control_plane.test_share_public import Env

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "herdr_engineering" / "control_plane" / "share_static"
THEME = ROOT / "packages" / "herdr-design-system" / "theme.css"
FONTS = ("inter-latin-400.woff2", "inter-latin-600.woff2")


def light_declarations(css: str) -> dict[str, str]:
    """The custom properties of the first ``:root`` block of a stylesheet, whitespace-normalised."""
    block = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {m.group(1): " ".join(m.group(2).split()) for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def test_tokens_css_equals_dashboard_light_tokens():
    tokens = (STATIC / "tokens.css").read_text()
    expected = light_declarations(THEME.read_text())
    assert expected and light_declarations(tokens) == expected
    assert "prefers-color-scheme" not in tokens and "data-theme" not in tokens
    assert "color-scheme: light" in tokens
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_share_tokens.py"), "--check"], capture_output=True)
    assert done.returncode == 0, done.stderr


def test_sync_script_check_fails_when_out_of_date(tmp_path):
    stale = tmp_path / "tokens.css"
    stale.write_text(":root {\n  --accent: #000000;\n}\n")
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_share_tokens.py"), "--check", "--out", str(stale)],
                          capture_output=True)
    assert done.returncode == 1


def test_no_prefers_color_scheme_dark_rules_in_public_css():
    files = [*STATIC.glob("*.css"), ROOT / "packages" / "member-artifacts.css"]
    assert {f.name for f in files} >= {"share.css", "annotate.css", "landing.css", "tokens.css", "member-artifacts.css"}
    for f in files:
        css = f.read_text()
        assert "prefers-color-scheme" not in css, f.name
        assert not re.search(r"color-scheme:\s*[^;]*dark", css), f.name
        assert 'data-theme="dark"' not in css, f.name


def test_fonts_served_same_origin_with_font_mime_and_cache_header(tmp_path):
    e = Env(tmp_path, audience="public", config_extra=lab_config(tmp_path))
    try:
        for name in FONTS:
            status, headers, body = e.request("GET", f"/s/static/fonts/{name}")
            assert status == 200, name
            assert headers["content-type"] == "font/woff2"
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["cache-control"] == share_public.FONT_CACHE and "max-age=" in headers["cache-control"]
            assert body == (STATIC / "fonts" / name).read_bytes()
            assert body[:4] == b"wOF2"
            assert hashlib.sha256(body).hexdigest() in (STATIC / "fonts" / "README.md").read_text()
        assert e.request("GET", "/s/static/fonts/other.woff2")[0] == 404
        assert e.request("GET", "/s/static/fonts/README.md")[0] == 404
        assert e.request("GET", "/s/static/tokens.css")[0] == 200
        status, headers, _ = e.request("GET", "/s/static/share.css")
        assert status == 200 and "font-src" in headers["content-security-policy"]
    finally:
        e.close()


def test_member_page_csp_has_font_src_self_and_no_external_host(stack):  # noqa: F811
    for path in ("/", "/my/callback"):
        csp = req(stack, "GET", path, member=None).headers["content-security-policy"]
        parts = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
        assert parts["font-src"] == ["'self'"]
        assert "http" not in " ".join(parts["font-src"])
    for name in FONTS:
        got = req(stack, "GET", f"/my/fonts/{name}", member=None)
        assert got.status == 200 and got.headers["content-type"] == "font/woff2"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"] == share_public.FONT_CACHE
        assert got.raw == (STATIC / "fonts" / name).read_bytes()
    assert req(stack, "GET", "/my/tokens.css", member=None).status == 200
    assert "fonts.googleapis" not in (ROOT / "packages" / "member-artifacts.html").read_text()


def _declarations(block: str) -> dict[str, str]:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {m.group(1): " ".join(m.group(2).split()) for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def test_dashboard_dark_palette_is_declared_identically_for_os_and_forced_dark():
    """Plain CSS cannot OR a media query with an attribute selector, so the dark palette is written twice; keep them identical."""
    css = THEME.read_text()
    media = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{(.*?)\n  \}\n\}", css, re.S).group(1)
    forced = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1)
    assert _declarations(media) and _declarations(media) == _declarations(forced)


def _luminance(hex_color: str) -> float:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_waiting_text_is_legible_on_white_and_warning_is_not_the_failure_colour():
    light = light_declarations(THEME.read_text())
    assert _contrast(light["--status-waiting"], light["--surface"]) >= 4.5
    assert light["--status-warn"] != light["--status-failed"] and light["--status-warn"].startswith("var(")


def test_warn_badges_use_the_warning_token_and_focus_uses_the_focus_ring():
    components = (ROOT / "packages" / "herdr-web-components" / "components.css").read_text()
    assert re.search(r"\.hd-ins__badge--warn[^{]*\{[^}]*var\(--status-warn", components)
    assert not re.search(r"\.hd-ins__badge--[a-z, .-]*warn[^{]*\{[^}]*status-failed", components)
    for name in ("herdr-design-system/base.css", "herdr-web-components/app-shell.js"):
        text = (ROOT / "packages" / name).read_text()
        assert not re.search(r"focus-visible[^{]*\{[^}]*outline:[^;}]*var\(--accent\)", text), name
    assert "ui-monospace" not in "\n".join(ln for ln in components.splitlines() if ".hd-fleet__" in ln)
