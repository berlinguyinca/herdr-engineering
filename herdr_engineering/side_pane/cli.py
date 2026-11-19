"""`herdr-eng side-pane`: the session side pane's commands."""
from __future__ import annotations

import argparse

from . import app

_DESCRIPTION = """The session side pane: a read-only viewer of the focused session of its own Herdr tab (Summary, Handoffs, CI).

Keys: 1, 2, 3 or Tab switch tabs; q closes the pane. Herdr has no collapse: q closes the pane and remembers that you
did, so it is not reopened until a new managed session appears in the tab. `herdr-eng side-pane ensure --tab TAB_ID`
opens it again and clears that memory."""


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("side-pane", help="session side pane: handoff chain, summary and CI of the focused session",
                       description=_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ssub = p.add_subparsers(dest="side_pane_command", required=True)
    run = ssub.add_parser("run", help="run the viewer in this pane (q closes it)")
    app.add_run_arguments(run)
    run.set_defaults(func=app.run_with)
