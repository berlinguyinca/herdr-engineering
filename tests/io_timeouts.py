"""One place for the socket and subprocess timeouts of tests that use a local server or spawn a CLI (see tests/test_io_timeouts.py).

HERDR_TEST_IO_TIMEOUT (seconds, default 30) is the wait for one local HTTP answer; subprocess waits scale from it (default 300 s).
A timeout here only has to catch a hang: pytest-timeout and the run watchdog bound the whole test, so a generous value costs nothing
on a healthy host and keeps a starved one from failing tests that did nothing wrong."""
from __future__ import annotations

import os


def _positive_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a positive number of seconds, got {raw!r}") from None
    if not value > 0:
        raise SystemExit(f"{name} must be a positive number of seconds, got {raw!r}")
    return value


HTTP_TIMEOUT: float = _positive_seconds("HERDR_TEST_IO_TIMEOUT", 30.0)
PROCESS_TIMEOUT: float = HTTP_TIMEOUT * 10
