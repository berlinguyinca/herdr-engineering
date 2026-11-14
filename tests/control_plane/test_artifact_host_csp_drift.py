"""Pins the artifact host's copied CSP constants to the control plane's originals while both exist.

This file is deleted in Task 13 together with the control-plane originals (artifact_report_render, artifact_rich_view).
"""

from herdr_artifact_host import rich_view as host
from herdr_engineering.control_plane import artifact_report_render, artifact_rich_view


def test_preview_csp_matches_the_report_renderer():
    assert host.PREVIEW_CSP == artifact_report_render._CSP


def test_rich_view_constants_match_the_dashboard():
    assert host.RICH_CSP == artifact_rich_view.RICH_CSP
    assert host.RICH_HEADERS == artifact_rich_view.RICH_HEADERS
    assert host.MAX_RICH_BYTES == artifact_rich_view.MAX_RICH_BYTES
