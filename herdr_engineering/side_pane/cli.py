"""`herdr-eng side-pane`: the session side pane's commands."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from . import app
from .placement import DEFAULT_COMMAND, Outcome, ensure
from .style import clean

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
    again = ssub.add_parser("ensure", help="open the side pane of a tab, also after q closed it",
                            description="Open the side pane of TAB_ID unless it has one or the tab is too narrow for it. "
                                        "Clears the memory that q (or closing the pane) leaves, so a closed pane comes back.")
    again.add_argument("--tab", required=True, metavar="TAB_ID", help="the Herdr tab")
    again.add_argument("--pane", default=None, help="split this pane (default: the rightmost pane of the tab)")
    again.add_argument("--command", default=DEFAULT_COMMAND, help=argparse.SUPPRESS)
    again.set_defaults(func=_ensure)


def _ensure(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    """Open the tab's side pane, also after q closed it (the dismissal is cleared). 0 when the tab has one afterwards."""
    result = ensure(args.tab, args.pane, env=env, command=args.command, force=True)
    print(clean(f"side-pane {args.tab}: {result.outcome}" + (f" {result.pane_id}" if result.pane_id else "")), file=sys.stderr)
    return 0 if result.outcome in (Outcome.CREATED, Outcome.EXISTS) else 1
