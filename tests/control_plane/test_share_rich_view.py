"""Opt-in "Rich view" of a report share: the author's HTML page in the bundle, served sandboxed, over real HTTP."""
import hashlib
import http.client
import json
import threading
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import artifact_rich_view as dashboard
from herdr_engineering.control_plane import share_rich_view as rich
from herdr_engineering.control_plane.share_public import ShareConfig, make_share_server
from tests.control_plane.test_artifact_share_api import (
    AID,
    SHARES,
    approve,
    create,
    output,
    report_document,
    request,
    share_server,
    token_of,
    tree,
)

HOSTILE = (b"<!doctype html><html><body><h1>Pretty</h1><script>window.top.document.title='x';fetch('/s/')</script>"
           b"<img src=https://evil.example/p.gif><form action=/x><button>go</button></form>"
           b"<style>h1{color:#c00}</style></body></html>")
RICH_HEADERS = {
    "content-security-policy": ("sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
                                "base-uri 'none'; form-action 'none'"),
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    "cache-control": "private, no-store"}


def html_asset(blobs, data, name="report.html", role="preview", aid=None):
    sha = blobs.add(data)
    return {"id": aid or name, "name": name, "role": role, "media_type": "text/html", "sha256": sha,
            "size_bytes": len(data)}


def add_version(registry, blobs, *assets, vid="v_rich"):
    document = report_document()
    document["assets"] = document["assets"] + list(assets)
    registry.versions[vid] = {"version_id": vid, "artifact_id": AID, "state": "ready", "report_descriptor": document,
                              "output_manifest": [output("data.csv", b"evidence", media_type="text/csv")]}
    return vid


class Host:
    """The real share host over a bundle root the owner API wrote, with a movable clock."""

    def __init__(self, root, tmp_path):
        events = tmp_path / "events"
        events.mkdir(exist_ok=True)
        self.clock = [datetime.now(UTC).timestamp()]
        config = ShareConfig(shares_root=root, events_path=events / "events.jsonl")
        self.server = make_share_server(config, host="127.0.0.1", port=0, now=lambda: self.clock[0])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()

    def raw_header_names(self, path):
        """Every header name as sent, duplicates kept (``get`` folds them into a dict)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            return [k.lower() for k, _ in response.getheaders()]
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def world(tmp_path):
    with share_server(tmp_path) as (base, registry, shares, blobs, root):
        host = Host(root, tmp_path)
        try:
            yield base, registry, shares, blobs, root, host
        finally:
            host.close()


def shared(world, *, flag, vid="v_rich", asset=None):
    base, registry, _, blobs, root, host = world
    add_version(registry, blobs, asset or html_asset(blobs, HOSTILE), vid=vid)
    body = {"include_author_html": True} if flag else {}
    status, made = create(base, version=vid, **body)
    assert status == 201, made
    status, done = approve(base, made["share"]["share_id"])
    assert status == 200, done
    return made, done, token_of(done["url"])


def test_default_is_off_bundle_state_and_route_never_hold_the_author_page(world):
    base, _, _, _, root, host = world
    made, done, token = shared(world, flag=False)
    share_id = made["share"]["share_id"]
    assert made["summary"]["author_html"] is False and [f["name"] for f in made["summary"]["files"]] == ["preview.html"]
    assert tree(root / share_id / "files") == ["preview.html"]
    state = json.loads(next((root / "by-token").glob("*/state.json")).read_text())
    assert "rich" not in state
    assert HOSTILE not in b"".join(p.read_bytes() for p in (root / share_id).rglob("*") if p.is_file())
    assert host.get(f"/s/{token}/f/author-page.html")[0] == 404
    status, _, preview = host.get(f"/s/{token}/f/preview.html")
    assert status == 200 and b"data-rich-view" not in preview and b"iframe" not in preview.lower()


def test_approver_sees_the_author_page_in_the_plan(world):
    base, registry, _, blobs, _, _ = world
    add_version(registry, blobs, html_asset(blobs, HOSTILE))
    status, made = create(base, version="v_rich", include_author_html=True)
    assert status == 201
    summary = made["summary"]
    assert summary["author_html"] is True and summary["entry"] == "preview.html"
    assert [f["name"] for f in summary["files"]] == ["preview.html", "author-page.html"]
    assert summary["files"][1]["size_bytes"] == len(HOSTILE)
    text = " ".join(summary["warnings"])
    assert "report.html" in text and "IS included" in text and "anyone holding the link" in text
    assert "evidence files are not included" in text
    # the pending share shows the same plan again before approve
    status, body = request(base, f"{SHARES}/{made['share']['share_id']}")
    shown = json.loads(body)["summary"]
    assert shown["author_html"] is True and "author-page.html" in [f["name"] for f in shown["files"]]


def test_every_share_response_sends_each_security_header_once(world):
    _, _, _, _, _, host = world
    _, _, token = shared(world, flag=True)
    for name in ("author-page.html", "preview.html"):
        names = host.raw_header_names(f"/s/{token}/f/{name}")
        assert names.count("referrer-policy") == 1, (name, names)
        for single in ("content-security-policy", "x-content-type-options", "cache-control"):
            assert names.count(single) <= 1, (name, single, names)


def test_opt_in_serves_hostile_html_verbatim_under_the_dashboard_sandbox_headers(world):
    _, _, _, _, root, host = world
    made, done, token = shared(world, flag=True)
    share_id = made["share"]["share_id"]
    assert done["summary"]["author_html"] is True
    assert tree(root / share_id / "files") == ["author-page.html", "preview.html"]
    state = json.loads(next((root / "by-token").glob("*/state.json")).read_text())
    assert state["rich"] == "author-page.html" and state["entry"] == "preview.html"
    status, headers, body = host.get(f"/s/{token}/f/author-page.html")
    assert status == 200 and body == HOSTILE
    for name, value in RICH_HEADERS.items():
        assert headers[name] == value, name
    assert headers["content-security-policy"] == RICH_HEADERS["content-security-policy"]
    assert "allow-" not in headers["content-security-policy"] and "frame-ancestors" not in headers["content-security-policy"]
    assert "x-frame-options" not in headers and "access-control-allow-origin" not in headers
    assert headers["x-robots-tag"] == "noindex"
    # the dashboard route keeps its framing controls
    assert "frame-ancestors 'self'" in dashboard.RICH_CSP and ("X-Frame-Options", "SAMEORIGIN") in dashboard.RICH_HEADERS
    assert headers["content-type"] == "text/html; charset=utf-8" and headers["content-length"] == str(len(HOSTILE))
    status, headers, body = host.get(f"/s/{token}/f/author-page.html", "HEAD")
    assert status == 200 and body == b"" and headers["content-security-policy"] == RICH_HEADERS["content-security-policy"]
    # the preview keeps its own policy and gains the toggle (hidden until its script runs)
    status, headers, preview = host.get(f"/s/{token}/f/preview.html")
    assert status == 200 and b"data-rich-view hidden" in preview and b"Show rich view" in preview
    assert headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert b"author-page.html" in preview and b"evil.example" not in preview


def test_revoked_and_expired_shares_answer_like_the_preview(world):
    base, _, _, _, _, host = world
    made, _, token = shared(world, flag=True)
    path = f"/s/{token}/f/author-page.html"
    assert host.get(path)[0] == 200
    host.clock[0] += 400 * 86400                   # past the 30 day public expiry
    assert host.get(path)[0] == host.get(f"/s/{token}/f/preview.html")[0] == 404
    host.clock[0] -= 400 * 86400
    assert host.get(path)[0] == 200
    assert request(base, f"{SHARES}/{made['share']['share_id']}/revoke", body={})[0] == 200
    assert host.get(path)[0] == host.get(f"/s/{token}/f/preview.html")[0] == 404


def test_another_shares_token_cannot_read_it(world):
    base, registry, _, blobs, _, host = world
    _, _, token_rich = shared(world, flag=True)
    _, _, token_plain = shared(world, flag=False, vid="v_plain")
    assert host.get(f"/s/{token_plain}/f/author-page.html")[0] == 404
    assert host.get(f"/s/{token_rich}/f/author-page.html")[0] == 200
    assert host.get(f"/s/{'Z' * 43}/f/author-page.html")[0] == 404


@pytest.mark.parametrize("assets, reason", [
    ((), "no single HTML"),
    (("two",), "no single HTML"),          # two HTML assets, neither the preview role
])
def test_no_single_html_asset_is_refused_at_create(world, assets, reason):
    base, registry, _, blobs, _, _ = world
    given = [] if not assets else [html_asset(blobs, b"<p>a", "a.html", "source"), html_asset(blobs, b"<p>b", "b.html", "data")]
    add_version(registry, blobs, *given)
    status, body = create(base, version="v_rich", include_author_html=True)
    assert status == 400 and reason in body["error"]


def test_the_preview_role_wins_among_several_pages_like_the_dashboard(world):
    base, registry, _, blobs, _, host = world
    one = html_asset(blobs, b"<p>a", "a.html", "source")
    two = html_asset(blobs, b"<p>b", "b.html", "preview")
    add_version(registry, blobs, one, two)
    status, made = create(base, version="v_rich", include_author_html=True)
    assert status == 201 and "b.html" in " ".join(made["summary"]["warnings"])
    token = token_of(approve(base, made["share"]["share_id"])[1]["url"])
    assert host.get(f"/s/{token}/f/author-page.html")[2] == b"<p>b"


def test_oversize_page_is_refused_declared_or_lying(world):
    base, registry, _, blobs, _, _ = world
    big = b"<p>" + b"a" * (rich.MAX_RICH_BYTES + 1)
    add_version(registry, blobs, html_asset(blobs, big))
    status, body = create(base, version="v_rich", include_author_html=True)
    assert status == 400 and "over" in body["error"]
    lying = html_asset(blobs, big)
    lying["size_bytes"] = 10
    add_version(registry, blobs, lying, vid="v_lie")
    status, body = create(base, version="v_lie", include_author_html=True)
    assert status == 400 and "over" in body["error"]


def test_a_corrupt_blob_is_refused(world):
    base, registry, _, blobs, _, _ = world
    asset = html_asset(blobs, b"<p>ok")
    asset["sha256"] = hashlib.sha256(b"other").hexdigest()
    blobs.objects[f"sha256/{asset['sha256'][:2]}/{asset['sha256'][2:4]}/{asset['sha256']}"] = b"<p>ok"
    add_version(registry, blobs, asset)
    status, body = create(base, version="v_rich", include_author_html=True)
    assert status == 400 and "verified" in body["error"]


def test_request_validation(world):
    base, registry, _, blobs, _, _ = world
    add_version(registry, blobs, html_asset(blobs, HOSTILE))
    assert create(base, version="v_rich", include_author_html="yes")[0] == 400
    assert create(base, version="v_rich", entry="preview.html+author-html")[0] == 400     # the mark is not user input
    assert create(base, version="v_rich", include_author_html=True, entry="x.html")[0] == 400
    status, body = create(base, version="v_html", include_author_html=True)               # not a report
    assert status == 400 and "report" in body["error"]
    assert create(base, version="v_rich", include_author_html=False)[0] == 201


def test_tampered_bundle_file_is_not_served(world):
    _, _, _, _, root, host = world
    made, _, token = shared(world, flag=True)
    target = root / made["share"]["share_id"] / "files" / "author-page.html"
    target.write_bytes(b"X" * len(HOSTILE))            # same size, other bytes
    assert host.get(f"/s/{token}/f/author-page.html")[0] == 404


@pytest.mark.parametrize("rich_value", ["evil.html", "preview.html", "../x", 3, ""])
def test_state_with_any_other_rich_name_fails_closed(world, rich_value):
    _, _, _, _, root, host = world
    _, _, token = shared(world, flag=True)
    state_path = next((root / "by-token").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    state["rich"] = rich_value
    state_path.write_text(json.dumps(state))
    assert host.get(f"/s/{token}/f/preview.html")[0] == 404
    assert host.get(f"/s/{token}/f/author-page.html")[0] == 404


def test_output_named_like_the_author_page_in_an_ordinary_share_is_not_special(world):
    """Only a state that says ``rich`` gets the sandbox route; nothing else changes meaning."""
    base, registry, _, blobs, _, host = world
    page = b"<!doctype html><title>x</title><p>mine</p>"
    blobs.add(page)
    registry.versions["v_named"] = {"version_id": "v_named", "artifact_id": AID, "state": "ready",
                                    "output_manifest": [output("index.html", page), output("author-page.html", page)]}
    status, made = create(base, version="v_named")
    assert status == 201
    token = token_of(approve(base, made["share"]["share_id"])[1]["url"])
    status, headers, _ = host.get(f"/s/{token}/f/author-page.html")
    assert status == 200 and headers["content-security-policy"].startswith("sandbox allow-scripts")


def test_member_created_links_cannot_include_the_author_page(world):
    base, registry, shares, blobs, root, _ = world
    from herdr_engineering.control_plane import artifact_share_api as api

    class Server:
        artifact_store = registry
        rustfs = blobs
        share_settings = {"max_files": 100, "max_share_bytes": 1 << 30, "max_total_bytes": 1 << 31}

    add_version(registry, blobs, html_asset(blobs, HOSTILE))
    with pytest.raises(api.ShareHTTPError) as err:
        api._plan(Server, AID, "v_rich", rich.PENDING_MARK, "member:someone")
    assert err.value.status == 400 and "member" in err.value.message


def test_only_an_opted_in_preview_lets_one_hashed_script_and_frames_run(world):
    import base64
    import re
    _, _, _, _, _, host = world
    _, _, token_on = shared(world, flag=True)
    _, _, token_off = shared(world, flag=False, vid="v_plain")
    on = host.get(f"/s/{token_on}/f/preview.html")[2].decode()
    off = host.get(f"/s/{token_off}/f/preview.html")[2].decode()
    meta = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)"', on).group(1)
    code = re.search(r"<script>(.*?)</script>", on, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in meta and "frame-src 'self'" in meta and "unsafe-inline'; script" not in meta
    assert on.count("<script") == 1 and "<script" not in off
    off_meta = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)"', off).group(1)
    assert "script-src" not in off_meta and "frame-src" not in off_meta


def test_no_preview_ever_gets_allow_same_origin(world):
    _, _, _, _, _, host = world
    _, _, token_on = shared(world, flag=True)
    _, _, token_off = shared(world, flag=False, vid="v_plain")
    on = host.get(f"/s/{token_on}/f/preview.html")[1]
    off = host.get(f"/s/{token_off}/f/preview.html")[1]
    for h in (on, off):
        assert h["content-security-policy"].startswith("sandbox allow-scripts allow-downloads;")
        assert "allow-same-origin" not in h["content-security-policy"]


def test_toggle_names_the_region_it_controls_and_has_a_visible_focus_style() -> None:
    from herdr_engineering.control_plane.share_rich_view import _PANEL

    assert 'aria-controls="rich-view-slot"' in _PANEL
    assert 'id="rich-view-slot" data-rich-slot' in _PANEL
    assert "[data-rich-view] button:focus-visible{outline:2px solid" in _PANEL
