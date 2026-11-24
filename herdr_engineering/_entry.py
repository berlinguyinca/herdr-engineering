"""Where the interpreter started, captured before anything can rewrite it.

`sys.path[0]` can be rewritten after start-up (older `herdr_engineering.fleet` inserted the checkout's
scripts directory), so it says nothing reliable about where the process began - reading it
at call time is how the first version of the shadowing guard (herdr-engineering#584) let bender's
real `python -m` invocation walk past it. Modules that must know whether the *working directory*
supplied their code import this one first.
"""
from __future__ import annotations

import sys

#: `sys.path[0]` as the interpreter started it: the sentinel `""` for `python -m` and `python -c`,
#: which means the current working directory, or an absolute path for other invocations.
PATH_ZERO = sys.path[0] if sys.path else ""
