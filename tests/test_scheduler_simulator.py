import copy

import pytest

from herdr_engineering.replay import placement_preview


def history():
    def candidate(cpu, memory, reasons=()):
        return {"eligible": not reasons, "reason_codes": list(reasons), "measured": {
            "total_cpu": 10, "available_cpu": cpu,
            "total_memory_gib": 10, "available_memory_gib": memory}}
    return [{"request_id": "one", "requested_cpu": 1, "requested_memory": 1,
             "decision": {"selected_host_uid": "a", "explanations": {
                 "a": candidate(9, 2), "b": candidate(2, 9),
                 "mac": candidate(10, 10, ["HOST_MANUAL_ONLY"])}}}]


def test_counterfactual_weights_change_rank_without_relaxing_exclusions_or_mutating_history():
    rows = history()
    previous = copy.deepcopy(rows)
    cpu = placement_preview(rows, cpu_weight=1, memory_weight=0)
    memory = placement_preview(rows, cpu_weight=0, memory_weight=1)
    assert cpu["decisions"][0]["simulated_host_uid"] == "a"
    assert memory["decisions"][0]["simulated_host_uid"] == "b"
    assert memory["changed_placements"] == 1
    assert rows == previous and memory["mutates_policy"] is False
    assert placement_preview(rows, excluded_hosts=["a", "b"])["queued"] == 1


def test_missing_historical_capacity_is_unknown_instead_of_zero_load():
    rows = history()
    del rows[0]["decision"]["explanations"]["a"]["measured"]["total_cpu"]
    del rows[0]["decision"]["explanations"]["b"]["measured"]["total_cpu"]
    assert placement_preview(rows)["decisions"][0]["state"] == "unknown"


def test_simulator_rejects_invalid_weights():
    with pytest.raises(ValueError, match="weights"):
        placement_preview(history(), cpu_weight=float("nan"))
