"""Prints where a unit-gate pass is while it runs, and what it was inside when it overran its budget (issue #937).

Loaded by name with `-p pytest_ci_progress` from `scripts/ci/pytest-plugins` (see `scripts/ci/pytest-parallel.sh`:
`herdr_pytest_report_load`), the same way `pytest_file_times` is loaded.

Why this exists rather than more `--durations`: a CI step that gets stopped by the platform loses whatever the run had not printed yet.
Pipeline 1978's shard 3 was `killed` at 1950 s and the log named nothing it was inside; pipeline 1754's single-pass gate ran 6829 s and
its tail was truncated. `--durations=40` and `pytest_file_times` both report at teardown, so in both cases they reported nothing at all.

What it does, in the controller process only:

* every `HERDR_PYTEST_PROGRESS_EVERY_SECONDS` (default 60) and every `HERDR_PYTEST_PROGRESS_EVERY_TESTS` (default 100, 0 = time only),
  one line prefixed `[ci-progress]` plus one JSON record in `$HERDR_PYTEST_PROGRESS_DIR/progress.jsonl`: shard, elapsed, counts by
  outcome, and per worker the test id it is inside and for how long;
* past `HERDR_PYTEST_PROGRESS_BUDGET_SECONDS` (0/unset = no budget) it repeats a `[ci-budget]` dump - the same per-worker view, once per
  interval - and keeps going. It stops nothing: `scripts/ci/unit-gates.sh` deliberately has no flat cap ("a slow but live run is never
  killed") and `scripts/ci/run-watched.py` owns stopping wedged ones. The budget exists so that when the platform stops the step anyway,
  the thing it was doing is already in the log.

The lines go to a duplicate of descriptor 1 taken when the plugin loads, so pytest's own capture cannot hold them back until teardown -
which is the whole point of a live progress line. Only reads reports and log events: it changes no result.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Descriptor 1 as it was before pytest redirected it. Writes here reach the CI step log as they happen.
_REAL_OUT = os.dup(1)

SHARD = os.environ.get("HERDR_UNIT_SHARD", "") or ""


def _number(name: str, default: float, cast: Callable[[str], Any]) -> Any:
    """Read one of the plugin's numeric overrides, falling back on anything unparseable.

    A progress tool that breaks the gate is worse than no progress tool: a typo in CI must cost a line format, not a red pipeline.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        sys.stderr.write(f"pytest_ci_progress: ignoring {name}={raw!r} (not a number)\n")
        return default


# 0.05 s floor: a smaller interval than that is a test wanting a line per report, and `EVERY_TESTS=1` says that outright.
EVERY_SECONDS = max(_number("HERDR_PYTEST_PROGRESS_EVERY_SECONDS", 60.0, float), 0.05)
EVERY_TESTS = max(_number("HERDR_PYTEST_PROGRESS_EVERY_TESTS", 100, int), 0)
BUDGET_SECONDS = max(_number("HERDR_PYTEST_PROGRESS_BUDGET_SECONDS", 0.0, float), 0.0)
SLOWEST_TOP = max(_number("HERDR_PYTEST_PROGRESS_SLOWEST_TOP", 3, int), 0)
_progress_dir = (os.environ.get("HERDR_PYTEST_PROGRESS_DIR") or "").strip()
PROGRESS_FILE = Path(_progress_dir) / "progress.jsonl" if _progress_dir else None

_SESSION = f"{os.getpid()}-{time.time():.6f}"
# The role and worker name are decided in `pytest_configure` from `config.workerinput`, not from `PYTEST_XDIST_WORKER`: the unit gate runs
# its own pytest under xdist, and any pytest it starts in a subprocess (a test of a CI helper, for instance) inherits that variable and
# would otherwise believe it was a worker - a controller that thinks it is a worker emits nothing, which is the bug this plugin exists to
# fix. The variable is used only as a name hint until `workerinput` proves it.
_ROLE = "main"
_WORKER_ID = "main"
_CONFIG: Any = None
_uses_xdist: bool | None = None
_pass_id: str | None = None
_cleaned = False


# --- worker side: publish what this worker is inside, so the controller can read it while it runs ------------------------------
#
# xdist forwards a report to the controller when a test *finishes*; there is no forwarded "started" event. So a worker writes the id of
# the test it started, and the controller reads those files. Workers live on the same host as the controller, so this is a local file
# under the pass's own directory; one small write per test start, and a test that never finishes is the case whose file never changes -
# which is the one that matters.


def _dsession_uid() -> str | None:
    """The xdist test-run id, as the controller knows it (workers get it in `PYTEST_XDIST_TESTRUNUID`).

    Resolved lazily: xdist registers its session plugin in its own `pytest_configure` (`trylast`), and the id is only put on the node
    manager in `pytest_sessionstart`, so it is knowable at the first emit and not at this plugin's configure. The `--testrunuid` option
    comes first because that is an id the operator pinned. If xdist's internals move, this returns None and the pass uses a process-local
    id: the record still names what each worker was inside, it just cannot be told apart from another pass sharing the directory.
    """
    if _CONFIG is None:
        return None
    try:
        pinned = _CONFIG.getoption("testrunuid")
    except (ValueError, AttributeError):
        pinned = None
    if pinned:
        return str(pinned)
    manager = getattr(_CONFIG, "pluginmanager", None)
    dsession = manager.getplugin("dsession") if manager is not None else None
    if dsession is None:
        return None
    for holder in (
        dsession,
        getattr(dsession, "nodemanager", None),
        getattr(dsession, "log", None),
    ):
        uid = getattr(holder, "testrunuid", None) or getattr(holder, "_testrunuid", None)
        if uid:
            return str(uid)
    return None


def _resolve_pass_id() -> str | None:
    """This pass's id, or None when it is an xdist pass whose id is not knowable yet.

    A worker is told the id (`PYTEST_XDIST_TESTRUNUID`, set from `workerinput`). The controller only learns it once xdist has created its
    session plugin, which can be after this plugin's `pytest_configure` - xdist starts workers early. Guessing a process-local id in the
    meantime would split the pass in two: the controller cleaning and reading one directory while its workers write another, which is how
    `workers=0` gets reported for a pass that plainly has workers.
    """
    uid = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    if uid:
        return uid
    uid = _dsession_uid()
    if uid:
        return uid
    if _is_serial_pass():
        return f"local-{os.getpid()}"
    return None


def _is_serial_pass() -> bool:
    """Whether this pass runs tests in its own process (no xdist workers).

    Decided once, and remembered: the answer is needed again during `pytest_unconfigure`, when probing the config has become unreliable,
    and a controller that briefly looked serial would add a `main` worker beside its own gw entries and report more workers than `-n`
    asked for.
    """
    global _uses_xdist
    if _uses_xdist is True:
        return False
    if _CONFIG is None:
        return True
    try:
        if _CONFIG.getoption("numprocesses"):
            _uses_xdist = True
            return False
    except (ValueError, AttributeError):
        pass
    manager = getattr(_CONFIG, "pluginmanager", None)
    if manager is not None and manager.getplugin("dsession") is not None:
        _uses_xdist = True
        return False
    return True


def pass_id() -> str:
    """This pass's id, or `""` while it is not resolved yet."""
    return _pass_id or ""


def state_dir() -> Path | None:
    """The directory this pass's worker state files live in, or None while the pass id is unresolved.

    One id per pass keeps concurrent passes apart: the unit gate runs its fast pass and its slow tail at the same time in one progress
    directory (`herdr_pytest_run_gate` in `scripts/ci/pytest-rerun.sh`), and with one flat directory each would read the other's workers
    as its own - and the controller's cleanup would delete the other's evidence mid-run.
    """
    global _pass_id
    if _pass_id is None:
        resolved = _resolve_pass_id()
        if resolved is None:
            return None
        _pass_id = resolved
    return Path(_progress_dir) / _pass_id


def _clean_state_dir() -> None:
    """Drop state files that belong to an earlier pass, once, as soon as this pass's id is knowable.

    The cleanup cannot simply empty the directory. For an xdist pass the id only becomes knowable after xdist has created its session, by
    which time workers have already written the file for the test they are inside - a blanket delete erases the live view, and because a
    worker writes again only when it *starts* its next test, `running=` stays empty for the whole pass. So a file is removed only when it
    cannot belong to this pass: written before this process started, or tagged as another pass's.
    """
    global _cleaned
    directory = state_dir()
    if directory is None or _cleaned or not _progress_dir:
        return
    _cleaned = True
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for path in directory.glob("worker-*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if raw is None or raw.get("pass") != pass_id() or raw.get("written", 0) < _start_wall:
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _worker_state_path() -> Path | None:
    directory = state_dir()
    return None if directory is None else directory / f"worker-{_WORKER_ID}.json"


def _write_worker_state(current: str | None, started: float | None) -> None:
    if not _progress_dir:
        return
    state = {
        "session": _SESSION,
        "pass": pass_id() or None,
        "worker": _WORKER_ID,
        "current": current,
        "started": started,
        "written": time.time(),
    }
    target = _worker_state_path()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass  # progress must never cost a run


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    """Record what this process started, so a quiet worker is still visible.

    `pytest_runtest_logreport` only arrives when a test finishes and xdist forwards no start events, so without this the moment a budget
    overrun is about - the test that has been running for four minutes - is invisible.

    A controller driving workers is excluded: `DSession` calls `pytest_runtest_logstart` in the controller as it forwards a worker's
    report, so recording it would add a `main` entry beside the worker that actually ran the test and report more workers than were asked
    for.
    """
    global _serial_current
    if _ROLE != "worker" and not _is_serial_pass():
        return
    _serial_current = (nodeid, time.time())
    _write_worker_state(nodeid, time.time())


def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    """Clear this process's "currently running" marker.

    A controller driving workers never had a marker to clear (see `pytest_runtest_logstart`), so it writes nothing: a `worker-main.json`
    left in the pass's directory reads as an extra worker.
    """
    if _ROLE != "worker" and not _is_serial_pass():
        return
    _serial_current = (None, None)
    _write_worker_state(None, None)


# --- controller side ---------------------------------------------------------------------------------------

_LOCK = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_start_mono = time.monotonic()
_start_wall = time.time()
_counts = {"done": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0}
_per_worker_done: dict[str, int] = {}
_slowest: list[tuple[float, str]] = []
_serial_current: tuple[str | None, float | None] = (None, None)
_tests_since_emit = 0
_last_emit_mono = 0.0
_last_budget_mono = 0.0
_total_collected: int | None = None
_workers_requested: int | str | None = None


def _elapsed() -> float:
    return time.monotonic() - _start_mono


def _worker_states() -> list[dict[str, Any]]:
    """What each worker said it was inside, read from the files the workers write.

    Files from another session (a previous pass in the same directory, or a rerun) are ignored by session tag, so a stale "still running"
    line cannot be read as evidence about a pass that is not running.
    """
    states: dict[str, dict[str, Any]] = {}
    directory = state_dir() if _progress_dir else None
    if directory is not None:
        for path in sorted(directory.glob("worker-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # Files from another pass are ignored; a fresh pass also cleans its own directory below, so this is belt and braces against a
            # pass whose id could not be resolved early enough.
            if raw.get("pass") != pass_id():  # another pass's file is not evidence about this one
                continue
            worker = str(raw.get("worker") or path.stem.replace("worker-", ""))
            started = raw.get("started")
            current = raw.get("current")
            states[worker] = {
                "worker": worker,
                "current": current,
                "running_s": round(time.time() - started, 1) if (current and started) else 0.0,
                "done": _per_worker_done.get(worker, 0),
            }
    if not states and _is_serial_pass():
        # A serial pass has no worker files: the test this process started is the whole live view.
        node, started = _serial_current
        states["main"] = {
            "worker": "main",
            "current": node,
            "running_s": round(time.time() - started, 1) if (node and started) else 0.0,
            "done": _per_worker_done.get("main", 0),
        }
    # A state left by the controller process of an xdist pass (it writes only at teardown, and finished no tests) is not a worker; a
    # worker that finished tests and went idle is, and stays listed so "8 workers, 7 idle" is visible.
    kept = {
        worker: state
        for worker, state in states.items()
        if (state["current"] or worker in _per_worker_done)
        and (_is_serial_pass() or worker != "main")
    }
    return sorted(kept.values(), key=lambda item: (-item["running_s"], item["worker"]))


def _record(kind: str, *, final: bool = False) -> dict[str, Any]:
    workers = _worker_states()
    record = {
        "kind": kind,
        "session": _SESSION,
        "pass": pass_id() or None,
        "shard": SHARD,
        "t": round(_elapsed(), 1),
        "wall": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "done": _counts["done"],
        "passed": _counts["passed"],
        "failed": _counts["failed"],
        "skipped": _counts["skipped"],
        "error": _counts["error"],
        "workers": len(workers),
        "worker_states": workers,
        "slowest": [[node, round(seconds, 1)] for seconds, node in _slowest[:SLOWEST_TOP]],
        "final": final,
    }
    if _total_collected is not None:
        record["collected"] = _total_collected
    if _workers_requested is not None:
        # At the start of a pass no worker has finished a test or written a state file, so `workers` reads 0; saying what was asked for
        # keeps the first line from looking like an idle pass.
        record["workers_requested"] = _workers_requested
    if BUDGET_SECONDS:
        record["budget_s"] = round(BUDGET_SECONDS, 1)
    return record


def _line(text: str) -> None:
    try:
        os.write(_REAL_OUT, (text + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def _emit(record: dict[str, Any]) -> None:
    if _ROLE != "main":
        return  # only the pass that owns the run writes records; see `pytest_runtest_logreport`
    with _LOCK:
        if PROGRESS_FILE is not None:
            try:
                PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
                with PROGRESS_FILE.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass  # same rule as the worker state file: progress costs nothing
        tag = "[ci-progress]" if record["kind"] == "progress" else "[ci-budget]"
        running = (
            ", ".join(
                f"{state['worker']}:{state['current']}({state['running_s']:.0f}s)"
                for state in record["worker_states"]
                if state["current"]
            )
            or "idle"
        )
        slow = ", ".join(f"{node} {seconds:.0f}s" for node, seconds in record["slowest"]) or "-"
        _line(
            f"{tag} shard={record['shard'] or '-'} t={record['t']:.1f}s "
            f"done={record['done']} pass={record['passed']} fail={record['failed']} "
            f"skip={record['skipped']} err={record['error']} "
            f"workers={record['workers']}{('/' + str(record['workers_requested'])) if 'workers_requested' in record else ''} "
            f"running=[{running}] slow=[{slow}]"
        )


def emit_progress(*, force: bool = False) -> None:
    """Print one progress line if its interval has come (or if `force`)."""
    global _tests_since_emit, _last_emit_mono
    _clean_state_dir()
    with _LOCK:
        elapsed = _elapsed()
        due = (
            force
            or (EVERY_SECONDS and (elapsed - _last_emit_mono) >= EVERY_SECONDS)
            or (EVERY_TESTS and _tests_since_emit >= EVERY_TESTS)
        )
        if not due:
            return
        _last_emit_mono = elapsed
        _tests_since_emit = 0
        _emit(_record("progress"))
        maybe_budget_dump()


def maybe_budget_dump() -> None:
    """Once past the budget, dump what is in flight once per interval, and keep running."""
    global _last_budget_mono
    if not BUDGET_SECONDS:
        return
    elapsed = _elapsed()
    if elapsed < BUDGET_SECONDS:
        return
    if _last_budget_mono and (elapsed - _last_budget_mono) < max(EVERY_SECONDS, 0.0):
        return
    _last_budget_mono = elapsed
    record = _record("budget")
    _line(
        f"[ci-budget] shard={record['shard'] or '-'} t={record['t']:.1f}s over the "
        f"{BUDGET_SECONDS:.0f}s budget; the pass keeps running (no flat cap: scripts/ci/unit-gates.sh). "
        f"If this step is stopped by the platform, this is what it was inside."
    )
    for state in record["worker_states"]:
        _line(
            f"[ci-budget]   {state['worker']}: {state['current'] or '<idle>'} "
            f"for {state['running_s']:.0f}s (tests done on this worker: {state['done']})"
        )
    if PROGRESS_FILE is not None:
        _emit_record_only(record)


def _emit_record_only(record: dict[str, Any]) -> None:
    if _ROLE != "main":
        return
    with _LOCK:
        try:
            PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with PROGRESS_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass


def _ticker() -> None:
    """Emit on wall time even when nothing is finishing - a wedged pass reports no reports, and that is the run worth reading."""
    interval = EVERY_SECONDS or 5.0
    while not _stop.wait(interval):
        try:
            emit_progress()
        except Exception as exc:  # a progress tool must never break the thing it reports on
            _line(f"[ci-progress] emitter error: {exc!r}")


def pytest_configure(config: Any) -> None:
    """Decide the role from `config.workerinput`, then start the controller's emitter."""
    global _ROLE, _WORKER_ID, _CONFIG
    # Before anything else, and for both roles: `_is_serial_pass()` and the pass id are asked from the first hook onwards, and a worker
    # that could not answer them writes its state file under the wrong pass and labels itself `main`.
    _CONFIG = config
    workerinput = getattr(config, "workerinput", None)
    if workerinput:
        _ROLE = "worker"
        _WORKER_ID = str(
            workerinput.get("workerid") or os.environ.get("PYTEST_XDIST_WORKER") or "worker"
        )
    else:
        _ROLE = "main"
        _WORKER_ID = "main"
    # Workers inherit the controller's environment after this point, so the session tag reaches their state files too.
    os.environ["HERDR_PYTEST_PROGRESS_SESSION"] = _SESSION
    if _ROLE != "main":
        return
    global _workers_requested
    try:
        requested = config.getoption("numprocesses")
    except (ValueError, AttributeError):
        requested = None
    _workers_requested = requested
    _clean_state_dir()  # may be unresolvable this early for an xdist pass; it is retried at every emit
    _stop.clear()
    thread = threading.Thread(target=_ticker, name="ci-progress", daemon=True)
    globals()["_thread"] = thread
    thread.start()
    emit_progress(force=True)


def pytest_report_collectionfinish(config: Any, start_path: Any, items: list[Any]) -> str:
    """Record the collected total (pytest renamed `startdir` to `start_path` in 8.x; the hookspec's name is what must match)."""
    global _total_collected
    _total_collected = len(items)
    return ""


def _worker_of(report: Any) -> str:
    node = getattr(report, "node", None)
    return (getattr(node, "workerinput", None) or {}).get("workerid", "main")


_outcomes: dict[str, str] = {}
SLOW_TEST_SECONDS = 1.0


def pytest_runtest_logreport(report: Any) -> None:
    """Count each test once, at its teardown, and classify it from the phases seen.

    Counting on the `call` report looks natural and is wrong twice over: a test skipped during `setup` never reaches `call`, and a setup
    error never reaches `call` either, so `done` silently under-reports exactly the tests a failing pass cares about. Teardown is the one
    phase every test reaches.
    """
    global _tests_since_emit
    if _ROLE == "worker":
        # A worker inherits `HERDR_PYTEST_PROGRESS_DIR`, and appending its own records to the shared file would count each test twice -
        # once by the worker, once by the controller that forwarded the report - and file the worker's private view under the pass's id.
        # The controller counts forwarded reports; the worker's only job here is the state file `pytest_runtest_logstart` writes.
        return
    key = report.nodeid
    finalized = False
    with _LOCK:
        if report.when == "setup":
            if report.failed:
                _outcomes[key] = "error"
            elif report.skipped:
                _outcomes[key] = "skipped"
        elif report.when == "call":
            if report.passed:
                _outcomes[key] = "passed"
            elif report.failed:
                _outcomes[key] = "failed"
            elif report.skipped:
                _outcomes[key] = "skipped"
            if report.duration >= SLOW_TEST_SECONDS:
                # Sub-second tests are not what an overrun is about, and listing them pushes the answer off the line.
                _slowest.append((report.duration, report.nodeid))
                _slowest.sort(reverse=True)
                del _slowest[SLOWEST_TOP:]
        elif report.when == "teardown":
            outcome = _outcomes.pop(key, "passed")
            _counts[outcome] += 1
            if report.failed and outcome != "error":
                _counts["error"] += (
                    1  # a failing teardown is a problem in the pass even when the test itself passed
                )
            _counts["done"] += 1
            worker = _worker_of(report)
            _per_worker_done[worker] = _per_worker_done.get(worker, 0) + 1
            _tests_since_emit += 1
            finalized = True
    if finalized:
        emit_progress()


def _stop_ticker_and_final() -> None:
    global _thread
    _stop.set()
    thread = globals().get("_thread")
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)
        globals()["_thread"] = None
    with _LOCK:
        _emit(_record("progress", final=True))


# `pytest_sessionfinish` runs in the controller too; the module-level hook above clears the worker's current test, so the final record
# has to be appended after it rather than beside it.
def pytest_unconfigure(config: Any) -> None:
    if _ROLE == "main":
        _stop_ticker_and_final()
