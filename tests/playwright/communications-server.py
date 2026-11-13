"""Disposable real control-plane server for the communication browser tests."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from herdr_engineering.communications import Outbox  # noqa: E402
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.communications import record_observation  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

with tempfile.TemporaryDirectory(prefix="herdr-communications-browser-") as temporary:
    repo = ControlPlaneRepo()
    repo.upsert_session(
        "sender", source="herdr", host_id="browser-test", pane_id="w1:p1", agent_role="Claude"
    )
    repo.upsert_session(
        "recipient", source="herdr", host_id="browser-test", pane_id="w1:p2", agent_role="Codex"
    )
    box = Outbox(Path(temporary) / "observations.sqlite")
    for n in range(61):
        data = {
            "host_id": "browser-test",
            "text": "Clean safely <script>window.auditInjected=true</script> Bearer browsersecretvalue",
            "sender": {"pane_id": "w1:p1", "name": "Claude"},
            "recipient": {"pane_id": "w1:p2", "name": "Codex"},
            "route": [],
            "channel": "herdr.agent.prompt",
            "evidence_kind": "host_observation",
            "sender_origin": "caller_environment",
        }
        for status in ("queued", "delivered"):
            box.put(f"browser_{n}", status, data)
    for row in box.pending(200):
        record_observation(repo, row)
    server = ControlPlaneServer(repo, host="127.0.0.1", port=18765)
    server.serve()
