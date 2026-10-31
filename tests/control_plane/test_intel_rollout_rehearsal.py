"""Rehearsal of RUNBOOK.md's enabling order against a private throwaway Postgres (never a live database).

Stage by stage: off -> enabled -> ingest -> restart (durable) -> kill switch -> disabled -> migration DOWN.
"""
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.control_plane import intel_bridge as br
from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.repo import ControlPlaneRepo

from ._private_pg import _bin, private_postgres

pytestmark = pytest.mark.integration
MIG = Path(__file__).parents[2] / "deploy/control-plane/migrations/0020_project_intelligence.sql"
CACHE = 2.1
KEY = {"kind": "operator", "id": "key:abc", "authenticated_by": "api_key"}


def ci_fix(repo, state, n):
    repo.record_event("ci_fix", "f1", "CIFixStateChanged", {"repository": "o/r", "run": 7, "state": state, "number": n})


def test_runbook_enabling_order_end_to_end():
    with private_postgres(MIG) as dsn:
        if dsn is None:
            pytest.skip("postgres binaries not installed")
        section = {}
        repo = ControlPlaneRepo()
        routes, bridge = br.install(repo, lambda: section, dsn)

        # 0. nothing configured: every route is 404, no store, no thread
        assert routes.get(["status"], {}, KEY, "operator")[0] == 404
        ci_fix(repo, "queued", 1)
        assert bridge._thread is None and routes._store is None

        # 1. enabled only: routes answer, nothing is ingested
        section.update(enabled=True, default_org_id="lab", forge_host="github.com",
                       viewers={"key:abc": {"org_id": "lab", "projects": "*"}})
        time.sleep(CACHE)                                   # the config cache is 2 s by design
        st, body = routes.get(["status"], {}, KEY, "operator")
        assert st == 200 and body["flags"]["ingest"] is False
        ci_fix(repo, "running", 2)
        bridge.drain()
        assert routes.get(["projects"], {}, KEY, "operator") == (200, {"items": []})

        # 2. ingest on: the YAML-mapped CI stream lands as a timeline event, durably
        section.update(ingest=True)
        time.sleep(CACHE)
        repo.record_event("ci_fix", "f2", "CIFixRequested", {"repository": "o/r", "run": 8, "state": "queued"})
        bridge.drain()
        assert bridge.stats["accepted"] == 1
        assert {i["key"].split("|")[1]: i["state"] for i in bridge.board.summary()}["o/r#8"] == "AUTHORIZED"
        project = "github.com/o/r"                          # from the YAML forge_host + the validated first event
        assert routes.store.timeline(pi.Viewer("lab", None, "restricted"), project_id=project)["total"] == 1

        # 3. restart: a fresh install reads the same rows back
        routes2, bridge2 = br.install(ControlPlaneRepo(), lambda: section, dsn)
        assert routes2.store.timeline(pi.Viewer("lab", None, "restricted"), project_id=project)["total"] == 1

        # 4. kill switch: ingest refuses, reads still work
        section.update(kill_switch=True)
        time.sleep(CACHE)
        with pytest.raises(pi.IngestionPaused):
            routes2.store.append_event({"org_id": "lab", "source_system": "s", "source_instance": "i", "source_id": "x", "event_type": "t",
                                        "occurred_at": "2026-10-01T00:00:00+00:00", "object_ref": "o:1", "project_ids": [project]},
                                       cfg=pi.config(section))
        assert routes2.get(["projects"], {}, KEY, "operator")[0] == 200

        # 5. disabled: back to 404
        section.update(enabled=False, kill_switch=False)
        time.sleep(CACHE)
        assert routes2.get(["status"], {}, KEY, "operator")[0] == 404

        # 6. migration DOWN drops only pi_* tables
        psql = _bin("psql")
        sock = dsn.split("host=")[1]
        down = "DROP TABLE IF EXISTS pi_audit, pi_finding, pi_relationship, pi_outbox, pi_event, pi_source_state, pi_record;"
        subprocess.run([psql, "-h", sock, "-U", "t", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-q", "-c", down], check=True)
        left = subprocess.run([psql, "-h", sock, "-U", "t", "-d", "postgres", "-At", "-c",
                               "select count(*) from information_schema.tables where table_name like 'pi\\_%'"],
                              check=True, capture_output=True, text=True).stdout.strip()
        assert left == "0"
