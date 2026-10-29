"""A slow boot must say where the time went."""
import logging

from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_import_state_logs_every_startup_phase(caplog):
    repo = ControlPlaneRepo()
    with caplog.at_level(logging.INFO, logger="herdr_engineering.control_plane.repo"):
        repo.import_state({})
    phases = [r.getMessage() for r in caplog.records if r.getMessage().startswith("boot import_state:")]
    for name in ("copy event streams", "scheduler state", "ledger", "lineage graph",
                 "delivery and CI history", "agent, inspector and host projections"):
        assert any(f": {name} took " in m for m in phases), name
