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


def test_load_into_logs_how_long_each_load_stage_takes(caplog):
    from herdr_engineering.control_plane.bridge import DurableSource, load_into

    class Empty(DurableSource):
        def missions(self): return []
        def sessions(self): return []
        def services(self): return []
        def hosts(self): return []
        def events(self): return []

    with caplog.at_level(logging.INFO, logger="herdr_engineering.control_plane.repo"):
        load_into(ControlPlaneRepo(), Empty())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("boot load_into:")]
    assert any(": group rows into streams took " in m for m in lines)
    assert any(": reconstruct entities from events took " in m for m in lines)
