"""Hostile, unterminated markup must not make the renderer's nesting scanner quadratic.

Real function, real input of the largest size the share server accepts; the bound is generous (a linear scan takes tens
of milliseconds; a quadratic one took minutes) so a loaded CI host does not flake. The control plane's mode-switch
scanner is covered by its own tests, not in this package.
"""

import time

import pytest

from herdr_artifact_host import share_render

SIZE = 2 * 1024 * 1024


@pytest.mark.parametrize("unit", ["</", "<!", "<?", "<!x", "<", "<!doctype", "</ "])
def test_bound_nesting_is_linear_without_a_closing_angle(unit):
    text = unit * (SIZE // len(unit))
    started = time.monotonic()
    share_render._bound_nesting(text)
    assert time.monotonic() - started < 3.0


def test_bound_nesting_keeps_working_after_the_last_angle():
    assert "<p>" in share_render._bound_nesting("<p>a</p><!x")
