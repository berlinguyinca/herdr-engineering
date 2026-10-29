"""Real local control plane for the Deployments page test (deploy-4 Task 4).

``python deployments-server.py defined`` loads three pipeline definitions over two stages,
a green CI release, confirmed release evidence, and host events: hive-gateway healthy
(holding), hive-nodes failed with a hostile ``<img onerror>`` evidence string (stage 1
halts). ``python deployments-server.py empty`` has no definitions configured.
Prints "PORT=<n>" once ready, then runs until killed.
"""
import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.deployment_coordinator import ReleaseEvidence  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from herdr_engineering.fleet_config import FleetConfigStore  # noqa: E402

REPO = "InferWeave/inferweave"
OLD, NEW = "0" * 40, "c0ffee" + "1" * 34
HOSTILE = '<img src=x onerror="window.__pwned=1">boom'


def _definition(component, stage, order, host):
    return {"component": component, "stage": stage, "order": order, "host": host,
            "updater": "deploy/hive/iw-node-pull-deploy",
            "follows": {"repo": REPO, "tag": "node-ci-latest", "asset": "inferweave",
                        "branch": "main", "ci_context": "ci/woodpecker/push/rust"},
            "restart": "gateway-cycle", "gpu": False,
            "health": {"probe": "served-request", "timeout_s": 600, "hold_s": 600},
            "rollback": {"strategy": "previous-version"}}


def _event(n, component, outcome, host="login2.hive", evidence=""):
    return {"id": f"{n:032x}", "ts": "2026-10-28T12:00:00Z", "host": host, "component": component,
            "step": f"deployment.{component}.cycle", "outcome": outcome, "from": OLD, "to": NEW,
            "evidence": evidence}


def main(mode):
    tmp = Path(tempfile.mkdtemp(prefix="herdr-deployments-ui-"))
    values = {}
    if mode == "defined":
        pipelines = tmp / "pipelines"
        pipelines.mkdir(mode=0o755)
        for doc in (_definition("hive-gateway", 1, 0, "hive"), _definition("hive-nodes", 1, 1, "hive"),
                    _definition("whiteale-gateway", 2, 0, "whiteale")):
            path = pipelines / f"{doc['component']}.yaml"
            path.write_text(yaml.safe_dump(doc))
            os.chmod(path, 0o644)
        values = {"deployment_definitions": str(pipelines)}
    fleet = tmp / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": values}} if values else {},
        "hosts": {h: {"roles": ["node"], "token_sha256": hashlib.sha256(h.encode()).hexdigest()}
                  for h in ("hive", "whiteale")}}))
    os.chmod(fleet, 0o600)
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, web_root=ROOT / "packages", fleet_config=FleetConfigStore(fleet))
    server.ci_monitor.close()
    ci = {"available": True, "pipelines": {REPO: [
        {"repository": REPO, "run_number": 7, "revision": NEW, "branch": "main", "event": "push",
         "state": "success"}]}}
    server.ci_monitor.snapshot = lambda start=True: ci
    if mode == "defined":
        server.deployment_evidence = lambda track, target, plan: ReleaseEvidence(target, True)
        for n, (component, outcome, evidence) in enumerate([
                ("hive-gateway", "started", ""), ("hive-gateway", "ok", "probe ok: 3 models answered"),
                ("hive-nodes", "started", ""), ("hive-nodes", "failed", HOSTILE)], start=1):
            repo.deployments.ingest(_event(n, component, outcome, evidence=evidence), reported_by="hive")
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    server.deployments_tick()
    print(f"PORT={server.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "defined")
