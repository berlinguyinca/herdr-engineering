from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane.annotation_anchor import (
    ANNOTATION_ID_RE,
    AnnotationInputError,
    clean_anchor,
    clean_comment,
    parse_annotation_request,
    parse_delete_request,
    parse_edit_request,
)


def code(fn, *args, **kwargs) -> str:
    with pytest.raises(AnnotationInputError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def body(anchor: object, comment: object = "hi") -> bytes:
    return json.dumps({"anchor": anchor, "comment": comment}, ensure_ascii=False).encode()


def test_document_minimal_and_extras_stripped():
    assert clean_anchor({"kind": "document", "file": "a/b.html", "junk": 1}) == {
        "kind": "document",
        "file": "a/b.html",
    }


def test_text_quote_min_and_max():
    assert clean_anchor({"kind": "text_quote", "file": "a", "exact": "x"}) == {
        "kind": "text_quote",
        "file": "a",
        "exact": "x",
        "prefix": "",
        "suffix": "",
    }
    full = {
        "kind": "text_quote",
        "file": "a",
        "exact": "e" * 1000,
        "prefix": "p" * 64,
        "suffix": "s" * 64,
    }
    assert clean_anchor(full) == full
    assert code(clean_anchor, {**full, "exact": "e" * 1001}) == "too_long"
    assert code(clean_anchor, {**full, "prefix": "p" * 65}) == "too_long"
    assert code(clean_anchor, {**full, "suffix": "s" * 65}) == "too_long"
    assert code(clean_anchor, {"kind": "text_quote", "file": "a", "exact": ""}) == "bad_anchor"


def test_pdf_page_with_and_without_region():
    assert clean_anchor({"kind": "pdf_page", "file": "a.pdf", "page": 1}) == {
        "kind": "pdf_page",
        "file": "a.pdf",
        "page": 1,
    }
    region = {"x": 0.5, "y": 0, "w": 0.5, "h": 1, "z": 9}
    got = clean_anchor(
        {"kind": "pdf_page", "file": "a.pdf", "page": 100000, "exact": "q", "region": region}
    )
    assert got["region"] == {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}
    assert got["exact"] == "q" and got["page"] == 100000


@pytest.mark.parametrize("page", [0, 100001, True, 1.5, "1", None])
def test_page_bounds(page):
    assert code(clean_anchor, {"kind": "pdf_page", "file": "a", "page": page}) == "bad_anchor"


@pytest.mark.parametrize("bad", [1.0000001, float("nan"), float("inf"), -0.1, True, "0.1"])
def test_regions_refuse_bad_numbers(bad):
    img = {"kind": "image_region", "file": "a.png", "x": 0, "y": 0, "w": 0.5, "h": 0.5}
    assert code(clean_anchor, {**img, "w": bad}) == "bad_anchor"
    pdf = {
        "kind": "pdf_page",
        "file": "a.pdf",
        "page": 1,
        "region": {"x": 0, "y": 0, "w": 0.5, "h": 0.5},
    }
    assert code(clean_anchor, {**pdf, "region": {**pdf["region"], "h": bad}}) == "bad_anchor"


def test_sum_overflow_refused_both_kinds():
    assert (
        code(
            clean_anchor,
            {"kind": "image_region", "file": "a", "x": 0.5000001, "y": 0, "w": 0.5, "h": 1},
        )
        == "bad_anchor"
    )
    region = {"x": 0, "y": 0.5000001, "w": 1, "h": 0.5}
    assert (
        code(clean_anchor, {"kind": "pdf_page", "file": "a", "page": 1, "region": region})
        == "bad_anchor"
    )
    ok = clean_anchor(
        {"kind": "image_region", "file": "a", "x": 0.25, "y": 0.25, "w": 0.75, "h": 0.75}
    )
    assert ok["w"] == 0.75


@pytest.mark.parametrize("name", ["../a", "a//b", "/abs", "x" * 1025, "", 5, None])
def test_bad_file(name):
    assert code(clean_anchor, {"kind": "document", "file": name}) == "bad_file"


def test_bad_kind_and_shape():
    assert code(clean_anchor, {"kind": "nope", "file": "a"}) == "bad_anchor"
    assert code(clean_anchor, []) == "bad_anchor"


def test_comment_boundaries():
    assert clean_comment("a" * 4000) == "a" * 4000
    assert code(clean_comment, "a" * 4001) == "too_long"
    assert clean_comment("a" * 500, max_chars=500)
    assert code(clean_comment, "a" * 501, max_chars=500) == "too_long"
    assert clean_comment("a" * 4000, max_chars=99999) == "a" * 4000
    assert code(clean_comment, "a" * 4001, max_chars=99999) == "too_long"


def test_comment_chars():
    assert code(clean_comment, "  \n ") == "empty"
    for bad in ("\x00", "\x1b", "\ud800"):
        assert code(clean_comment, bad) == "bad_chars"
    assert clean_comment("a\n\tb") == "a\n\tb"
    assert code(clean_comment, 5) == "bad_anchor"


def test_hostile_comment_unchanged():
    hostile = "<img src=x onerror=1>"
    assert clean_comment(hostile) == hostile


def test_parse_request_ok_and_top_level_extras():
    anchor, comment = parse_annotation_request(
        body({"kind": "document", "file": "a", "z": 1}, " hi ")
    )
    assert anchor == {"kind": "document", "file": "a"} and comment == "hi"
    raw = json.dumps(
        {"anchor": {"kind": "document", "file": "a"}, "comment": "x", "extra": 1}
    ).encode()
    assert code(parse_annotation_request, raw) == "bad_anchor"
    assert code(parse_annotation_request, b'{"comment":"x"}') == "bad_anchor"


def test_body_shapes():
    assert code(parse_annotation_request, b"not json") == "bad_anchor"
    assert code(parse_annotation_request, b"[]") == "bad_anchor"
    assert code(parse_annotation_request, b"\xff\xfe") == "bad_anchor"
    assert code(parse_annotation_request, b"[" * 100000) == "too_long"
    assert code(parse_annotation_request, b"[" * 30000) == "bad_anchor"
    assert code(parse_annotation_request, b" " * 32769) == "too_long"
    assert code(parse_annotation_request, b" " * 32768) == "bad_anchor"
    assert code(parse_annotation_request, "x") == "bad_anchor"  # type: ignore[arg-type]


def test_max_realistic_body_accepted():
    raw = body({"kind": "text_quote", "file": "a", "exact": "e" * 1000}, "\U0001f600" * 4000)
    assert len(raw) <= 32768
    _, comment = parse_annotation_request(raw)
    assert len(comment) == 4000


def test_edit_and_delete():
    assert parse_edit_request(b'{"comment":" x "}') == "x"
    assert code(parse_edit_request, b'{"comment":"x","anchor":{}}') == "bad_anchor"
    assert code(parse_edit_request, b"{}") == "bad_anchor"
    assert code(parse_edit_request, b'{"comment":"a"}', max_chars=0) == "too_long"
    assert parse_delete_request(b"{}") is None
    assert code(parse_delete_request, b'{"x":1}') == "bad_anchor"


def test_id_regex_and_error_code():
    assert ANNOTATION_ID_RE.fullmatch("ann_" + "A" * 22)
    assert not ANNOTATION_ID_RE.fullmatch("ann_" + "A" * 21)
    with pytest.raises(ValueError):
        AnnotationInputError("nonsense")
