"""The "Shared by" labelling gate, ported from the control plane: the pure helpers (fuzz corpus, strict gate,
html5ever differential) and the decoy / unlabelable pages through the real host. Nothing here is mocked."""

from __future__ import annotations

import random

import nh3
import pytest

from herdr_artifact_host import share_http
from herdr_artifact_host.share_http import MODE_SWITCH_MAX_BYTES
from tests.artifact_host.seed import Host

BAR_MARK = b"data-herdr-shared-by"


def f(name: str) -> str:
    return "/f/" + name


DECOYS = {
    "attribute": b'<div title="<body>">x</div><body><p>real</p></body>',
    "template": b"<template><body></template><p>real</p>",
    "noscript": b"<noscript><body></noscript><p>real</p>",
    "xmp": b"<xmp><body></xmp><p>real</p>",
    "iframe": b"<iframe><body></iframe><p>real</p>",
    "select": b"<select><body></select><p>real</p>",
    "cdata": b"<svg><![CDATA[<body>]]></svg><p>real</p>",
    "pi": b"<?pi <body> ?><p>real</p>",
    "double-escaped-script": b"<script><!--<script></script><body>--></script><p>real</p>",
    "plaintext": b"<plaintext><body><p>real</p>",
    "frameset": b"<html><frameset><body></frameset>",
    "svg-open": b"<svg><body></svg><p>real</p>",
    "math-open": b"<math><body></math><p>real</p>",
    # round 2: tokenizer disagreements the scanner and Python's HTMLParser share (html5ever hides the bar in a template)
    "abrupt-comment-dash": b"<!doctype html><!---><template><!--><body></template><h1>visible page</h1>",
    "abrupt-comment": b"<!doctype html><!--><template><!--><body></template><h1>visible page</h1>",
    "abrupt-comment-textarea": b"<!doctype html><!---><textarea><!--><body></textarea><h1>visible page</h1>",
    "cdata-html": b"<!doctype html><html><![CDATA[ x><template><body\n]]></template><h1>visible page</h1>",
    "bogus-end-tag": b"<!doctype html></ x><template><body></template><h1>visible page</h1>",
    "bang-comment-close": b"<!doctype html><!-- x --!><template><body></template><h1>visible page</h1>",
    "svg-desc-html": b"<h1>visible page</h1><svg><desc><p></svg><body>",
    "svg-foreignobject-html": b"<svg><foreignObject><p></svg><body><h1>x</h1>",
    "math-annotation-html": b'<h1>visible page</h1><math><annotation-xml encoding="text/html"><p></math><body>',
    "math-mtext-html": b"<math><mtext><b></math><body>",
    "frameset-then-body": b"<frameset></frameset><body>x",
    "frameset-frame": b'<html><head></head><frameset cols="*"><frame src=a></frameset><body>',
    "form-dialog": b"<form><dialog></form><body></dialog><h1>visible</h1>",
    "form-details": b"<form><details></form><body></details><h1>visible</h1>",
    "form-object": b"<form><object data=x></form><body></object><h1>visible</h1>",
    "div-hidden": b"<div hidden><body><h1>visible</h1>",
    "text-before-body": b"hello<body><h1>visible</h1>",
    "dashdash-in-comment": b"<!doctype html><!-- a -- b --><template><body></template><h1>visible page</h1>",
}


def _page(host: Host, data: bytes, **kw):
    s = host.seed_share({"index.html": data}, **kw)
    return s, host.get(f"/s/{s.token}/f/index.html")


@pytest.mark.parametrize("decoy", sorted(DECOYS))
def test_decoys_never_give_a_200_without_an_effective_bar(host: Host, decoy: str) -> None:
    _, r = _page(host, DECOYS[decoy])
    assert r.status == 404 or (r.status == 200 and r.body.count(BAR_MARK) == 1), (
        r.status,
        r.body[:200],
    )
    assert r.status == 404


def test_uppercase_tags_without_attributes_and_an_empty_file(host: Host) -> None:
    files = {
        "index.html": b"<!DOCTYPE HTML><HTML><HEAD><TITLE>t</TITLE></HEAD><BODY><P>x</P></BODY></HTML>",
        "empty.html": b"",
    }
    s = host.seed_share(files)
    body = host.get(f"/s/{s.token}/f/index.html").body
    assert body.count(BAR_MARK) == 1 and body.index(b"<BODY>") < body.index(BAR_MARK) < body.index(
        b"<P>"
    )
    assert host.get(f"/s/{s.token}/f/empty.html").body.count(BAR_MARK) == 1


@pytest.mark.parametrize(
    "name,data",
    [
        ("utf16", "<html><body>hi</body></html>".encode("utf-16")),
        ("utf32", "<html><body>hi</body></html>".encode("utf-32")),
        ("nul", b"\x00<html><body>hi</body></html>"),
        ("over-cap", b"<html><body>" + b"x" * (MODE_SWITCH_MAX_BYTES + 1) + b"</body></html>"),
    ],
)
def test_unlabelable_html_is_a_404_not_an_unlabelled_page(
    host: Host, name: str, data: bytes
) -> None:
    s = host.seed_share({"index.html": data})
    assert host.get(f"/s/{s.token}/f/index.html").status == 404


FUZZ_PIECES = [
    b"<html>",
    b"<head>",
    b"</head>",
    b"<body>",
    b"<BODY a='1'>",
    b"</body>",
    b"<!--",
    b"-->",
    b"<script>",
    b"</script>",
    b"<style>",
    b"</style>",
    b"<template>",
    b"</template>",
    b"<noscript>",
    b"<xmp>",
    b"<iframe>",
    b"<select>",
    b"<svg>",
    b"<![CDATA[",
    b"]]>",
    b'<div title="',
    b'">',
    b"'",
    b'"',
    b"<textarea>",
    b"</textarea>",
    b"<title>",
    b"</title>",
    b"<p>",
    b"text",
    b"<?",
    b"?>",
    b"<!doctype html>",
    b"<plaintext>",
    b"<math>",
    b"<object>",
    b"<frameset>",
    b"<",
    b">",
    b"&lt;body&gt;",
    b"<!--<script>",
]


def test_fuzz_corpus_accepted_pages_have_exactly_one_effective_bar():

    import nh3

    bar = share_http.shared_by_bar("Al")
    rng = random.Random(8)
    accepted = rejected = 0
    for _ in range(4000):
        data = b"".join(rng.choice(FUZZ_PIECES) for _ in range(rng.randint(0, 9)))
        out = share_http.labelled_html(data, "text/html; charset=utf-8", bar)
        if out is None:
            rejected += 1
            continue
        accepted += 1
        assert out.count(b"data-herdr-shared-by") == 1
        # independent oracle: html5ever (nh3) must still see the bar as an element of the cleaned document
        cleaned = nh3.clean(
            out.decode(),
            tags={"div", "html", "body", "head", "p"},
            attributes={"div": {"data-herdr-shared-by"}},
            strip_comments=True,
        )
        assert cleaned.count("data-herdr-shared-by") == 1, out
    assert accepted > 200 and rejected > 200


# ---------------------------------------------------------------------------- the strict gate, class by class

GATE_REFUSED = {
    "cdata": b"<html><![CDATA[ x ]]><body><p>x</p>",
    "abrupt-1": b"<!--><body><p>x</p>",
    "abrupt-2": b"<!---><body><p>x</p>",
    "bang-close": b"<!-- x --!><body><p>x</p>",
    "pi": b"<?php x ?><body><p>x</p>",
    "bogus-comment": b"<!x><body><p>x</p>",
    "bad-end-tag": b"</ x><body><p>x</p>",
    "end-tag-digit": b"</1><body><p>x</p>",
    "double-dash-comment": b"<!-- a -- b --><body><p>x</p>",
    "triple-dash-close": b"<!-- a ---><body><p>x</p>",
    "unterminated-comment": b"<body><p>x</p><!-- never closed",
    "nul": b"<body><p>"
    + b"x" * 2000
    + b"\x00</p>",  # past the first 1 KiB the encoding check does not look
    "comment-hides-tag": b"<template><!-- </template> --><body><p>x</p>",
    "script-comment": b"<script><!-- </script><body><p>x</p>",
    "commented-closer": b"<script><!-- </script> --><body><p>x</p>",
    "balanced-svg-before-anchor": b"<svg></svg><body><p>x</p>",
    "balanced-math-before-anchor": b"<math></math><body><p>x</p>",
    "balanced-template-before-anchor": b"<template></template><body><p>x</p>",
    "balanced-select-before-anchor": b"<select></select><body><p>x</p>",
    "frameset-after-body": b"<body><p>x</p><frameset></frameset>",
    "frame-alone": b"<body><frame src=a>",
    "form-dialog": b"<form><dialog></form><body></dialog><h1>x</h1>",
    "form-details": b"<form><details></form><body></details><h1>x</h1>",
    "form-object": b"<form><object data=x></form><body></object><h1>x</h1>",
    "unknown-element": b"<custom-el><body><p>x</p>",
    "div-before-body": b"<div></div><body><p>x</p>",
    "text-before-html": b"hi<html><body><p>x</p>",
    "bom-prefix": b"\xef\xbb\xbf<html><body><p>x</p>",
    "two-doctypes": b"<!doctype html><!doctype html><body><p>x</p>",
    "doctype-after-html": b"<html><!doctype html><body><p>x</p>",
    "script-tag-not-closer": b"<script></script foo><body><p>x</p>",
    "angle-in-attr": b'<meta content="<b>"><body><p>x</p>',
    "noscript-with-div": b"<head><noscript><div></div></noscript></head><body><p>x</p>",
    "selfclosing-html": b"<html/><body><p>x</p>",
    "stray-head-end": b"</head><body><p>x</p>",
    "meta-after-head": b"<head></head><meta><body><p>x</p>",
    "unclosed-template": b"<template><body><p>x</p>",
    "unclosed-xmp": b"<xmp><body><p>x</p>",
    "unclosed-iframe": b"<iframe><body><p>x</p>",
    "unclosed-noembed": b"<noembed><body><p>x</p>",
    "unclosed-noframes": b"<noframes><body><p>x</p>",
    "unclosed-noscript": b"<noscript><body><p>x</p>",
    "plaintext": b"<plaintext></plaintext><body><p>x</p>",
    "unclosed-select": b"<select><body><p>x</p>",
    "unclosed-svg": b"<svg><body><p>x</p>",
    "unclosed-math": b"<math><body><p>x</p>",
    "attr-tag": b'<a title="<template>"><body><p>x</p>',
}
GATE_ACCEPTED = {
    "plain": b"<!doctype html><html><head><title>t</title></head><body><p>x</p></body></html>",
    "comment": b"<!doctype html><!-- fine --><html><body><!-- also fine --><p>x</p>",
    "style-script": b"<head><style>p{}</style><script>var a = 1 < 2;</script></head><body>",
    "commented-pair": b"<head><!-- <script src=a.js></script> --></head><body><p>x</p>",
    "svg-after-anchor": b"<body><svg><desc><p></svg><math><mtext><b></math><template></template><select></select>",
    "commented-opener": b"<!-- <template> --><body><p>x</p>",
    "full-head": (
        b"<!DOCTYPE html>\n<!-- c --><html lang=en><head><meta charset=utf-8><base href=/><link rel=x href=y>"
        b"<title>a <b> title</title><style>p{}</style><script src=a.js></script>"
        b"<noscript><style>p{}</style><meta name=a></noscript></head>\n<body class=a>"
    ),
    "selfclosing-script-swallows-anchor": b"<script/><body><p>x</p>",  # no anchor is found, so the bar goes first
    "double-body": b"<body><body>",
    "html-only": b"<html lang=en><p>x</p>",
    "doctype-only": b"<!doctype html><p>x</p>",
    "framework-word": b"<body><p>a frame of reference, framed</p>",
    "closed-noscript": b"<head><noscript><style>p{}</style></noscript></head><body>",
    "raw-after-anchor": b"<body><template><body><textarea>",
    # an unclosed raw-text element swallows the would-be anchor, so the bar goes first (offset 0) and is top-level
    "unclosed-script-no-anchor": b"<script><body><p>x</p>",
    "unclosed-textarea-no-anchor": b"<textarea><body><p>x</p>",
    "no-anchor": b"<p>just a fragment</p>",
    "empty": b"",
}


@pytest.mark.parametrize("name", sorted(GATE_REFUSED))
def test_the_gate_refuses_every_ambiguous_construct(name):
    data = GATE_REFUSED[name]
    cut = share_http._anchor_index(data) or 0
    # the gate alone, not the HTMLParser check behind it: each refusal class is pinned by its own page
    assert not (share_http._well_formed_markup(data) and share_http._prefix_allowed(data[:cut]))
    assert share_http.labelled_html(data, "text/html", share_http.shared_by_bar("Al")) is None


@pytest.mark.parametrize("name", sorted(GATE_ACCEPTED))
def test_the_gate_still_labels_ordinary_pages(name):
    out = share_http.labelled_html(GATE_ACCEPTED[name], "text/html", share_http.shared_by_bar("Al"))
    assert out is not None and out.count(b"data-herdr-shared-by") == 1


GRAMMAR = [
    b"<meta charset=utf-8>",
    b"<link rel=a href=b>",
    b"<base href=/>",
    b"<div>",
    b"<span hidden>",
    b"\n",
    b"<!doctype html>",
    b"<html>",
    b"<head>",
    b"</head>",
    b"<body>",
    b"<body class=a>",
    b"</body>",
    b"</html>",
    b"<p>",
    b"</p>",
    b"text",
    b"<h1>x</h1>",
    b"<title>t</title>",
    b"<style>p{}</style>",
    b"<script>1</script>",
    b"<!-- ok -->",
    b"<!--",
    b"-->",
    b"<!-->",
    b"<!--->",
    b"--!>",
    b"<!-- a -- b -->",
    b"<![CDATA[",
    b"]]>",
    b"<?pi ",
    b"?>",
    b"<!x>",
    b"</ x>",
    b"</>",
    b"<template>",
    b"</template>",
    b"<textarea>",
    b"</textarea>",
    b"<title>",
    b"</title>",
    b"<xmp>",
    b"</xmp>",
    b"<iframe>",
    b"</iframe>",
    b"<noembed>",
    b"<noframes>",
    b"<noscript>",
    b"</noscript>",
    b"<plaintext>",
    b"<select>",
    b"</select>",
    b"<svg>",
    b"</svg>",
    b"<math>",
    b"</math>",
    b"<form>",
    b"</form>",
    b"<dialog>",
    b"</dialog>",
    b"<details>",
    b"</details>",
    b"<object data=x>",
    b"</object>",
    b"<div hidden>",
    b"</div>",
    b"<desc>",
    b"<foreignObject>",
    b"<mtext>",
    b'<annotation-xml encoding="text/html">',
    b"</frameset>",
    b"<frame src=a>",
    b"<script>",
    b"</script>",
    b"<style>",
    b"</style>",
    b'<a title="',
    b'">',
    b"'",
    b'"',
    b"<",
    b">",
    b"\n",
    b"<frameset>",
    b'<frameset cols="*">',
    b"<object>",
    b"<!doctype html",
    b"&lt;",
]
BYPASSES = [
    b"<!doctype html><!---><template><!--><body></template><h1>visible page</h1>",
    b"<!doctype html><!--><template><!--><body></template><h1>visible page</h1>",
    b"<!doctype html><html><![CDATA[ x><template><body\n]]></template><h1>visible page</h1>",
]
_OUT_TAG = __import__("re").compile(r"<(/?)([a-z0-9]+)([^>]*)>")


def _oracle_bar_is_top_level(page: str) -> bool:
    """html5ever (nh3) view: exactly one bar element, and no element is open around it."""
    tags = {
        "html",
        "head",
        "body",
        "div",
        "p",
        "span",
        "title",
        "template",
        "b",
        "i",
        "a",
        "h1",
        "table",
        "tr",
        "td",
        "ul",
        "li",
        "svg",
        "math",
        "section",
        "form",
        "select",
        "option",
        "textarea",
        "style",
        "script",
        "pre",
    }
    cleaned = nh3.clean(
        page,
        tags=tags,
        attributes={"div": {"data-herdr-shared-by"}},
        strip_comments=True,
        clean_content_tags=set(),
    )
    if cleaned.count("data-herdr-shared-by") != 1:
        return False
    stack: list[str] = []
    pos = 0
    while (m := _OUT_TAG.search(cleaned, pos)) is not None:
        pos = m.end()
        closing, name, attrs = m.groups()
        if not closing and name in ("script", "style", "title", "textarea"):
            stack.append(
                name
            )  # raw text / RCDATA: whatever looks like a tag up to the closer is text
            pos = cleaned.find("</" + name, pos)
            if pos < 0:
                return False
            continue
        if "data-herdr-shared-by" in attrs:
            return not stack
        if closing:
            if name in stack:
                while stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return False


def test_differential_a_served_bar_is_a_real_top_level_element_for_html5ever():
    bar = share_http.shared_by_bar("Al")
    rng = random.Random(20261104)
    pages = list(BYPASSES) + [
        b"".join(rng.choice(GRAMMAR) for _ in range(rng.randint(0, 10))) for _ in range(6000)
    ]
    accepted = rejected = 0
    for data in pages:
        out = share_http.labelled_html(data, "text/html; charset=utf-8", bar)
        if out is None:
            rejected += 1
            continue
        accepted += 1
        assert _oracle_bar_is_top_level(out.decode()), (data, out)
    assert accepted > 300 and rejected > 3000, (accepted, rejected)
    # head-shaped prefixes followed by a body/html anchor and a random tail: the allowlist's own territory
    head = [
        b"<!doctype html>",
        b"<html>",
        b"<head>",
        b"</head>",
        b"<meta charset=a>",
        b"<title>t</title>",
        b"<style>p{}</style>",
        b"<script>1</script>",
        b"<noscript><style>a{}</style></noscript>",
        b"<!-- c -->",
        b"\n",
        b" ",
        b"<link rel=a>",
        b"<base href=/>",
        b"<div>",
        b"<form>",
        b"<dialog>",
        b"x",
        b"<template>",
        b"</form>",
        b"<svg>",
        b"<p>",
        b"<script>",
        b"<title>",
        b"<noscript>",
    ]
    shaped = accepted_shaped = 0
    for _ in range(4000):
        data = (
            b"".join(rng.choice(head) for _ in range(rng.randint(0, 8)))
            + rng.choice([b"<body>", b"<BODY a=1>", b"<html>"])
            + b"".join(rng.choice(GRAMMAR) for _ in range(rng.randint(0, 6)))
        )
        out = share_http.labelled_html(data, "text/html; charset=utf-8", bar)
        shaped += 1
        if out is not None:
            accepted_shaped += 1
            assert _oracle_bar_is_top_level(out.decode()), (data, out)
    assert accepted_shaped > 500, accepted_shaped
    for data in BYPASSES:
        assert share_http.labelled_html(data, "text/html", bar) is None
