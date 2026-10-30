"""Share bundle rules: hostile names/bundles are refused, a verified frozen bundle is written, state is projected atomically."""
import hashlib
import json
import os
import stat

import pytest

from herdr_engineering.control_plane.share_bundle import (
    BundleLimits,
    ShareBundleError,
    build_bundle,
    content_type_for,
    normalise_name,
    remove_bundle,
    remove_state,
    scan_external_references,
    write_state,
)

LIMITS = BundleLimits(max_bytes=1024, max_files=5)
TOKEN_HASH = "ab" * 32


def _f(name, body=b"x", *, sha=None, size=None, chunks=None):
    return (name, sha or hashlib.sha256(body).hexdigest(), len(body) if size is None else size,
            chunks if chunks is not None else [body])


@pytest.mark.parametrize("bad", [
    "../x", "a/../x", "/abs", "a//b", "a/", "a\\b", "a\x00b", "a\nb", "x" * 300, "ok/" + "y" * 256, ".", "..", "a/./b",
    "", "%00".replace("%00", "\x7f"), "z" * 2000,
])
def test_normalise_rejects_escape_forms(bad):
    with pytest.raises(ShareBundleError):
        normalise_name(bad)


def test_normalise_keeps_nested_and_unicode_nfc():
    assert normalise_name("data/x.parquet") == "data/x.parquet"
    assert normalise_name("é.txt") == "é.txt"
    assert normalise_name("%2e%2e/x") == "%2e%2e/x"  # literal, never decoded here


def test_content_type_table():
    assert content_type_for("a/x.parquet") == "application/vnd.apache.parquet"
    assert content_type_for("m.wasm") == "application/wasm"
    assert content_type_for("i.SVG") == "image/svg+xml"
    assert content_type_for("d.pdf") == "application/pdf"
    assert content_type_for("f.woff2") == "font/woff2"
    assert content_type_for("i.html").startswith("text/html")
    assert content_type_for("a.exe") == "application/octet-stream"
    assert content_type_for("noext") == "application/octet-stream"


def test_build_bundle_writes_verified_files(tmp_path):
    dest = tmp_path / "s1"
    index = build_bundle(dest, [_f("index.html", b"<p>hi</p>"), _f("data/x.parquet", b"PAR1"), _f("empty.txt", b"")],
                         entry=None, limits=LIMITS)
    assert index.entry == "index.html" and index.total_bytes == 13
    assert (dest / "files" / "data" / "x.parquet").read_bytes() == b"PAR1"
    assert (dest / "files" / "empty.txt").read_bytes() == b""
    by = {f.name: f for f in index.files}
    assert by["data/x.parquet"].sha256 == hashlib.sha256(b"PAR1").hexdigest()
    assert by["data/x.parquet"].content_type == "application/vnd.apache.parquet"


@pytest.mark.parametrize("files, limits", [
    ([_f("index.html", sha="0" * 64)], LIMITS),
    ([_f("index.html", b"abc", size=2)], LIMITS),                      # claims short, body longer
    ([_f("index.html", b"abc", size=4)], LIMITS),                      # claims long, body shorter
    ([_f("index.html", b"x" * 2000)], LIMITS),                         # over max_bytes
    ([_f("a%d.txt" % i) for i in range(6)], LIMITS),                   # over max_files
    ([_f("A.html"), _f("a.html")], LIMITS),                            # case-fold duplicate
    ([_f("a.html"), _f("a.html")], LIMITS),                            # exact duplicate
    ([_f("é.txt"), _f("é.txt")], LIMITS),                   # unicode-equivalent duplicate
    ([_f("a"), _f("a/b")], LIMITS),                                    # file vs directory
    ([_f("../evil.html")], LIMITS),
    ([_f("index.html", b"xx", size=-1)], LIMITS),
    ([], LIMITS),
])
def test_build_bundle_refuses_hostile_input(tmp_path, files, limits):
    dest = tmp_path / "s1"
    with pytest.raises(ShareBundleError):
        build_bundle(dest, files, entry=None, limits=limits)
    assert not dest.exists()  # nothing partial is left behind
    assert not (tmp_path / "evil.html").exists()


def test_build_bundle_stream_overrun_stops_early(tmp_path):
    def endless():
        while True:
            yield b"x" * 100
    with pytest.raises(ShareBundleError):
        build_bundle(tmp_path / "s", [("a.html", "0" * 64, 10, endless())], entry=None, limits=LIMITS)
    assert not (tmp_path / "s").exists()


def test_build_bundle_refuses_existing_dest_and_leaves_it(tmp_path):
    dest = tmp_path / "s1"
    dest.mkdir()
    (dest / "keep").write_text("k")
    with pytest.raises(ShareBundleError):
        build_bundle(dest, [_f("index.html")], entry=None, limits=LIMITS)
    assert (dest / "keep").read_text() == "k"


def test_build_bundle_never_follows_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "s1"
    # a pre-planted symlinked dest is refused outright
    os.symlink(outside, dest)
    with pytest.raises(ShareBundleError):
        build_bundle(dest, [_f("index.html")], entry=None, limits=LIMITS)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("files, entry, want", [
    ([_f("index.html"), _f("b.html"), _f("c.pdf")], "b.html", "b.html"),
    ([_f("index.html"), _f("b.html")], None, "index.html"),
    ([_f("only.html"), _f("c.pdf"), _f("d.css")], None, "only.html"),
    ([_f("only.pdf"), _f("d.css")], None, "only.pdf"),
])
def test_entry_selection(tmp_path, files, entry, want):
    assert build_bundle(tmp_path / "s", files, entry=entry, limits=LIMITS).entry == want


def test_entry_errors_name_the_choices(tmp_path):
    with pytest.raises(ShareBundleError) as e:
        build_bundle(tmp_path / "s", [_f("a.html"), _f("b.html")], entry=None, limits=LIMITS)
    assert "a.html" in str(e.value) and "b.html" in str(e.value)
    assert not (tmp_path / "s").exists()
    with pytest.raises(ShareBundleError):
        build_bundle(tmp_path / "t", [_f("a.html")], entry="missing.html", limits=LIMITS)
    with pytest.raises(ShareBundleError):
        build_bundle(tmp_path / "u", [_f("a.csv")], entry=None, limits=LIMITS)


def test_entry_warnings_from_external_references(tmp_path):
    body = b'<script src="https://cdn.example/x.js"></script><img src="/abs.png">'
    index = build_bundle(tmp_path / "s", [_f("index.html", body)], entry=None, limits=LIMITS)
    assert any("https://cdn.example/x.js" in w for w in index.warnings)
    assert any("/abs.png" in w for w in index.warnings)


def test_scan_external_references():
    html = (b'<script src="https://cdn.example/a.js"></script>'
            b'<style>@import "https://f.example/c.css"; a{background:url(//x.example/i.png)}</style>'
            b"<script type=module>import 'https://esm.example/m.js'; import {y} from \"https://esm.example/y.js\";"
            b"</script><link rel=stylesheet href='http://h.example/s.css'>")
    found = scan_external_references(html)
    for want in ("https://cdn.example/a.js", "https://f.example/c.css", "//x.example/i.png",
                 "https://esm.example/m.js", "https://esm.example/y.js", "http://h.example/s.css"):
        assert want in found
    ok = (b'<script src="app.js"></script><img src="data:image/png;base64,AAAA">'
          b'<a href="#top">t</a><style>a{background:url(img/x.png)} b{background:url("data:image/svg+xml,x")}</style>'
          b"<script>import './m.js'; fetch('data/x.parquet')</script>")
    assert scan_external_references(ok) == []
    assert scan_external_references(b"\xff\xfe<script src='https://z.example/q.js'>") == ["https://z.example/q.js"]


def test_state_written_atomically_and_removed(tmp_path):
    write_state(tmp_path, TOKEN_HASH, {"share_id": "s1", "state": "live"})
    d = tmp_path / "by-token" / TOKEN_HASH
    assert json.loads((d / "state.json").read_text()) == {"share_id": "s1", "state": "live"}
    assert stat.S_IMODE((d / "state.json").stat().st_mode) == 0o644
    assert stat.S_IMODE(d.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "by-token").stat().st_mode) == 0o755
    write_state(tmp_path, TOKEN_HASH, {"share_id": "s1", "state": "revoked"})
    assert json.loads((d / "state.json").read_text())["state"] == "revoked"
    assert [p.name for p in d.iterdir()] == ["state.json"]  # no temp litter
    remove_state(tmp_path, TOKEN_HASH)
    assert not d.exists()
    remove_state(tmp_path, TOKEN_HASH)  # idempotent


@pytest.mark.parametrize("bad", ["../x", "", "AB" * 32, "ab" * 31, "a/b" + "0" * 61])
def test_state_rejects_bad_token_hash(tmp_path, bad):
    with pytest.raises(ShareBundleError):
        write_state(tmp_path, bad, {})
    with pytest.raises(ShareBundleError):
        remove_state(tmp_path, bad)


def test_remove_bundle(tmp_path):
    build_bundle(tmp_path / "share-1", [_f("index.html")], entry=None, limits=LIMITS)
    remove_bundle(tmp_path, "share-1")
    assert not (tmp_path / "share-1").exists()
    remove_bundle(tmp_path, "share-1")
    for bad in ("..", "a/b", "", ".", "by-token"):
        with pytest.raises(ShareBundleError):
            remove_bundle(tmp_path, bad)
    assert tmp_path.exists()
