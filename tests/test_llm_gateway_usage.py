import json

from herdr_engineering import llm_gateway_usage as u


class FakeControl:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def report_batch(self, records):
        if self.fail:
            raise OSError("control plane down")
        self.events.extend(records)


def report(started, requests, completed, tokens_in, tokens_out):
    return {"startedAt": started, "usage": [
        {"principal": KEY, "model": "m", "requests": requests, "completed": completed,
         "inputTokens": tokens_in, "outputTokens": tokens_out}]}


KEY = "herdr:proj:k1"


def test_growth_becomes_one_event_per_request_with_exact_token_totals(tmp_path):
    control, state = FakeControl(), tmp_path / "s.json"
    assert u.sync_once(control, lambda: report(1, 3, 3, 10, 5), state) == 3
    assert len(control.events) == 3
    assert sum(e["input_tokens"] for e in control.events) == 10
    assert sum(e["output_tokens"] for e in control.events) == 5
    assert {(e["project"], e["key_id"], e["source"]) for e in control.events} == {
        ("proj", "k1", "llm-gateway")}


def test_only_new_growth_is_reported_on_the_next_pass(tmp_path):
    control, state = FakeControl(), tmp_path / "s.json"
    u.sync_once(control, lambda: report(1, 2, 2, 4, 2), state)
    n = u.sync_once(control, lambda: report(1, 3, 3, 10, 3), state)
    assert n == 1
    assert (control.events[-1]["input_tokens"], control.events[-1]["output_tokens"]) == (6, 1)


def test_a_gateway_restart_counts_from_zero_not_negative(tmp_path):
    control, state = FakeControl(), tmp_path / "s.json"
    u.sync_once(control, lambda: report(1, 5, 5, 5, 5), state)
    assert u.sync_once(control, lambda: report(2, 2, 2, 2, 2), state) == 2
    assert all(e["input_tokens"] >= 0 for e in control.events)


def test_failures_carry_no_tokens_and_are_marked_unsuccessful(tmp_path):
    control, state = FakeControl(), tmp_path / "s.json"
    u.sync_once(control, lambda: report(1, 3, 1, 9, 4), state)
    ok = [e for e in control.events if e["success"]]
    bad = [e for e in control.events if not e["success"]]
    assert (len(ok), len(bad)) == (1, 2)
    assert ok[0]["input_tokens"] == 9 and all(e["input_tokens"] == 0 for e in bad)


def test_a_refused_report_keeps_the_baseline_so_the_next_pass_retries(tmp_path):
    state = tmp_path / "s.json"
    def fetch():
        return report(1, 1, 1, 3, 1)

    assert u.sync_once(FakeControl(fail=True), fetch, state) is None
    assert not state.exists()
    retry = FakeControl()
    assert u.sync_once(retry, fetch, state) == 1


def test_an_unreachable_gateway_changes_nothing(tmp_path):
    def down():
        raise OSError("gateway down")
    control, state = FakeControl(), tmp_path / "s.json"
    assert u.sync_once(control, down, state) is None
    assert control.events == [] and not state.exists()


def test_principals_that_are_not_herdr_keys_are_ignored(tmp_path):
    control, state = FakeControl(), tmp_path / "s.json"
    u.sync_once(control, lambda: {"startedAt": 1, "usage": [
        {"principal": "default", "model": "m", "requests": 4, "completed": 4,
         "inputTokens": 1, "outputTokens": 1},
        {"principal": "__overflow__", "model": "__overflow__", "requests": 1, "completed": 1,
         "inputTokens": 1, "outputTokens": 1}]}, state)
    assert control.events == []
    assert json.loads(state.read_text())["rows"]  # still baselined, so it is not replayed later
