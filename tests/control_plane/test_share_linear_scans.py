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
