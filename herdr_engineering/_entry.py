"""Where the interpreter started, captured before anything can rewrite it.

`herdr_engineering.fleet` does `sys.path.insert(0, <checkout>/scripts)` at import time so it can
reach `fleet_inventory.py` beside the repository. By the time an updater runs, `sys.path[0]`
therefore names that scripts directory and says nothing about where the process began - reading it
at call time is how the first version of the shadowing guard (herdr-engineering#584) let bender's
real `python -m` invocation walk past it. Modules that must know whether the *working directory*
supplied their code import this one first.
"""
from __future__ import annotations

import sys

#: `sys.path[0]` as the interpreter started it: the sentinel `""` for `python -m` and `python -c`,
#: which means the current working directory, or an absolute path for other invocations.
PATH_ZERO = sys.path[0] if sys.path else ""
