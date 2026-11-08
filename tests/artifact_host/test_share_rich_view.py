"""The opt-in Rich view helpers the share path needs: asset choice, sandbox headers, the hashed toggle script."""

import base64
import hashlib

import pytest

from herdr_artifact_host import rich_view as rich
from herdr_artifact_host import share_rich_view as share_rich

PAGE_META = f'<meta http-equiv="Content-Security-Policy" content="{rich.PREVIEW_CSP}">'
PREVIEW = f"<html><head>{PAGE_META}</head><body><header>h</header><main>m</main></body></html>"


def asset(aid, mime="text/html", role="preview"):
    return {"id": aid, "name": aid, "role": role, "media_type": mime}


def test_pick_html_asset_needs_exactly_one_choice():
    assert rich.pick_html_asset({"assets": []}) is None
    assert rich.pick_html_asset({}) is None
    only = asset("a", role="data")
    assert rich.pick_html_asset({"assets": [only, asset("c", "text/csv")]}) is only
    assert (
        rich.pick_html_asset({"assets": [asset("a", role="data"), asset("b", role="data")]}) is None
    )
    winner = asset("b")
    assert rich.pick_html_asset({"assets": [asset("a", role="data"), winner]}) is winner
    assert rich.pick_html_asset({"assets": [asset("a"), asset("b")]}) is None
    assert rich.pick_html_asset({"assets": ["junk", asset("x", "text/html,text/plain")]}) is None


def test_content_error_is_a_plain_exception():
    assert issubclass(rich.ContentError, Exception)


def test_rich_csp_is_sandboxed_without_any_allow_token():
    assert (
        rich.RICH_CSP.startswith("sandbox;")
        and "allow-" not in rich.RICH_CSP
        and "script-src" not in rich.RICH_CSP
    )
    assert rich.RICH_CSP == (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    )


def test_share_headers_drop_only_the_two_framing_controls():
    assert (
        "frame-ancestors" not in share_rich.SHARE_RICH_CSP
        and share_rich.SHARE_RICH_CSP.startswith("sandbox;")
    )
    names = {k for k, _ in share_rich.SHARE_RICH_HEADERS}
    assert "X-Frame-Options" not in names
    assert names == {k for k, _ in rich.RICH_HEADERS} - {"X-Frame-Options"}
    assert (
        dict(share_rich.SHARE_RICH_HEADERS)["Content-Security-Policy"] == share_rich.SHARE_RICH_CSP
    )
    assert share_rich.MAX_RICH_BYTES == 2 * 1024 * 1024


def test_with_toggle_allows_exactly_one_hashed_script_and_self_frames():
    out = share_rich.with_toggle(PREVIEW)
    code = (share_rich._TOGGLE_JS).read_text(encoding="utf-8")
    digest = base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()
    assert f"{rich.PREVIEW_CSP}; script-src 'sha256-{digest}'; frame-src 'self'" in out
    assert out.count("<script>") == 1 and "data-rich-toggle" in out
    assert out.index("</header>") < out.index("data-rich-view")
    assert "allow-same-origin" not in out


@pytest.mark.parametrize(
    "page", ["<html><header>x</header></html>", PAGE_META + PAGE_META + "</header>", PAGE_META]
)
def test_with_toggle_refuses_a_page_without_the_expected_policy_and_header(page):
    with pytest.raises(ValueError):
        share_rich.with_toggle(page)


def test_select_author_asset_uses_the_dashboard_rule():
    a = asset("a")
    assert share_rich.select_author_asset({"assets": [a]}) is a
