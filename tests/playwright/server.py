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
    return m1, m2, s1


def main():
    repo = ControlPlaneRepo()
    _seed(repo)

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
