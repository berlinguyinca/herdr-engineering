"""Real control plane serving the dashboard with a MOCKED herdr-memory adapter (Memory Explorer UI test).

Every record below is test data defined in this file; no herdr-memory CLI or Hindsight is contacted."""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.memory_explorer import MemoryNotFound  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

RECORDS = [{"memory_id": f"mock-{i:02}", "bank_id": "herdr-engineering-facts", "revision": 1 + i % 3, "kind": "fact",
            "status": "verified" if i % 2 else "candidate", "text": f"MOCK memory {i}: example fact for the UI test",
            "provenance": {"session_id": "mock-session", "mission_id": "mock-mission", "host_id": "mock-host", "harness_id": "claude",
                           "evidence_refs": ["handoff:mock-handoff"]}} for i in range(25)]


SELECTOR_RECORDS = [
    {"memory_id": "selector-summary", "bank_id": "fixture-project-summaries", "kind": "summary",
     "text": "Selector fixture summary <img src=x onerror=alert(1)>", "status": "candidate", "revision": 1, "provenance": {}},
    {"memory_id": "selector-decision", "bank_id": "fixture-project-facts", "kind": "decision",
     "text": "Selector fixture decision", "status": "verified", "revision": 1, "provenance": {}},
    {"memory_id": "selector-global", "bank_id": "herdr-global", "kind": "fact",
     "text": "Selector fixture global", "status": "verified", "revision": 1,
     "provenance": {"repo_id": "fixture-project"}},
]


def selected(record, scope, project, kind):
    if (record["kind"] == "summary") != (kind == "summary"):
        return False
    if scope in ("global", "infra"):
        return record["bank_id"] == "herdr-" + scope
    return record["bank_id"] == project + ("-summaries" if kind == "summary" else "-facts")


class MockAdapter:
    def recall(self, query, *, scope=None, project=None, max_results=200, kind="fact"):
        return {"results": [r for r in RECORDS + SELECTOR_RECORDS
                            if selected(r, scope, project, kind) and query.lower() in r["text"].lower()][:max_results],
                "degraded": False, "offline": False}

    def get(self, memory_id, *, scope="project", project=None, kind="fact"):
        for r in RECORDS + SELECTOR_RECORDS:
            if r["memory_id"] == memory_id and selected(r, scope, project, kind):
                return r
        raise MemoryNotFound()

    def status(self):
        return {"healthy": True, "outbox_pending": 0, "dead_letter": 0, "server_version": "mock", "contract": 1}


if __name__ == "__main__":
    server = ControlPlaneServer(ControlPlaneRepo(), web_root=ROOT / "packages", memory_adapter=MockAdapter(), allow_open=True,
                                memory_config={"enabled": True, "viewers": {"open-listener": {"projects": ["herdr-engineering", "fixture-project"], "scopes": ["global"]}}})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    print(f"PORT={server.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        server.shutdown()
