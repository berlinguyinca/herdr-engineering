"""Hostile, unterminated markup must not make the share host's scanners quadratic (final review A-C1, A-I1).

Real functions, real inputs of the largest size the server accepts. The bounds are generous (a linear scan takes tens
of milliseconds; the quadratic one took minutes) so a loaded CI host does not flake.
"""
import time

import pytest

from herdr_engineering.control_plane import share_public, share_render

SIZE = share_public.MODE_SWITCH_MAX_BYTES
LINK = b"<a>x</a>"


@pytest.mark.parametrize("unit", [b"<!doctype", b"<!doctype ", b"<!doctype<!doctype x", b"<!DOCTYPE\n"])
def test_inject_mode_switch_is_linear_on_unterminated_doctype(unit):
    data = unit * (SIZE // len(unit))
    started = time.monotonic()
    assert share_public.inject_mode_switch(data, LINK) is None  # nothing safe to anchor on
    assert time.monotonic() - started < 3.0


def test_inject_mode_switch_still_anchors_after_a_terminated_doctype():
    assert share_public.inject_mode_switch(b"<!doctype html>hi", LINK) == b"<!doctype html>" + LINK + b"hi"


@pytest.mark.parametrize("unit", ["</", "<!", "<?", "<!x", "<", "<!doctype", "</ "])
def test_bound_nesting_is_linear_without_a_closing_angle(unit):
    text = unit * (SIZE // len(unit))
    started = time.monotonic()
    share_render._bound_nesting(text)
    assert time.monotonic() - started < 3.0


def test_bound_nesting_keeps_working_after_the_last_angle():
    assert "<p>" in share_render._bound_nesting("<p>a</p><!x")


# --- X2: every _SCAN alternative, unit*N and unit*N + ">" (final opus review) ---------------------------------------
UNITS = [b"<html ", b"<html '", b'<html "', b"<html a=", b"<html/", b"<html\n", b"<html", b"<body ", b"<body '", b"<body",
         b"<!--", b"<!-- ", b"<!-->", b"<script ", b"<script", b"<script>", b"<style ", b"<style>", b"<textarea ",
         b"<textarea>", b"<title ", b"<title>", b"<script></scrip", b"<!doctype ", b"<!doctype", b"<!DOCTYPE html",
         b"<html ><html ", b"<!doctype x <html ", b"<script <html ", b"<!-- <html ", b"<html <body "]


@pytest.mark.parametrize("unit", UNITS)
@pytest.mark.parametrize("tail", [b"", b">", b"</script>", b"-->"])
def test_every_scan_alternative_is_linear(unit, tail):
    data = unit * (SIZE // len(unit)) + tail
    started = time.monotonic()
    share_public.inject_mode_switch(data, LINK)
    assert time.monotonic() - started < 3.0, (unit, tail)


def test_repeated_html_openers_before_one_close_are_linear():
    data = b"<html " * (SIZE // 6) + b">"
    started = time.monotonic()
    result = share_public.inject_mode_switch(data, LINK)
    assert time.monotonic() - started < 3.0
    assert result == data + LINK    # the first html tag ends at the only '>'


@pytest.mark.parametrize("document, expected", [
    (b"<!doctype html><html lang=en><head><title>t</title></head><body class='a'><p>hi</p></body></html>",
     b"<!doctype html><html lang=en><head><title>t</title></head><body class='a'>" + LINK + b"<p>hi</p></body></html>"),
    (b"<html><head></head>text", b"<html>" + LINK + b"<head></head>text"),
    (b"<!doctype html>text", b"<!doctype html>" + LINK + b"text"),
    (b"<html a='>'><p>x</p>", b"<html a='>'>" + LINK + b"<p>x</p>"),
    (b"<!-- <body> --><script>var s='<body>';</script><html><body>x", b"<!-- <body> --><script>var s='<body>';</script><html><body>" + LINK + b"x"),
    (b"<html><html><p>x", b"<html>" + LINK + b"<html><p>x"),
    (b"<html <html x><p>", b"<html <html x>" + LINK + b"<p>"),
])
def test_injection_still_lands_in_the_right_place(document, expected):
    assert share_public.inject_mode_switch(document, LINK) == expected


def test_no_anchor_means_no_injection():
    assert share_public.inject_mode_switch(b"just text", LINK) is None
    assert share_public.inject_mode_switch(b"<html unterminated", LINK) is None
