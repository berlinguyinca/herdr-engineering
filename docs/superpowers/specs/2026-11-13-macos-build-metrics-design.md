# Portable measured-build waiting

The maintenance goal authorizes fixing measured builds on macOS without changing their exit status, lock lifetime, signal forwarding, registry cancellation or resource accounting. Python 3.12 on this Mac has no `os.waitid`; its existing real-process exit and daemon tests fail with `AttributeError`.

## Approaches

Use the existing `waitid(WNOWAIT)` path on Linux and native `kqueue` process-exit notifications on macOS. This is the implementation choice: both observe exit without releasing the child's PID. Reaping with `waitpid` first would lose `wait4` accounting and permit late signals to hit a reused PID. A ctypes wrapper would duplicate platform ABI details unnecessarily.

Add `_wait_without_reaping(pid: int) -> None` in `build_metrics.py`. On macOS, register a one-shot process NOTE_EXIT event, close the queue reliably, retry interrupted waits, and report unexpected event errors. An ESRCH result for the owned unreaped child means it already exited; its status remains available to wait4. The caller still disables forwarding and stops registry cancellation before its existing `wait4` call. Do not change non-recorded execution or add a dependency.

Verify real already-exited and delayed children, exact exit status/rusage after observation, signal forwarding, registry cancellation and lock release. Exercise Python 3.12 on macOS and Linux in CI, with lint, typing, 80% coverage and wheel build. Real CLI E2E uses isolated state and a real subprocess. No mocks are added.

References: [Apple kevent](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/kevent.2.html), [Python 3.12 select](https://docs.python.org/3.12/library/select.html).
