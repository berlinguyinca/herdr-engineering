"""Reproducible local fixture workflow; no email, Sheets, browser or model calls.

Run: python -m herdr_engineering.event_fixture --fixture PATH --output PRIVATE_DIR
The output directory is chosen by the caller; use only synthetic/user-supplied fixtures.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .artifacts import ArtifactWorkspaceProvider, FilesystemArtifactWorkspace, PublishResult
from .event_coordinator import Coordinator, Requirements, Venue


def prepare_fixture(data: dict, workspace: ArtifactWorkspaceProvider) -> PublishResult:
    coordinator = Coordinator("user:fixture", "task:event-draft-001", Requirements(**data["requirements"]),
                              tuple(v["id"] for v in data["venues"]), ("resource:event-fixture",))
    request, result = coordinator.browser_proposal("request:event-fixture-001", ("resource:event-fixture",))
    rows = []
    for row in data["venues"]:
        # Historical contact leads are deliberately never promoted to verified Contact.
        venue = Venue(row["id"], row["name"], row["city"], row["identity"])
        draft = coordinator.draft(venue, "fixture-host@example.test")
        rows.append({
            "id": venue.id, "name": venue.name, "priority": row["priority"],
            "identity": venue.identity, "historical_lead": row.get("contact"),
            "lead_source": row.get("source"), "provenance": "local_user_summary_fixture",
            "draft": asdict(draft), "draft_hash": draft.digest,
            "blockers": coordinator.blockers(venue, draft, None, 0),
            "unanswered": coordinator.unanswered(venue.id),
            "comparison": coordinator.comparison(venue.id),
        })
    report = {
        "version": 1, "mode": "offline_fixture", "source": data["source"],
        "completion": "drafts_prepared_outreach_blocked", "venues": rows,
        "browser": {"request": request, "result": result}, "checkpoint": coordinator.dumps(),
    }
    return workspace.publish_bytes(
        title="Offline event drafts and comparison", kind="event-fixture-report",
        data=json.dumps(report, indent=2, allow_nan=False).encode(),
        project="event-coordinator", mission_or_session=coordinator.task_id,
        rel_name="report.json", media_type="application/json",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = prepare_fixture(json.loads(args.fixture.read_text()), FilesystemArtifactWorkspace(root=args.output))
    print(json.dumps({"status": result.status, "artifact": result.artifact.to_dict() if result.artifact else None,
                      "error": result.error}))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
