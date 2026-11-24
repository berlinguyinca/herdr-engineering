"""Read-only session side pane: the handoff chain of a Herdr session, as pure data and text renderers.

The package exports what the handoff controller and the CLI call: `ensure` (one pane per tab), `is_side_pane` (the
marker the runner registers) and `run` (the viewer).
"""
from .app import run
from .placement import ensure, is_side_pane

__all__ = ["ensure", "is_side_pane", "run"]
