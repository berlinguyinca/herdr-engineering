"""Playwright test fixture: serve the control-plane SPA with seeded data and a
continuous heavy event burst.

The burst drives the SSE live stream so the browser's live-refresh path is
exercised under load (the scenario that previously produced "too many
requests" / "Failed to fetch"). The burst is recorded *directly* into the repo
(not via the HTTP ingest endpoint) so it does not consume the API rate-limit
budget itself, letting the test observe only the browser's real API traffic.

Usage:
    BURST_INTERVAL=0.02 python tests/playwright/server.py
Prints "PORT=<n>" on stdout once ready, then runs until killed.
"""
import os
import sys
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from herdr_engineering.control_plane.api import (  # noqa: E402
    ControlPlaneServer,
    _RateLimiter,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402


def _seed(repo):
    m1 = repo.create_mission("Alpha", purpose="first mission")
    m2 = repo.create_mission("Beta", purpose="second mission")
    s1 = repo.create_session(mission_id=m1, agent_role="pi", model="gpt-4o")
    repo.upsert_host("beast", "beast", "100.96.156.65")
    repo.upsert_host("bender", "bender", "100.104.39.6")
    repo.upsert_host("mac.lan", "macbook-pro", "100.84.185.100")
    repo.register_service("web", "beast", 8080, session_id=s1)
    repo.register_service("gateway", "bender", 9000)
    repo.record_event("mission", m1, "MissionStarted",
                      {"stage": "ready", "title": "Alpha"}, mission_id=m1)
    repo.record_event("session", s1, "SessionStarted",
                      {"cwd": "/home/user/work", "status": "active",
                       "attach": f"herdr workspace focus {m1}"},
                      mission_id=m1, session_id=s1)
    if os.environ.get("SERVICES_SEED") == "1":
        repo.register_service("Spectra API", "beast", 18012, owner="python",
                              project="metabolomics", source_port=8080,
                              description="Parse experimental spectra")
        repo.register_service("Inactive parser", "beast", 18013, status="unavailable")
        archived = repo.register_service("Archived parser", "beast", 18014,
                                         status="unavailable")
        repo._services[archived]["unavailable_since"] = "2020-01-01T00:00:00+00:00"
        stale = repo.register_service("Stale healthy parser", "beast", 18016)
        repo._services[stale]["updated_at"] = "2020-01-01T00:00:00+00:00"
        repo.register_service("Unsafe <script>window.catalogXss=true</script>", "beast", 18015,
                              route="javascript:alert(1)")
    # Non-herdr-sourced mission: keyed by mission_key 'pi:MSN-PI1' (UI-008).
    repo.upsert_mission("MSN-PI1", title="Pi mission", purpose="from pi",
                       stage="implementation", source="pi")
    repo.record_event("mission", "pi:MSN-PI1", "MissionStageChanged",
                      {"from": "planning", "to": "implementation"},
                      mission_id="MSN-PI1")
    # Merged timeline entities for the pi mission (UI-009): one passed test
    # run and one merged pull request -> timeline kinds 'test' and 'pr'/'merge'.
    repo.upsert_test_run('run-1', mission_id='pi:MSN-PI1', status='passed',
                         passed=3, failed=0, skipped=0, total=3)
    repo.upsert_pull_request('pr-1', mission_id='pi:MSN-PI1', number=42,
                             url='https://example.com/pr/42', status='merged',
                             branch='ws/x', base='main')
    return m1, m2, s1


def _seed_inspector(repo, sid):
    """Rich inspector data for the session detail view (spec 0200)."""
    t = lambda call, phase, **kw: {"kind": "tool", "call_id": call, "phase": phase, **kw}  # noqa: E731
    repo.inspector.ingest(sid, {"events": [
        {"kind": "capabilities", "reasoning": "summary", "streaming_tools": True, "diffs": True},
        {"kind": "message", "role": "user", "text": "make the parser faster"},
        {"kind": "reasoning", "text": "I should profile before changing anything"},
        {"kind": "message", "role": "assistant", "text": "Profiling the parser first."},
        t("c1", "start", tool="bash", args_preview="pytest -q"),
        t("c1", "end", output="2 failed, 8 passed", exit_code=1, is_error=True, duration_ms=1500),
        t("c2", "start", tool="bash", args_preview="make build", agent_id="builder", parent_agent_id="main"),
        t("c2", "update", output="compiling…", agent_id="builder", parent_agent_id="main"),
        {"kind": "file", "path": "src/parser.py", "op": "edit",
         "diff": "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1,2 +1,2 @@\n-slow()\n+fast()"},
        {"kind": "file", "path": "config/.env", "op": "read", "diff": "+SECRET_ONLY_FOR_OPERATORS"},
        {"kind": "test", "run_id": "r1", "phase": "end", "command": "pytest -q", "passed": 8, "failed": 2,
         "failures": [{"name": "test_parse_empty", "message": "AssertionError: expected []", "file": "t.py", "line": 12}]},
        {"kind": "usage", "input_tokens": 1200, "output_tokens": 300, "context_tokens": 40000,
         "context_window": 200000, "model": "gpt-4o"},
        {"kind": "association", "mission_id": "m-alpha", "branch": "perf/parser", "worktree": "/wt/parser",
         "handoff_from": "w0:p0", "pr": {"number": 42, "url": "https://github.com/o/r/pull/42", "repo": "o/r"}},
        {"kind": "model", "model": "gpt-4o", "previous_model": "gpt-4o-mini", "reason": "handoff"},
    ]})


def main():
    repo = ControlPlaneRepo()
    _m1, _m2, _s1 = _seed(repo)
    if os.environ.get("INSPECTOR_SEED", "1") != "0":
        _seed_inspector(repo, _s1)

    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0,
                             web_root=_REPO / "packages")
    # Stricter than the production default (600/min): a throttled live-
    # refreshing dashboard must respect a finite per-IP budget. This makes the
    # rate-limit regression test fast and unambiguous (an un-throttled client
    # re-rendering on every event exceeds even 600/min within ~18s, but trips
    # this 200/min budget in a few seconds).
    budget = int(os.environ.get("RATE_BUDGET", "200"))
    svr._limiter = _RateLimiter(max_requests=budget, window_seconds=60)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    print(f"PORT={svr.port}", flush=True)

    # Emit events in short bursts separated by gaps. The SPA debounces its
    # live refresh to ~150ms after the last event; a burst-with-gap pattern
    # (not a continuous trickle) is what makes that debounce actually fire
    # between bursts -- reproducing the production shape that previously
    # produced "too many requests" / "Failed to fetch".
    burst_len = int(os.environ.get("BURST_LEN", "6"))
    burst_gap = float(os.environ.get("BURST_GAP", "0.18"))
    interval = float(os.environ.get("BURST_INTERVAL", "0.02"))

    def _burst():
        i = 0
        while True:
            for _ in range(burst_len):
                # "Telemetry" is an unrecognised event_type -> humanize renders
                # it via the generic default branch; it never crashes the UI.
                repo.record_event("host", "beast", "Telemetry", {"cpu": i % 100})
                i += 1
                time.sleep(interval)
            time.sleep(burst_gap)

    if os.environ.get("BURST", "1") != "0":
        threading.Thread(target=_burst, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
