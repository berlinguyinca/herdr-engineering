"""Durable pull-request trails for the learning loop (``learning_trails``): readiness, assembly once per end, null propagation, bounds,
redaction, export index, late evidence, retention, access."""
import copy
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import learning_trails
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.learning_trails import (
    ENTITY, EVENT, LATE, MAX_BYTES, TrailRecords, learning_config, parse_repository, scrub)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000
OK = {"ci": "ok", "handoffs": "ok", "reviews": "ok", "plan_reviews": "ok"}


def sig(value=None, reason="no evidence"):
    return {"value": value, "reason": reason}


def fake_trail(extra=None, sources=None):
    body = {
        "pull_request": {"number": 7, "repository": "org/app", "status": "merged", "url": "https://github.com/org/app/pull/7",
                         "branch": "feat/x", "opened_at": "2026-10-01T09:00:00+00:00"},
        "state": "linked", "reason": "ok", "sources": dict(OK if sources is None else sources), "omitted": {"sessions": 0},
        "sessions": [{"session_id": "s1", "rule": "branch_window", "inferred": False, "name": "Swift Amber", "branch": "feat/x"}],
        "inferred_sessions": [{"session_id": "hint"}], "ambiguous": [],
        "handoffs": [{"handoff_id": "h1", "state": "accepted", "goal": "fix the parser for alice@example.com", "rule": "session",
                      "from": {"harness": "pi", "agent_id": "a1", "session_id": "s1"}, "to": {"harness": "claude", "session_id": "s2"}}],
        "reviews": [{"review_id": "r1", "source": "review", "kind": "code", "verdict": "approved", "reviewer": "carol", "rule": "session"}],
        "ci_runs": [{"run_number": 3, "state": "success", "ci_url": "https://ci.example/3", "branch": "feat/x", "match": "branch"}],
        "timeline": [{"at": "x"}],
        "signals": {"merged_without_rework": sig(None, "CI data is unavailable."), "duration_s": sig(120, "reported end")},
    }
    body.update(extra or {})
    return body


def fake_evolution(session_id, sources=None):
    return {"sources": dict({"handoffs": "ok", "reviews": "ok", "plan_reviews": "ok", "ci": "ok", "event_stream": "ok", "tools": "ok"},
                            **(sources or {})),
            "learning_record": {"session_id": session_id, "tool_calls": {"total": sig(None, "tool events were lost")},
                                "prs": [{"ref": "org/app#7", "signals": {"merged_without_rework": sig(True, "x")}}],
                                "outcome": sig("finished", "terminal status")}}


def world(*, trail=None, evolution=fake_evolution, **cfg):
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    calls = []

    def trail_for(host, owner, name, number):
        calls.append((host, owner, name, number))
        return trail() if callable(trail) else (fake_trail() if trail is None else trail)

    cfg.setdefault("warmup_minutes", 0)
    records = TrailRecords(repo, learning_config(cfg), trail_for=trail_for, evolution_for=evolution, now=lambda: clock[0])
    return repo, records, clock, calls


def end_pr(repo, status="merged", **kw):
    return repo.upsert_pull_request("pr-1", repository="github.com/org/app", number=7, status=status, branch="feat/x", **kw)


def stored(repo, event_type=EVENT, pr_id="pr-1"):
    return [e for e in repo.stream(ENTITY, pr_id) if e["event_type"] == event_type]


# ---------------------------------------------------------------- assembly
def test_an_ended_pr_gets_one_record_with_signals_agents_and_sources():
    repo, records, _, calls = world()
    end_pr(repo)
    assert records.sweep(force=True) == 1
    (event,) = stored(repo)
    p = event["payload"]
    assert p["schema_version"] == 1 and p["kind"] == "pull_request_trail" and p["pr_id"] == "pr-1" and p["end_seq"] == 1
    assert p["ended_source"] == "observed" and p["ended_at"] == repo.get_pull_request("pr-1")["merged_at"]
    assert p["incomplete"] is False and p["missing_sources"] == []
    assert p["signals"]["merged_without_rework"] == sig(None, "CI data is unavailable.")
    assert p["sources"]["ci"] == "ok" and p["sources"]["agent_evolution"]["state"] == "ok"
    assert [a["session_id"] for a in p["agents"]["items"]] == ["s1"] and p["agents"]["items"][0]["rule"] == "branch_window"
    assert "prs" not in p["agents"]["items"][0]["learning_record"] and p["agents"]["items"][0]["learning_record"]["tool_calls"]["total"]["value"] is None
    assert p["inferred_sessions_count"] == 1 and "timeline" not in p and "inferred_sessions" not in p
    assert p["pull_request"]["host"] == "github.com" and p["pull_request"]["repository"] == "org/app"
    assert calls == [("github.com", "org", "app", 7)] and event["entity_type"] == ENTITY
    assert "learning_seq" in p


def test_only_an_allowlist_is_stored_no_goals_names_review_text_or_urls():
    repo, records, _, _ = world()
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    text = json.dumps(p)
    for leaked in ("fix the parser", "alice@example.com", "Swift Amber", "carol", "ci.example", "agent_id"):
        assert leaked not in text, leaked
    assert p["handoffs"]["items"][0]["from"] == {"harness": "pi", "session_id": "s1"} and p["handoffs"]["items"][0]["state"] == "accepted"
    assert p["reviews"]["items"][0]["verdict"] == "approved" and "reviewer" not in p["reviews"]["items"][0]
    assert p["ci_runs"]["items"][0]["run_number"] == 3 and "ci_url" not in p["ci_runs"]["items"][0]
    assert "[REDACTED]" not in text, "no ordinary key was masked by the key hints"


def test_assembly_happens_once_per_end_and_a_replay_or_second_sweep_writes_nothing():
    repo, records, _, calls = world()
    end_pr(repo)
    assert records.sweep(force=True) == 1
    assert records.sweep(force=True) == 0
    end_pr(repo)
    assert records.sweep(force=True) == 0 and len(stored(repo)) == 1 and len(calls) == 1
    fresh = ControlPlaneRepo(now=lambda: NOW)
    fresh.import_state(repo.export_state())
    again = TrailRecords(fresh, learning_config({"warmup_minutes": 0}), trail_for=lambda *a: fake_trail(), evolution_for=fake_evolution, now=lambda: NOW)
    assert again.sweep(force=True) == 0 and len(stored(fresh)) == 1, "the stored record survives a restart and is not written twice"


def test_a_reopened_pr_that_ends_again_gets_a_second_record_not_a_rewrite():
    repo, records, _, _ = world(low_confidence_after_minutes=10080)
    end_pr(repo, merged_at="2026-10-01T10:00:00Z")
    records.sweep(force=True)
    end_pr(repo, status="open")
    end_pr(repo, status="merged", merged_at="2026-10-02T10:00:00Z")
    assert records.sweep(force=True) == 1
    assert [e["payload"]["ended_at"] for e in stored(repo)] == ["2026-10-01T10:00:00Z", "2026-10-02T10:00:00Z"]
    assert [e["payload"]["end_seq"] for e in stored(repo)] == [1, 2] and [e["payload"]["learning_seq"] for e in stored(repo)] == [0, 1]


def test_a_legacy_pr_without_end_count_is_one_end_and_a_reopen_then_end_is_a_second_without_late_evidence():
    repo, records, _, _ = world()
    repo._pull_requests["old"] = {"pr_id": "old", "mission_id": None, "url": None, "base": None, "repository": "github.com/org/app", "number": 9, "status": "merged", "branch": "b",
                                  "merged_at": "2026-10-01T10:00:00Z", "closed_at": None, "ended_source": "reported", "created_at": "x", "updated_at": "x"}
    assert records.sweep(force=True) == 1
    assert stored(repo, EVENT, "old")[0]["payload"]["end_seq"] == 1
    repo.upsert_pull_request("old", status="open")
    assert repo.get_pull_request("old")["end_count"] == 1
    repo.upsert_pull_request("old", status="merged", merged_at="2026-10-05T10:00:00Z")
    assert repo.get_pull_request("old")["end_count"] == 2
    assert records.sweep(force=True) == 1
    assert [e["payload"]["end_seq"] for e in stored(repo, EVENT, "old")] == [1, 2] and stored(repo, LATE, "old") == []


def test_open_prs_and_prs_without_an_end_time_are_not_assembled():
    repo, records, _, calls = world()
    repo.upsert_pull_request("open", repository="github.com/org/app", number=1, status="open")
    repo.upsert_pull_request("old", repository="github.com/org/app", number=2, status="merged")
    repo._pull_requests["old"]["merged_at"] = None
    assert records.sweep(force=True) == 0 and calls == [] and repo.entity_ids(ENTITY) == []


def test_null_propagation_nothing_is_replaced_by_a_zero_and_unreadable_agents_are_named():
    repo, records, clock, _ = world(evolution=lambda session_id: None, ready_retry_minutes=0)
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert p["signals"]["merged_without_rework"]["value"] is None
    assert p["agents"]["items"][0]["learning_record"] is None and "could not be read" in p["agents"]["items"][0]["reason"]
    assert p["sources"]["agent_evolution"]["state"] == "partial" and p["incomplete"] is True and p["missing_sources"] == ["agent_evolution"]
    unwired = world(evolution=None, ready_retry_minutes=0)
    end_pr(unwired[0])
    unwired[1].sweep(force=True)
    q = stored(unwired[0])[0]["payload"]
    assert q["agents"]["items"] is None and q["sources"]["agent_evolution"]["state"] == "unavailable" and q["incomplete"] is True


def test_agents_are_capped_but_the_omission_is_recorded():
    many = fake_trail({"sessions": [{"session_id": f"s{i}", "rule": "commit"} for i in range(5)]})
    repo, records, _, _ = world(trail=many, max_agents=2)
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert len(p["agents"]["items"]) == 2 and p["agents"]["omitted"] == 3 and p["omitted"]["agents"] == 3 and p["incomplete"] is False
    assert p["sources"]["agent_evolution"]["state"] == "partial" and "max_agents" in p["sources"]["agent_evolution"]["reason"]


def test_an_unreadable_trail_writes_nothing_and_is_retried_at_the_interval_not_every_sweep():
    repo, records, clock, calls = world(trail=lambda: None)
    end_pr(repo)
    assert records.sweep(force=True) == 0 and repo.entity_ids(ENTITY) == []
    assert records.sweep(force=True) == 0 and len(calls) == 1, "backed off"
    clock[0] += 301
    assert records.sweep(force=True) == 0 and len(calls) == 2


def test_disabled_writes_nothing():
    repo, records, _, calls = world(enabled=False)
    end_pr(repo)
    assert records.sweep(force=True) == 0 and calls == []


def test_the_sweep_is_batched_and_throttled_on_the_clock():
    repo, records, clock, _ = world(sweep_batch=2)
    for n in range(5):
        repo.upsert_pull_request(f"pr-{n}", repository="github.com/org/app", number=10 + n, status="merged", merged_at="2026-10-01T10:00:00Z")
    assert records.sweep() == 2
    assert records.sweep() == 0, "throttled"
    clock[0] += 301
    assert records.sweep() == 2


def test_the_throttle_also_holds_in_real_time():
    repo = ControlPlaneRepo()
    records = TrailRecords(repo, learning_config({"warmup_minutes": 0}), trail_for=lambda *a: fake_trail(), evolution_for=fake_evolution)
    repo.upsert_pull_request("p", repository="github.com/org/app", number=1, status="merged", merged_at="2026-10-01T10:00:00Z")
    assert records.sweep() == 1
    repo.upsert_pull_request("q", repository="github.com/org/app", number=2, status="merged", merged_at="2026-10-01T10:00:00Z")
    assert records.sweep() == 0, "five minutes have not passed"
    assert records.sweep(force=True) == 1


# ---------------------------------------------------------------- readiness: cold start and transient outages
def test_nothing_is_assembled_before_the_warm_up_ends_even_after_a_restart():
    repo, records, clock, calls = world(warmup_minutes=10)
    end_pr(repo)
    assert records.sweep(force=True) == 0 and calls == []
    clock[0] += 599
    assert records.sweep(force=True) == 0 and calls == []
    clock[0] += 2
    assert records.sweep(force=True) == 1
    fresh = ControlPlaneRepo(now=lambda: clock[0])
    fresh.import_state(repo.export_state())
    fresh.upsert_pull_request("late", repository="github.com/org/app", number=3, status="merged", merged_at="2026-10-01T10:00:00Z")
    cold = TrailRecords(fresh, learning_config({"warmup_minutes": 10}), trail_for=lambda *a: fake_trail(sources={"ci": "unavailable"}),
                        evolution_for=fake_evolution, now=lambda: clock[0])
    assert cold.sweep(force=True) == 0, "a restarted process waits before it freezes anything"


def test_a_critical_source_that_is_not_ready_is_retried_then_stored_as_incomplete_naming_it():
    state = {"sources": {**OK, "ci": "unavailable"}}
    repo, records, clock, calls = world(trail=lambda: fake_trail(sources=state["sources"]))
    end_pr(repo)
    assert records.sweep(force=True) == 0 and repo.entity_ids(ENTITY) == []
    clock[0] += 301 * 6
    assert records.sweep(force=True) == 0, "still inside the 60 minute retry window"
    clock[0] += 301 * 7
    assert records.sweep(force=True) == 1
    p = stored(repo)[0]["payload"]
    assert p["incomplete"] is True and p["missing_sources"] == ["ci"] and p["sources"]["ci"] == "unavailable" and len(calls) >= 3


def test_a_source_that_comes_back_inside_the_window_gives_a_complete_record_once():
    state = {"sources": {**OK, "handoffs": "unavailable", "reviews": "truncated"}}
    repo, records, clock, _ = world(trail=lambda: fake_trail(sources=state["sources"]))
    end_pr(repo)
    assert records.sweep(force=True) == 0
    state["sources"] = dict(OK)
    clock[0] += 301
    assert records.sweep(force=True) == 1 and records.sweep(force=True) == 0
    p = stored(repo)[0]["payload"]
    assert p["incomplete"] is False and p["missing_sources"] == [] and len(stored(repo)) == 1


def test_partial_agent_sources_are_not_ready_either():
    repo, records, clock, _ = world(evolution=lambda sid: fake_evolution(sid, {"ci": "unavailable", "tools": "unavailable"}))
    end_pr(repo)
    assert records.sweep(force=True) == 0
    clock[0] += 3601
    assert records.sweep(force=True) == 1
    assert stored(repo)[0]["payload"]["missing_sources"] == ["agent_evolution.ci", "agent_evolution.tools"]


def test_a_gate_timeout_that_returns_no_trail_is_not_ready_and_never_frozen():
    answers = [None, fake_trail()]
    repo, records, clock, _ = world(trail=lambda: answers.pop(0) if len(answers) > 1 else answers[0])
    end_pr(repo)
    assert records.sweep(force=True) == 0 and repo.entity_ids(ENTITY) == []
    clock[0] += 301
    assert records.sweep(force=True) == 1 and stored(repo)[0]["payload"]["incomplete"] is False


def test_the_pr_moving_while_its_trail_is_built_skips_the_write():
    repo, records, clock, _ = world()

    def moves_underneath():
        end_pr(repo, status="open")                     # reopened while the trail is being built
        return fake_trail()
    records.trail_for = lambda host, owner, name, number: moves_underneath()
    end_pr(repo)
    assert records.sweep(force=True) == 0 and repo.entity_ids(ENTITY) == []


# ---------------------------------------------------------------- confidence of an observed end
def test_an_observed_end_assembled_late_is_low_confidence_and_withholds_the_signals_built_on_it():
    signals = {"merged_without_rework": sig(True, "x"), "ci_green_first_try": sig(True, "y"), "ci_reruns": sig(0, "z"),
               "duration_s": sig(120, "d"), "review_verdicts": sig(["approved"], "v")}
    repo, records, clock, _ = world(trail=lambda: fake_trail({"signals": copy.deepcopy(signals)}))
    end_pr(repo)
    clock[0] += 1800
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert p["end_time_confidence"] == "low" and p["withheld_signals"] == ["duration_s", "ci_green_first_try", "ci_reruns", "merged_without_rework"]
    for name in p["withheld_signals"]:
        assert p["signals"][name]["value"] is None and "Withheld" in p["signals"][name]["reason"]
    assert p["signals"]["review_verdicts"]["value"] == ["approved"]


def test_a_reported_end_or_a_prompt_observation_is_high_confidence():
    repo, records, clock, _ = world(trail=lambda: fake_trail({"signals": {"duration_s": sig(120, "d")}}))
    end_pr(repo)
    records.sweep(force=True)
    assert stored(repo)[0]["payload"]["end_time_confidence"] == "high"
    other, rec2, clock2, _ = world(trail=lambda: fake_trail({"signals": {"duration_s": sig(120, "d")}}))
    end_pr(other, merged_at="2026-10-01T10:00:00Z")
    clock2[0] += 99999
    rec2.sweep(force=True)
    p = stored(other)[0]["payload"]
    assert p["end_time_confidence"] == "high" and p["signals"]["duration_s"]["value"] == 120


# ---------------------------------------------------------------- redaction and size
def test_pii_and_secrets_are_scrubbed_everywhere_including_keys_and_branches():
    leaky = fake_trail({
        "pull_request": {"number": 7, "url": "https://github.com/org/app/pull/7", "branch": "fix/alice@example.com-/home/alice/work"},
        "reason": "see /Users/bob/code and C:\\Users\\carol\\x and token=" + "a" * 40,
        "signals": {"custom": {"value": 1, "reason": "mail dave@example.org", "password": "hunter2hunter2"},
                    "dave@example.org": sig(1, "key with an address")},
        "sessions": [{"session_id": "s1", "rule": "commit", "branch": "feat/erin@example.com"}]})
    repo, records, _, _ = world(trail=leaky)
    end_pr(repo)
    records.sweep(force=True)
    text = json.dumps(stored(repo)[0]["payload"])
    for secret in ("alice@example.com", "/home/alice", "bob", "carol", "a" * 40, "dave@example.org", "hunter2hunter2", "erin@example.com"):
        assert secret not in text, secret
    assert "[REDACTED]" in text and "[EMAIL]" in text and "/home/[USER]" in text and "/Users/[USER]" in text


def test_scrub_function_cases():
    assert scrub("a.b@c.io and /home/u1/x") == "[EMAIL] and /home/[USER]/x"
    assert scrub("C:\\Users\\Jo\\f") == "C:\\Users\\[USER]\\f"
    assert scrub("plain text") == "plain text"


def test_long_free_text_is_clipped_with_a_visible_marker_and_named_in_trimmed():
    repo, records, _, _ = world(trail=fake_trail({"pull_request": {"number": 7, "url": None, "branch": "b" * 500}}))
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert p["pull_request"]["branch"].endswith("…[truncated]") and len(p["pull_request"]["branch"]) <= 200 and "text:branch" in p["trimmed"]


def test_the_size_cap_is_hard_and_whole_lists_become_an_omitted_marker_in_a_defined_order():
    many = fake_trail({"sessions": [{"session_id": f"s{i}", "rule": "commit"} for i in range(60)],
                       "ci_runs": [{"run_number": i, "state": "success"} for i in range(1500)]})
    repo, records, _, _ = world(trail=many, evolution=lambda sid: {"sources": {}, "learning_record": {"blob": [1] * 5000}}, max_agents=50)
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert len(json.dumps(p, separators=(",", ":")).encode()) <= MAX_BYTES
    assert p["ci_runs"] == {"items": [], "omitted": 1500, "reason": f"dropped to keep the record under {MAX_BYTES} bytes"} and "ci_runs" in p["trimmed"]
    assert len(p["agents"]["items"]) == 50, "the agent allowlist leaves the 5000-element blob out, so agents still fit"
    assert "blob" not in json.dumps(p) and p["pull_request"]["number"] == 7


def test_every_list_field_has_one_shape_and_a_smaller_cap_drops_them_in_order(monkeypatch):
    many = fake_trail({"sessions": [{"session_id": f"s{i}", "rule": "commit"} for i in range(30)],
                       "ci_runs": [{"run_number": i, "state": "success"} for i in range(30)]})
    repo, records, _, _ = world(trail=many, max_agents=30)
    end_pr(repo)
    records.sweep(force=True)
    full = stored(repo)[0]["payload"]
    for field in ("sessions", "ambiguous", "handoffs", "reviews", "ci_runs", "agents"):
        assert set(full[field]) == {"items", "omitted", "reason"} and isinstance(full[field]["items"], list), field
    monkeypatch.setattr(learning_trails, "MAX_BYTES", 6000)
    repo2, records2, _, _ = world(trail=many, max_agents=30)
    end_pr(repo2)
    records2.sweep(force=True)
    p = stored(repo2)[0]["payload"]
    assert len(json.dumps(p, separators=(",", ":")).encode()) <= 6000
    order = [f for f in ("ci_runs", "handoffs", "reviews", "ambiguous", "sessions", "agents") if full[f]["items"]]
    dropped = [f for f in order if p[f]["items"] == [] and p[f]["omitted"] > 0]
    assert dropped and dropped == order[:len(dropped)], "lists go in the defined order, never out of it"
    for field in dropped:
        assert set(p[field]) == {"items", "omitted", "reason"} and field in p["trimmed"]


# ---------------------------------------------------------------- late evidence
def test_a_reported_end_time_after_an_observed_one_adds_one_late_evidence_event_and_leaves_the_record_alone():
    repo, records, _, _ = world()
    end_pr(repo)
    records.sweep(force=True)
    record = json.dumps(stored(repo)[0]["payload"], sort_keys=True)
    end_pr(repo, merged_at="2026-10-01T10:00:00Z")
    assert repo.get_pull_request("pr-1")["ended_source"] == "reported"
    records.sweep(force=True)
    records.sweep(force=True)
    (late,) = stored(repo, LATE)
    assert late["payload"]["evidence"]["merged_at"] == "2026-10-01T10:00:00Z" and late["payload"]["evidence"]["ended_source"] == "reported"
    assert json.dumps(stored(repo)[0]["payload"], sort_keys=True) == record


def test_late_evidence_keeps_only_whitelisted_keys_scrubs_them_and_needs_a_record():
    repo, records, _, _ = world()
    end_pr(repo)
    records.sweep(force=True)
    out = records.late_evidence("pr-1", 1, {"note": "token=" + "b" * 40 + " mail x@y.com", "evil": "x"})
    assert set(out["evidence"]) == {"note"} and "b" * 40 not in out["evidence"]["note"] and "x@y.com" not in out["evidence"]["note"]
    with pytest.raises(KeyError):
        records.late_evidence("pr-1", 9, {"note": "n"})
    with pytest.raises(ValueError):
        records.late_evidence("pr-1", 1, {"evil": "x"})


def test_a_signal_that_was_null_and_becomes_known_is_noted_once_and_only_a_bounded_number_of_times():
    state = {"signals": {"merged_without_rework": sig(None, "CI data is unavailable.")}}
    repo, records, clock, calls = world(trail=lambda: fake_trail({"signals": copy.deepcopy(state["signals"])}),
                                        late_check_interval_minutes=1, late_checks=2, low_confidence_after_minutes=10080)
    end_pr(repo)
    records.sweep(force=True)
    state["signals"] = {"merged_without_rework": sig(True, "reviewed, CI saw no later revision")}
    clock[0] += 120
    records.sweep(force=True)
    (late,) = stored(repo, LATE)
    assert late["payload"]["evidence"]["signals"] == {"merged_without_rework": sig(True, "reviewed, CI saw no later revision")}
    clock[0] += 120
    records.sweep(force=True)
    clock[0] += 120
    records.sweep(force=True)
    assert len(stored(repo, LATE)) == 1, "deduplicated by digest, and the check budget is spent"
    assert json.loads(json.dumps(stored(repo)[0]["payload"]))["signals"]["merged_without_rework"]["value"] is None, "the record is untouched"


def test_a_late_check_waits_for_all_sources_to_be_readable():
    state = {"sources": dict(OK), "signals": {"merged_without_rework": sig(None, "n")}}
    repo, records, clock, _ = world(trail=lambda: fake_trail({"signals": copy.deepcopy(state["signals"])}, sources=state["sources"]),
                                    late_check_interval_minutes=1, low_confidence_after_minutes=10080)
    end_pr(repo)
    records.sweep(force=True)
    state["sources"] = {**OK, "ci": "unavailable"}
    state["signals"] = {"merged_without_rework": sig(False, "now known")}
    clock[0] += 120
    records.sweep(force=True)
    assert stored(repo, LATE) == []


# ---------------------------------------------------------------- the index, ordering and concurrency
def test_the_sequence_advances_only_when_the_event_was_recorded():
    repo, records, _, _ = world()
    end_pr(repo)
    original = repo.record_event
    repo.record_event = lambda *a, **k: None
    assert records.sweep(force=True) == 0 and records._seq == -1
    repo.record_event = original
    assert records.sweep(force=True) == 1 and records._seq == 0


def test_a_counter_behind_the_stored_records_refuses_to_write():
    repo, records, _, _ = world()
    end_pr(repo)
    records.sweep(force=True)
    records._seq = -1
    repo.upsert_pull_request("pr-2", repository="github.com/org/app", number=8, status="merged", merged_at="2026-10-01T10:00:00Z")
    assert records.sweep(force=True) == 0 and len(repo.entity_ids(ENTITY)) == 1


def test_two_concurrent_sweeps_write_each_end_once():
    release = threading.Event()

    def slow():
        release.wait(2)
        return fake_trail()
    repo, records, _, calls = world(trail=slow)
    end_pr(repo)
    results = []
    threads = [threading.Thread(target=lambda: results.append(records.sweep(force=True))) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(5)
    assert sorted(results) == [0, 0, 1] and len(stored(repo)) == 1 and len(calls) == 1


def test_the_sweep_does_not_hold_the_lock_while_a_trail_is_built():
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(3)
        return fake_trail()
    repo, records, _, _ = world(trail=slow)
    end_pr(repo)
    worker = threading.Thread(target=lambda: records.sweep(force=True))
    worker.start()
    assert started.wait(2)
    began = time.monotonic()
    assert records.export()["records"] == [] and time.monotonic() - began < 0.5, "an export is not blocked by a trail build"
    release.set()
    worker.join(5)
    assert records.export()["count"] == 1


def seeded(n=5, **cfg):
    repo, records, clock, _ = world(page_default=2, page_max=3, **cfg)
    for i in range(n):
        repo.upsert_pull_request(f"pr-{i}", repository="github.com/org/app", number=10 + i, status="merged", merged_at="2026-10-01T10:00:00Z")
    records.sweep(force=True)
    return repo, records, clock


def test_export_pages_oldest_first_with_a_cursor_that_survives_appends():
    repo, records, clock = seeded(5)
    first = records.export()
    assert first["count"] == 2 and first["has_more"] and [r["learning_seq"] for r in first["records"]] == [0, 1]
    repo.upsert_pull_request("pr-late", repository="github.com/org/app", number=99, status="merged", merged_at="2026-10-02T10:00:00Z")
    clock[0] += 301
    records.sweep()
    second = records.export(cursor=first["next_cursor"], limit=3)
    assert [r["learning_seq"] for r in second["records"]] == [2, 3, 4] and second["has_more"]
    third = records.export(cursor=second["next_cursor"], limit=3)
    assert [r["pr_id"] for r in third["records"]] == ["pr-late"] and not third["has_more"]
    assert records.export(cursor=third["next_cursor"])["records"] == []


def test_export_since_filters_on_assembly_time_and_rejects_bad_parameters():
    repo, records, _ = seeded(2)
    assert records.export(since="2999-01-01T00:00:00Z")["records"] == []
    assert records.export(since="2000-01-01T00:00:00Z", limit=3)["count"] == 2
    for kw in ({"since": "yesterday"}, {"limit": 0}, {"limit": 4}, {"limit": True}, {"cursor": "abc"}, {"cursor": "s-1"}, {"cursor": 7}):
        with pytest.raises(ValueError):
            records.export(**kw)


def test_the_counter_continues_after_a_restart_from_the_stored_events():
    repo, records, _ = seeded(3)
    fresh = ControlPlaneRepo(now=lambda: NOW)
    fresh.import_state(repo.export_state())
    again = TrailRecords(fresh, learning_config({"warmup_minutes": 0, "page_default": 2, "page_max": 3}), trail_for=lambda *a: fake_trail(), evolution_for=fake_evolution, now=lambda: NOW)
    fresh.upsert_pull_request("pr-new", repository="github.com/org/app", number=50, status="merged", merged_at="2026-10-03T10:00:00Z")
    again.sweep(force=True)
    assert [r["learning_seq"] for r in again.export(limit=3, cursor="s1")["records"]] == [2, 3]


def test_a_page_of_a_large_store_is_fast_and_copies_only_the_page(monkeypatch):
    repo, records, _, _ = world(page_max=200)
    for n in range(2000):
        records._write(f"pr-{n}", {"kind": "pull_request_trail", "pr_id": f"pr-{n}", "end_seq": 1, "assembled_at": "2026-10-01T10:00:00+00:00"}, EVENT)
    records._ensure_index()

    def no_scan(*a, **k):
        raise AssertionError("an export must not read a stream")
    monkeypatch.setattr(repo, "stream", no_scan)
    monkeypatch.setattr(repo, "entity_ids", no_scan)
    copies = []
    real = learning_trails.copy.deepcopy
    monkeypatch.setattr(learning_trails.copy, "deepcopy", lambda v: copies.append(1) or real(v))
    began = time.monotonic()
    page = records.export(cursor="s1500", limit=50)
    assert time.monotonic() - began < 0.25 and page["count"] == 50 and len(copies) == 50 and page["records"][0]["learning_seq"] == 1501


def test_an_export_never_builds_a_trail():
    repo, records, _, calls = world()
    end_pr(repo)
    records.export()
    assert calls == []


# ---------------------------------------------------------------- retention
def test_retention_prunes_the_oldest_whole_prs_and_reports_where_the_export_now_starts():
    repo, records, _, _ = world(max_records=100, late_checks=0)
    for n in range(105):
        records._write(f"pr-{n}", {"kind": "pull_request_trail", "pr_id": f"pr-{n}", "end_seq": 1, "assembled_at": "2026-10-01T10:00:00+00:00"}, EVENT)
    assert records._prune() == 5
    assert len(records._records) == 100 and "pr-0" in repo.entity_ids(ENTITY), "the stored events are untouched; only the index and export are pruned"
    page = records.export(limit=3, cursor="s2")
    assert page["truncated_before"] == "s4" and page["cursor_truncated"] is True and page["records"][0]["learning_seq"] == 5
    assert records.export(limit=3, cursor="s9")["cursor_truncated"] is False
    assert records.export(limit=3)["truncated_before"] == "s4"


# ---------------------------------------------------------------- config and repository parsing
@pytest.mark.parametrize("raw,message", [
    ("x", "must be a mapping"), ({"nope": 1}, "unknown keys: nope"), ({"enabled": "yes"}, "enabled must be true or false"),
    ({"max_agents": 0}, "max_agents must be a whole number from 1 to 50"), ({"sweep_batch": True}, "sweep_batch"),
    ({"page_default": 300, "page_max": 100}, "page_default must not exceed page_max"), ({"require_role": "ingest"}, "require_role must be one of"),
    ({"max_records": 5}, "max_records"), ({"warmup_minutes": -1}, "warmup_minutes"), ({"retry_interval_minutes": 0}, "retry_interval_minutes"),
    ({"late_checks": 99}, "late_checks"), ({"unbuildable_retry_hours": 0}, "unbuildable_retry_hours"), ({"low_confidence_after_minutes": "x"}, "low_confidence_after_minutes")])
def test_config_is_validated(raw, message):
    with pytest.raises(ValueError, match=message):
        learning_config(raw)


def test_config_defaults_and_repository_parsing():
    assert learning_config(None) == {"enabled": True, "max_agents": 12, "sweep_batch": 20, "page_default": 50, "page_max": 200,
                                     "max_records": 20000, "require_role": "operator", "warmup_minutes": 10, "retry_interval_minutes": 5,
                                     "ready_retry_minutes": 60, "low_confidence_after_minutes": 15, "late_checks": 3,
                                     "late_window_hours": 24, "late_check_interval_minutes": 30, "max_build_attempts": 12,
                                     "unbuildable_retry_hours": 24}
    assert parse_repository("github.com/org/app") == ("github.com", "org", "app")
    assert parse_repository("org/app") == (None, "org", "app")
    assert parse_repository("https://ghe.example/org/app.git") == ("ghe.example", "org", "app")
    assert parse_repository("nonsense") is None and parse_repository(None) is None and parse_repository("a/b/c/d") is None


# ---------------------------------------------------------------- HTTP
class Client:
    def __init__(self, port, key=None):
        self.base, self.key = f"http://127.0.0.1:{port}", key

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={"X-API-Key": self.key} if self.key else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()


def make_api(sink, **learning):
    repo = ControlPlaneRepo(sink=sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True, learning={"page_default": 2, "page_max": 3, "warmup_minutes": 0, **learning})
    svr.learning_trails.trail_for = lambda *a: fake_trail()
    svr.learning_trails.evolution_for = fake_evolution
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    for n in range(4):
        repo.upsert_pull_request(f"pr-{n}", repository="github.com/org/app", number=7 + n, status="merged", branch="feat/x",
                                 merged_at="2026-10-01T10:00:00Z")
    return repo, svr


@pytest.fixture()
def api(acknowledged_sink):
    repo, svr = make_api(acknowledged_sink)
    svr.learning_trails.sweep(force=True)
    yield Client(svr.port), repo, svr
    svr.shutdown()


def test_the_endpoint_pages_with_a_cursor(api):
    c, repo, _ = api
    status, headers, body = c.get("/api/v1/learning/trails")
    out = json.loads(body)
    assert status == 200 and out["count"] == 2 and out["has_more"] and out["schema_version"] == 1 and out["truncated_before"] is None
    assert out["records"][0]["kind"] == "pull_request_trail" and out["records"][0]["pull_request"]["repository"] == "org/app"
    status, _, body = c.get(f"/api/v1/learning/trails?limit=3&cursor={out['next_cursor']}")
    nxt = json.loads(body)
    assert status == 200 and [r["learning_seq"] for r in nxt["records"]] == [2, 3] and not nxt["has_more"]


def test_json_and_jsonl_carry_the_same_content_and_the_jsonl_has_one_object_per_line_and_headers(api):
    c, _, _ = api
    status, headers, body = c.get("/api/v1/learning/trails.jsonl?limit=3")
    rows = [json.loads(line) for line in body.decode().splitlines()]
    assert status == 200 and headers["Content-Type"] == "application/x-ndjson" and headers["X-Has-More"] == "true" and len(rows) == 3
    assert headers["X-Next-Cursor"] == rows[-1]["cursor"] and body.endswith(b"\n") and headers["X-Cursor-Truncated"] == "false"
    assert rows == json.loads(c.get("/api/v1/learning/trails?limit=3")[2])["records"]
    status, headers, body = c.get(f"/api/v1/learning/trails.jsonl?cursor={headers['X-Next-Cursor']}")
    assert [json.loads(x)["learning_seq"] for x in body.decode().splitlines()] == [3] and headers["X-Has-More"] == "false"
    status, headers, body = c.get(f"/api/v1/learning/trails.jsonl?cursor={headers['X-Next-Cursor']}")
    assert status == 200 and body == b"" and headers["X-Next-Cursor"].startswith("s") and headers["X-Has-More"] == "false"


def test_the_export_is_redacted_once_at_write_time_and_not_again_when_ci_fix_is_unset(api):
    c, _, _ = api
    assert api[2].ci_fix is None
    text = c.get("/api/v1/learning/trails.jsonl?limit=3")[2].decode() + c.get("/api/v1/learning/trails?limit=3")[2].decode()
    assert "alice@example.com" not in text and "fix the parser" not in text


@pytest.mark.parametrize("query", ["limit=0", "limit=4", "limit=abc", "limit=", "limit=-1", "since=yesterday", "since=", "ended_after=soon", "ended_after=", "cursor=zzz", "cursor=s"])
def test_bad_parameters_are_400_on_both_forms(api, query):
    c, _, _ = api
    assert c.get(f"/api/v1/learning/trails?{query}")[0] == 400
    assert c.get(f"/api/v1/learning/trails.jsonl?{query}")[0] == 400


def test_a_read_starts_a_background_sweep_and_never_waits_for_a_trail_build(acknowledged_sink):
    repo, svr = make_api(acknowledged_sink)
    started, release = threading.Event(), threading.Event()

    def slow(*a):
        started.set()
        release.wait(5)
        return fake_trail()
    svr.learning_trails.trail_for = slow
    svr.learning_trails._swept = 0.0
    try:
        began = time.monotonic()
        status, _, body = Client(svr.port).get("/api/v1/learning/trails")
        assert status == 200 and json.loads(body)["records"] == [] and time.monotonic() - began < 2
        assert started.wait(3), "the sweep ran in the background"
    finally:
        release.set()
        svr.shutdown()


def test_disabled_is_404_and_unknown_subpaths_are_404(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True, learning={"enabled": False})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        c = Client(svr.port)
        assert c.get("/api/v1/learning/trails")[0] == 404 and c.get("/api/v1/learning/trails.jsonl")[0] == 404
        assert c.get("/api/v1/learning/other")[0] == 404
    finally:
        svr.shutdown()


def test_the_maintenance_sweep_writes_records_without_any_read(acknowledged_sink):
    repo, svr = make_api(acknowledged_sink)
    try:
        svr.learning_trails._swept = 0.0
        svr.sweep_once()
        assert len(repo.entity_ids(ENTITY)) == 4 and svr.learning_trails.export(limit=3)["count"] == 3
    finally:
        svr.shutdown()


def test_the_server_rejects_a_bad_learning_config(acknowledged_sink):
    with pytest.raises(ValueError, match="control_plane.learning"):
        ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), scheduler_admission=True, learning={"page_max": 0})


def test_access_defaults_to_the_operator_role_and_can_be_opened_to_viewers(acknowledged_sink):
    for role_config, viewer_status in (({}, 403), ({"require_role": "viewer"}, 200)):
        repo = ControlPlaneRepo(sink=acknowledged_sink)
        svr = ControlPlaneServer(repo, scheduler_admission=True, api_keys={"opkey": "operator", "viewkey": "viewer"},
                                 learning={"warmup_minutes": 0, **role_config})
        threading.Thread(target=svr.serve, daemon=True).start()
        svr.wait_until_ready()
        try:
            for path in ("/api/v1/learning/trails", "/api/v1/learning/trails.jsonl"):
                assert Client(svr.port).get(path)[0] == 401
                assert Client(svr.port, "wrong").get(path)[0] == 401
                assert Client(svr.port, "viewkey").get(path)[0] == viewer_status
                assert Client(svr.port, "opkey").get(path)[0] == 200
        finally:
            svr.shutdown()


# ---------------------------------------------------------------- retention never re-assembles; fair batches; permanent failure
def merged(repo, n, at="2026-10-01T10:00:00Z"):
    repo.upsert_pull_request(f"pr-{n}", repository="github.com/org/app", number=100 + n, status="merged", merged_at=at)


def test_pruned_prs_are_never_assembled_again_the_sequence_is_stable_and_a_restart_agrees():
    repo, records, clock, calls = world(max_records=100, late_checks=0)
    for n in range(120):
        merged(repo, n)
    for _ in range(12):
        records.sweep(force=True)
        clock[0] += 301
    top, built = records._seq, len(calls)
    assert top == 119 and len(records._records) == 100 and records.export(limit=1)["truncated_before"] == "s19"
    for _ in range(6):
        records.sweep(force=True)
        clock[0] += 301
    assert records._seq == top and len(calls) == built, "no pass writes or builds anything for a pruned PR"
    assert records.export(limit=1, cursor="s5")["cursor_truncated"] is True
    fresh = ControlPlaneRepo(now=lambda: clock[0])
    fresh.import_state(repo.export_state())
    for n in range(120):
        merged(fresh, n)
    calls2 = []
    again = TrailRecords(fresh, learning_config({"warmup_minutes": 0, "max_records": 100, "late_checks": 0}), now=lambda: clock[0],
                         trail_for=lambda *a: calls2.append(a) or fake_trail(), evolution_for=fake_evolution)
    again.sweep(force=True)
    assert again._seq == top and calls2 == [] and again.export(limit=1)["truncated_before"] == "s19"


def test_an_end_older_than_what_retention_already_dropped_is_not_assembled():
    repo, records, clock, calls = world(max_records=100, late_checks=0)
    for n in range(101):
        merged(repo, n, at="2026-10-05T10:00:00Z")
    for _ in range(7):
        records.sweep(force=True)
        clock[0] += 301
    assert len(records._records) == 100
    repo.upsert_pull_request("old", repository="github.com/org/app", number=999, status="merged", merged_at="2026-09-01T10:00:00Z")
    seen = len(calls)
    records.sweep(force=True)
    assert len(calls) == seen and not records._has_record("old", 1)


def test_the_batch_is_fair_so_postponed_prs_never_starve_newer_ones():
    unavailable = {**OK, "ci": "unavailable"}
    repo, records, clock, calls = world(trail=fake_trail(sources=unavailable), sweep_batch=20, late_checks=0)
    for n in range(200):
        merged(repo, n)
    for _ in range(10):
        records.sweep(force=True)
        clock[0] += 301
    assert len({c[3] for c in calls}) == 200, "ten passes reach all 200 PRs once, oldest and newest alike"
    assert len(calls) == 200


def test_a_trail_that_can_never_be_built_is_given_up_on_and_the_tracking_maps_stay_bounded():
    repo, records, clock, calls = world(trail=lambda: None, max_build_attempts=3)
    merged(repo, 1)
    for _ in range(8):
        records.sweep(force=True)
        clock[0] += 301
    assert len(calls) == 3 and records.unbuildable() == {("pr-1", 1): "the trail could not be built in 3 attempts"}
    assert repo.entity_ids(ENTITY) == [] and records._track == {}
    repo._pull_requests.pop("pr-1")
    records.sweep(force=True)
    assert records.unbuildable() == {} and records._track == {}


def test_without_a_ci_provider_ci_is_not_applicable_and_the_record_is_complete_at_once():
    unavailable = {**OK, "ci": "unavailable"}
    repo, records, _, _ = world(trail=fake_trail(sources=unavailable), evolution=lambda sid: fake_evolution(sid, {"ci": "unavailable"}))
    records.ci_configured = lambda: False
    end_pr(repo)
    assert records.sweep(force=True) == 1
    p = stored(repo)[0]["payload"]
    assert p["sources"]["ci"] == "not_applicable" and p["incomplete"] is False and p["missing_sources"] == []
    repo2, records2, _, _ = world(trail=fake_trail(sources=unavailable))
    records2.ci_configured = lambda: True
    end_pr(repo2)
    assert records2.sweep(force=True) == 0, "a configured but unreadable CI still waits"


# ---------------------------------------------------------------- scrub and allowlists
@pytest.mark.parametrize("raw,gone", [
    ("a%40b.com", "a%40b"), ("a＠b.com", "a"), ("a@localhost", "localhost"), ("/root/x", "/x"), ("%2Fhome%2Falice", "alice"),
    ("~alice/x", "alice"), ("C:/Users/bob/x", "bob"), ("C:\\Users\\bob\\x", "bob"), ("%252Fhome%252Fcarol", "carol")])
def test_scrub_bypasses_are_closed(raw, gone):
    out = scrub(raw)
    assert gone not in out.replace("[USER]", "").replace("[EMAIL]", ""), (raw, out)


def test_text_that_needs_nothing_is_kept_verbatim_and_identifiers_like_branches_are_not_hashed():
    for text in ("feat/john.smith", "fix/rootcause", "release-1.2", "ünïcode…ok"):
        assert scrub(text) == text


def test_a_cut_signal_reason_is_marked_and_named_in_trimmed():
    repo, records, _, _ = world(trail=fake_trail({"signals": {"custom": sig(1, "r" * 900)}}))
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert p["signals"]["custom"]["reason"].endswith("…[truncated]") and len(p["signals"]["custom"]["reason"]) <= 300
    assert "text:reason" in p["trimmed"]


def test_the_agent_record_is_an_allowlist_of_scalar_figures():
    evolution = lambda sid: {"sources": {}, "learning_record": {  # noqa: E731
        "session_id": sig("s1", "id"), "harness": sig("claude", "h"), "outcome": sig("finished", "o"),
        "tool_calls": {"total": sig(3, "t"), "secret_extra": sig(1, "x")}, "notes": "free text carol@example.com",
        "model": sig({"nested": "dict"}, "m"), "prs": [{"x": 1}], "handoffs_out": {"total": sig(1, "h"), "other": 5}}}
    repo, records, _, _ = world(evolution=evolution)
    end_pr(repo)
    records.sweep(force=True)
    rec = stored(repo)[0]["payload"]["agents"]["items"][0]["learning_record"]
    assert set(rec) == {"session_id", "harness", "model", "role", "started_at", "ended_at", "duration_s", "cost_usd_estimate", "outcome",
                        "tool_calls", "handoffs_in", "handoffs_out"}
    none = {"value": None, "reason": "not recorded"}
    assert rec["tool_calls"] == {"total": sig(3, "t"), "failed": none, "retried": none}
    assert rec["model"] == {"value": None, "reason": "a non-scalar value was not stored"} and set(rec["handoffs_out"]) == {"total", "rejected", "failed"}
    assert "carol" not in json.dumps(rec) and "secret_extra" not in json.dumps(rec)


# ---------------------------------------------------------------- filters, locks, lifecycle
def test_since_filters_on_assembly_time_and_ended_after_on_the_end_time():
    repo, records, _ = seeded(1)
    merged(repo, 50, at="2026-10-09T10:00:00Z")
    records.sweep(force=True)
    assert records.export(ended_after="2026-10-05T00:00:00Z")["count"] == 1
    assert records.export(ended_after="2026-09-01T00:00:00Z")["count"] == 2
    assert records.export(ended_after="2999-01-01T00:00:00Z")["records"] == []
    with pytest.raises(ValueError):
        records.export(ended_after="soon")


def test_a_reader_is_not_blocked_by_a_slow_durable_write():
    repo, records, _, _ = world()
    end_pr(repo)
    started, release = threading.Event(), threading.Event()
    original = repo.record_event

    def slow(*a, **k):
        started.set()
        release.wait(3)
        return original(*a, **k)
    repo.record_event = slow
    worker = threading.Thread(target=lambda: records.sweep(force=True))
    worker.start()
    assert started.wait(2)
    began = time.monotonic()
    assert records.export()["records"] == [] and time.monotonic() - began < 0.5
    release.set()
    worker.join(5)
    assert records.export()["count"] == 1


def test_a_server_with_nothing_ended_does_no_work_and_a_close_stops_the_background_sweep(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True, learning={"warmup_minutes": 0})
    built = []
    svr.learning_trails.trail_for = lambda *a: built.append(a)
    began = time.monotonic()
    svr.sweep_once()
    assert time.monotonic() - began < 2 and built == [] and svr.learning_trails.trigger_sweep() is False
    assert not [t for t in threading.enumerate() if t.name == "learning-trails-sweep"]
    merged(repo, 1)
    release = threading.Event()
    svr.learning_trails.trail_for = lambda *a: release.wait(5) or fake_trail()
    svr.learning_trails.evolution_for = fake_evolution
    svr.learning_trails._swept = 0.0
    assert svr.learning_trails.trigger_sweep() is True
    release.set()
    svr.learning_trails.close()
    assert svr.learning_trails._worker is not None and not svr.learning_trails._worker.is_alive()
    assert svr.learning_trails.sweep(force=True) == 0 and svr.learning_trails.trigger_sweep() is False


# ---------------------------------------------------------------- scrub: linear time, no false positives, closed bypasses
@pytest.mark.parametrize("make", [lambda: "a" * 1_000_000, lambda: "a." * 500_000, lambda: "%" * 1_000_000, lambda: "%25" * 330_000,
                                  lambda: "/home/" * 170_000, lambda: "a@" * 500_000, lambda: ("a" * 60 + ".") * 16_000])
def test_scrub_is_linear_on_hostile_input(make):
    text = make()
    began = time.monotonic()
    out = scrub(text)
    assert time.monotonic() - began < 0.2 and len(out) <= 4096


def test_clip_marks_an_input_that_was_capped_before_scrubbing():
    repo, records, _, _ = world(trail=fake_trail({"pull_request": {"number": 7, "url": None, "branch": "a" * 100_000}}))
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert p["pull_request"]["branch"].endswith("…[truncated]") and "text:branch" in p["trimmed"]


@pytest.mark.parametrize("text", ["lodash@4.17.21", "feat@v2", "git@github.com:org/repo.git", "ssh://git@github.com/org/repo.git",
                                  "pkg@1.2.3-beta.1", "@types/node", "bump react@18"])
def test_things_that_are_not_email_addresses_are_left_alone(text):
    assert scrub(text) == text


@pytest.mark.parametrize("text", ["a@b.com", "first.last+tag@sub.example.co.uk", "root@localhost", "x@LOCALHOST"])
def test_real_addresses_are_still_scrubbed(text):
    assert scrub(text) == "[EMAIL]"


@pytest.mark.parametrize("text,gone", [
    ("a​@b.com", "a"), ("‮a@b.com", "b.com"), ("/ho​me/alice", "alice"), ("/home‮/alice", "alice"), ("/ho­me/alice", "alice"),
    ("%2f%48ome%2falice", "alice"), ("%25252Fhome%25252Falice", "alice"), ("%2F%68ome%2F%61lice", "alice"),
    ("/home//alice", "alice"), ("/home/ alice", "alice"), ("/Users//bob", "bob"),
    ("alice (at) example.com", "alice"), ("alice [at] example [dot] com", "alice"), ("alice(AT)example.org", "alice")])
def test_remaining_bypasses_are_closed(text, gone):
    out = scrub(text)
    assert gone not in out.replace("[USER]", "").replace("[EMAIL]", ""), (text, out)


def test_a_url_is_not_rewritten_when_decoding_exposes_a_path_in_its_query():
    url = "https://x.example/cb?next=%2Fhome%2Falice&a=1"
    out = scrub(url)
    assert "alice" not in out and out.startswith("https://x.example/cb?next=%2Fhome%2F") and out.endswith("&a=1"), out
    mail = "https://x.example/u?e=bob%40example.com&z=2"
    out = scrub(mail)
    assert "bob" not in out and out.startswith("https://x.example/u?e=") and out.endswith("&z=2"), out


# ---------------------------------------------------------------- unbuildable: long retry, metadata, horizon count, idle cost, close
def test_an_unbuildable_pr_is_retried_daily_and_a_success_clears_it():
    state = {"ok": False}
    repo, records, clock, calls = world(trail=lambda: fake_trail() if state["ok"] else None, max_build_attempts=2, late_checks=0)
    merged(repo, 1)
    for _ in range(4):
        records.sweep(force=True)
        clock[0] += 301
    assert len(calls) == 2 and ("pr-1", 1) in records.unbuildable()
    clock[0] += 24 * 3600
    records.sweep(force=True)
    assert len(calls) == 3, "after the long retry it is tried once more"
    assert ("pr-1", 1) in records.unbuildable()
    clock[0] += 301
    records.sweep(force=True)
    assert len(calls) == 3, "and a failure starts the long wait again"
    state["ok"] = True
    clock[0] += 24 * 3600
    assert records.sweep(force=True) == 1 and records.unbuildable() == {} and records._track == {}


def test_the_export_reports_how_many_records_are_missing_and_how_many_were_skipped_by_the_horizon():
    repo, records, clock, _ = world(trail=lambda: None, max_build_attempts=1, late_checks=0, page_default=2, page_max=3)
    for n in range(30):
        merged(repo, n)
    for _ in range(2):
        records.sweep(force=True)
        clock[0] += 301
    meta = records.export()["unbuildable"]
    assert meta["count"] == 30 and len(meta["items"]) == 20 and set(meta["items"][0]) == {"pr_id", "end_seq", "reason", "retry_at"}
    assert records.export()["skipped_by_horizon"] == 0
    repo2, rec2, clock2, _ = world(max_records=100, late_checks=0)
    for n in range(101):
        merged(repo2, n, at="2026-10-05T10:00:00Z")
    for _ in range(7):
        rec2.sweep(force=True)
        clock2[0] += 301
    repo2.upsert_pull_request("old", repository="github.com/org/app", number=999, status="merged", merged_at="2026-09-01T10:00:00Z")
    rec2.sweep(force=True)
    assert rec2.export()["skipped_by_horizon"] == 1


def test_an_idle_read_costs_nothing_until_a_pr_changes_or_a_retry_comes_due(monkeypatch):
    repo, records, clock, _ = world()
    merged(repo, 1)
    records.sweep(force=True)
    clock[0] += 400
    scans = []
    original = repo.list_pull_requests
    monkeypatch.setattr(repo, "list_pull_requests", lambda *a, **k: scans.append(1) or original(*a, **k))
    assert records.trigger_sweep() is False
    first = len(scans)
    for _ in range(50):
        assert records.trigger_sweep() is False
    assert len(scans) == first, "nothing moved, so no scan"
    merged(repo, 2)
    clock[0] += 400
    assert records.trigger_sweep() is True
    records._worker.join(5)
    assert len(scans) > first


def test_a_sweep_that_is_closed_while_it_builds_writes_nothing():
    repo, records, _, _ = world()

    def closing():
        records._closed = True
        return fake_trail()
    records.trail_for = lambda *a: closing()
    end_pr(repo)
    assert records.sweep(force=True) == 0 and stored(repo) == []
