"""Review prompt v2 and the replay harness of deploy/ci-review/always-on-review.py.

Same approach as test_always_on_review.py: a real temporary git origin and clone, stand-in `gh`/`claude`/`pi`
executables on PATH that log their calls and answer from a JSON script, real YAML and JSON files. Scoring tests use
fixture findings files from tests/ci_review/fixtures. Nothing here calls a model.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CI = ROOT / "deploy/ci-review"
FIXTURES = HERE / "fixtures"
CORPUS = HERE / "corpus"


def _ta():
    spec = importlib.util.spec_from_file_location("ta_always_on", HERE / "test_always_on_review.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ta_always_on"] = mod
    spec.loader.exec_module(mod)
    return mod


TA = _ta()
load, block, APPROVE, REPO = functools.cache(TA.load), TA.block, TA.APPROVE, TA.REPO


@pytest.fixture
def env(tmp_path, monkeypatch):
    return TA.Env(tmp_path, monkeypatch)


def system_of(call: dict) -> str:
    return call["argv"][call["argv"].index("--append-system-prompt") + 1]


def logs(env) -> list[dict]:
    return [json.loads(x) for x in (env.tmp / "state/reviews.jsonl").read_text().splitlines()]


def with_evidence(answer: dict, **kw) -> dict:
    f = {**answer["findings"][0], **kw}
    return {**answer, "findings": [f]}


# ---------------- 1. project rules ----------------

RULES = ["TDD: every change carries a test", "No mocks, stubs or fakes"]


def test_project_rules_reach_both_tiers_as_system_prompt_only(env):
    env.config("prompt: {project_rules: " + json.dumps(RULES) + "}\n")
    assert env.review("tier1") == 0 and env.review("tier2") == 0
    calls = env.reviewer_calls()
    assert {c["tool"] for c in calls} == {"pi", "claude"}
    for c in calls:
        s = system_of(c)
        assert all(r in s for r in RULES) and not any(r in c["stdin"] for r in RULES)
        assert "PROJECT RULES" in s


def test_no_rules_means_no_rules_section(env):
    env.review("tier2")
    assert "PROJECT RULES" not in system_of(env.reviewer_calls()[0])


@pytest.mark.parametrize("bad", ['"a string"', "[1]", '[""]', '["' + "x" * 501 + '"]'])
def test_project_rules_are_validated(env, bad):
    env.config(f"prompt: {{project_rules: {bad}}}\n")
    assert env.review("tier2") == 2


def test_the_example_config_ships_the_operators_rules():
    cfg = load().load_config(str(CI / "always-on-review.example.yaml"))
    rules = " ".join(cfg["prompt"]["project_rules"]).lower()
    for needle in ("tdd", "mock", "environment variable", "migration", "parameterized", "any", "messagepack", "secret",
                   "mda", "changelog", "restart"):
        assert needle in rules, needle


# ---------------- 2. the checklist ----------------

def checklist(tmp: Path, body: dict | str) -> Path:
    p = tmp / "checklist.yaml"
    p.write_text(body if isinstance(body, str) else yaml.safe_dump(body))
    return p


GOOD = {"version": 1, "categories": {"code": [{"id": "global-state", "question": "Does a test leak global state?"}],
                                     "migrations": [{"id": "rollback", "question": "Can the migration be undone?"}]}}


def test_shipped_checklist_is_valid_and_covers_every_category_and_the_seeded_misses():
    m = load()
    items = m.load_checklist(CI / "review-checklist.yaml")
    assert {c for c, _, _ in items} == {"code", "security", "tests", "docs", "performance", "migrations"}
    ids = [i for _, i, _ in items]
    assert len(ids) == len(set(ids)) and len(items) >= 15
    text = " ".join(q.lower() for _, _, q in items)
    for needle in ("order", "global state", "default", "version", "bound", "startup", "wiring", "restart", "changelog",
                   "rollback", "unique", "degrade", "partial index", "leak", "snapshot", "quot", "permission",
                   "concurren", "idempot"):
        assert needle in text, needle
    cfg = m.load_config(str(CI / "always-on-review.example.yaml"))
    assert len(cfg["prompt"]["checklist"]) == len(items)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),                                              # unknown top key
    lambda d: d["categories"].update(style=[{"id": "x1", "question": "q"}]),  # unknown category
    lambda d: d["categories"]["code"][0].update(extra=1),                     # unknown item key
    lambda d: d["categories"]["code"].append({"id": "global-state", "question": "dup id"}),
    lambda d: d["categories"]["code"].append({"id": "Bad Id!", "question": "q"}),
    lambda d: d["categories"]["code"].append({"id": "multi-line", "question": "two\nlines"}),
    lambda d: d["categories"]["code"].append({"id": "no-question"}),
    lambda d: d.update(version=2),
    lambda d: d.update(categories=[]),
])
def test_checklist_validation_refuses(tmp_path, mutate):
    import copy
    d = copy.deepcopy(GOOD)
    mutate(d)
    with pytest.raises(load().Refused):
        load().load_checklist(checklist(tmp_path, d))


def test_checklist_unreadable_or_not_yaml_is_refused(tmp_path):
    m = load()
    with pytest.raises(m.Refused):
        m.load_checklist(tmp_path / "missing.yaml")
    with pytest.raises(m.Refused):
        m.load_checklist(checklist(tmp_path, "[unclosed"))


def test_checklist_reaches_the_prompt_and_asks_for_checked_items(env):
    cl = checklist(env.tmp, GOOD)
    env.config(f"prompt: {{checklist_file: {cl}}}\n")
    assert env.review("tier2") == 0
    s = system_of(env.reviewer_calls()[0])
    assert "global-state" in s and "Does a test leak global state?" in s and "rollback" in s
    assert "checked_items" in s


def test_relative_checklist_path_is_relative_to_the_config_file(env):
    checklist(env.tmp, GOOD)
    env.config("prompt: {checklist_file: checklist.yaml}\n")
    assert env.review("tier2") == 0 and "global-state" in system_of(env.reviewer_calls()[0])


def test_a_bad_checklist_in_the_config_refuses_the_run(env):
    cl = checklist(env.tmp, {**GOOD, "bogus": 1})
    env.config(f"prompt: {{checklist_file: {cl}}}\n")
    assert env.review("tier2") == 2 and env.reviewer_calls() == []


def test_checked_items_are_accepted_logged_and_optional(env):
    ans = {**APPROVE, "checked_items": ["global-state", "rollback"]}
    env.reviewer("claude", answer=ans)
    assert env.review("tier2") == 0
    assert logs(env)[0]["checked_items"] == ["global-state", "rollback"]


# ---------------- 3. investigation ----------------

def test_claude_is_told_to_read_callers_tests_and_schemas_before_the_verdict(env):
    env.review("tier2")
    s = system_of(env.reviewer_calls()[0]).lower()
    assert "callers of every changed function" in s and "existing tests" in s and "migrations" in s
    assert "judge from the diff alone" in s


def test_pi_prompt_has_no_claude_investigation_orders(env):
    env.review("tier1")
    assert "callers of every changed function" not in system_of(env.reviewer_calls()[0]).lower()


def test_turns_are_logged_only_when_the_backend_reports_them(env):
    env.review("tier2")
    assert "turns" not in logs(env)[0]["usage"]
    env.reviewer("claude", answer=APPROVE, usage={"input_tokens": 1, "output_tokens": 1}, num_turns=7)
    env.review("tier2", "--force")
    assert logs(env)[1]["usage"]["turns"] == 7


# ---------------- 4. the evidence rule ----------------

def test_schema_has_the_new_optional_fields_and_old_answers_still_validate():
    m = load()
    props = m.FINDING_SCHEMA["properties"]
    assert "evidence" in props and "how_it_fails" in props and "evidence" not in m.FINDING_SCHEMA["required"]
    assert "checked_items" in m.VERDICT_SCHEMA["properties"] and "checked_items" not in m.VERDICT_SCHEMA["required"]
    old = {"head_sha": "a" * 40, "verdict": "block", "checked": ["code"],
           "findings": [{"file": "a", "line": 1, "severity": "blocking", "category": "code", "why": "x"}]}
    v = m.parse_verdict(old, "a" * 40)
    assert v.findings[0].evidence == ""
    old["findings"][0]["evidence"] = 5
    with pytest.raises(m.ReviewFailed):
        m.parse_verdict(old, "a" * 40)


def test_a_blocker_whose_evidence_is_in_the_diff_still_blocks(env):
    env.reviewer("claude", answer=block())
    env.config(post=True)
    assert env.review("tier2", post=True) == 0
    assert env.statuses_posted()[-1]["state"] == "failure"
    assert logs(env)[0]["downgraded"] == 0


@pytest.mark.parametrize("ev", [None, "", "x = totally_made_up(call)", "  return\n   eval( x )  ".replace("( x )", "(x)")])
def test_evidence_missing_or_invented_downgrades_the_blocker_to_a_note(env, ev):
    f = block()["findings"][0]
    if ev is None:
        f.pop("evidence")
    else:
        f["evidence"] = ev
    if ev == "  return\n   eval(x)  ":
        pytest.skip("whitespace variant is the passing case, tested below")
    env.reviewer("claude", answer={**block(), "findings": [f]})
    env.config(post=True)
    assert env.review("tier2", post=True) == 0
    assert env.statuses_posted()[-1]["state"] == "success"
    assert env.gh_state().get("comments", []) == []
    assert logs(env)[0]["downgraded"] == 1 and logs(env)[0]["blocking"] == 0


def test_evidence_is_whitespace_normalised(env):
    env.reviewer("claude", answer=with_evidence(block(), evidence="  return \n\t eval(x) "))
    env.config(post=True)
    env.review("tier2", post=True)
    assert env.statuses_posted()[-1]["state"] == "failure"


def test_apply_evidence_accepts_a_quote_from_the_file_at_head_but_not_the_diff(tmp_path):
    m = load()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("def f(x):\n    helper(x)\n    return x\n")
    f = m.Finding("src/a.py", 2, "blocking", "code", "w", "def f(x):\n    helper(x)", "x=1 -> crash")
    v = m.Verdict("s", "block", (f,), ("code",))
    out, n = m.apply_evidence(v, "diff --git a/x b/x\n+unrelated line here\n", tmp_path)
    assert n == 0 and out.findings[0].severity == "blocking"
    out, n = m.apply_evidence(v, "diff --git a/x b/x\n", None)                      # no worktree: the diff only
    assert n == 1 and out.findings[0].severity == "note" and out.findings[0].downgraded


def test_apply_evidence_ignores_diff_line_prefixes_and_refuses_escaping_paths(tmp_path):
    m = load()
    diff = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n context line\n+    first_added(1)\n+    second_added(2)\n"
    two = m.Finding("a.py", 2, "blocking", "code", "w", "first_added(1)\nsecond_added(2)", "h")
    out, n = m.apply_evidence(m.Verdict("s", "block", (two,), ("code",)), diff, None)
    assert n == 0
    (tmp_path / "wt").mkdir()
    (tmp_path / "secret.txt").write_text("outside the worktree text")
    esc = m.Finding("../secret.txt", 1, "blocking", "code", "w", "outside the worktree text", "h")
    out, n = m.apply_evidence(m.Verdict("s", "block", (esc,), ("code",)), "", tmp_path / "wt")
    assert n == 1


def test_notes_need_no_evidence_and_the_rule_can_be_switched_off(env):
    note = {**block(), "verdict": "approve", "findings": [{**block()["findings"][0], "severity": "note",
                                                          "evidence": ""}]}
    env.reviewer("claude", answer=note)
    assert env.review("tier2") == 0 and logs(env)[0]["downgraded"] == 0
    f = block()["findings"][0]
    f.pop("evidence")
    env.reviewer("claude", answer={**block(), "findings": [f]})
    env.config("prompt: {require_evidence: false}\n", post=True)
    env.review("tier2", "--force", post=True)
    assert env.statuses_posted()[-1]["state"] == "failure"


def test_the_prompt_asks_for_evidence_and_how_it_fails(env):
    env.review("tier2")
    s = system_of(env.reviewer_calls()[0])
    assert '"evidence"' in s and '"how_it_fails"' in s and "exact quote" in s


# ---------------- 5. multiple passes ----------------

PASSES = ("tiers: {tier2: {passes: [{name: general, focus: 'Look at everything.'}, "
          "{name: tests, focus: 'Only test quality.'}, {name: security, focus: 'Only security.'}]}}\n")


def test_each_pass_is_one_sequential_invocation_with_its_own_focus(env):
    env.config(PASSES)
    assert env.review("tier2") == 0
    calls = env.reviewer_calls()
    assert len(calls) == 3
    for c, focus in zip(calls, ("Look at everything.", "Only test quality.", "Only security.")):
        assert focus in system_of(c)
        assert [f for f in ("Look at everything.", "Only test quality.", "Only security.") if f in system_of(c)] == [focus]
    fences = {c["stdin"].splitlines()[0] for c in calls}
    assert len(fences) == 3                                                      # a fresh fence per invocation
    rec = logs(env)[0]
    assert [p["name"] for p in rec["passes"]] == ["general", "tests", "security"]
    assert all("duration_s" in p and p["cost_usd"] == 0.5 for p in rec["passes"])
    assert rec["usage"]["cost_usd"] == 1.5


def test_passes_empty_or_unset_behave_like_a_single_unfocused_review(env):
    for extra in ("", "tiers: {tier2: {passes: []}}\n"):
        env.config(extra)
        env.review("tier2", "--force")
    calls = env.reviewer_calls()
    assert len(calls) == 2 and all("FOCUS FOR THIS PASS" not in system_of(c) for c in calls)
    assert len(logs(env)[0]["passes"]) == 1


def test_a_single_pass_is_one_invocation(env):
    env.config("tiers: {tier2: {passes: [{name: only, focus: 'Just this.'}]}}\n")
    env.review("tier2")
    assert len(env.reviewer_calls()) == 1 and "Just this." in system_of(env.reviewer_calls()[0])


def test_one_blocking_pass_blocks_and_duplicate_findings_merge(env):
    env.config(PASSES, post=True)
    env.reviewer("claude", answer=block())
    assert env.review("tier2", post=True) == 0
    assert env.statuses_posted()[-1]["state"] == "failure"
    rec = logs(env)[0]
    assert rec["blocking"] == 1 and len(env.reviewer_calls()) == 3               # three identical findings, one kept


def test_merge_verdicts_dedupes_by_file_line_category_and_keeps_the_stronger_severity():
    m = load()
    mk = lambda sev, why: m.Finding("a.py", 3, sev, "code", why, "e" * 8, "h")
    a = m.Verdict("s", "approve", (mk("note", "first"),), ("code",), ("x",))
    b = m.Verdict("s", "block", (mk("blocking", "second"), m.Finding("a.py", 3, "note", "tests", "t")), ("tests",), ("y",))
    out = m.merge_verdicts([a, b], "s")
    assert [(f.category, f.severity) for f in out.findings] == [("code", "blocking"), ("tests", "note")]
    assert out.verdict == "block" and set(out.checked) == {"code", "tests"} and set(out.checked_items) == {"x", "y"}
    only = m.merge_verdicts([a], "s")
    assert only == a


def test_a_pass_whose_blockers_are_all_downgraded_does_not_block(env):
    f = block()["findings"][0]
    f.pop("evidence")
    env.reviewer("claude", answer={**block(), "findings": [f]})
    env.config(PASSES, post=True)
    env.review("tier2", post=True)
    assert env.statuses_posted()[-1]["state"] == "success" and logs(env)[0]["downgraded"] == 3


def test_the_time_cap_covers_all_passes_together(env):
    env.reviewer("claude", answer=APPROVE, sleep=4)
    env.config("tiers: {tier2: {time_cap_s: 10, passes: [{name: a, focus: A}, {name: b, focus: B}, {name: c, focus: C}]}}\n",
               post=True)
    assert env.review("tier2", post=True) == 1                                   # 3 x 4 s > 10 s; stays pending
    assert [s for s in env.statuses_posted() if s["state"] in ("success", "failure")] == []
    assert len(env.reviewer_calls()) == 3 and "time cap" in logs(env)[-1]["why"]


def test_a_failing_pass_fails_the_whole_review(env):
    env.reviewer("claude", answer=APPROVE, exit=3)
    env.config(PASSES)
    assert env.review("tier2") == 1 and len(env.reviewer_calls()) == 1


def test_passes_are_validated(env):
    for bad in ("passes: [{name: x}]", "passes: [{name: x, focus: y, extra: 1}]", "passes: [{name: '', focus: y}]",
                "passes: [{name: x, focus: y}, {name: x, focus: z}]", "passes: [1]"):
        env.config(f"tiers: {{tier2: {{{bad}}}}}\n")
        assert env.review("tier2") == 2, bad


def test_the_example_config_ships_four_passes():
    cfg = load().load_config(str(CI / "always-on-review.example.yaml"))
    assert [p["name"] for p in cfg["tiers"]["tier2"]["passes"]] == ["general", "tests", "security", "migrations"]


# ---------------- 6. the replay harness: corpus format, scoring ----------------

def findings(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())["findings"]


def case(name: str) -> dict:
    return load().load_case(CORPUS / name)


def test_the_seeded_cases_are_valid_and_complete():
    m = load()
    c733, c782 = case("pr733.json"), case("pr782.json")
    assert c733["head_sha"] == "7225e12dd9bdb174ed816285902a3f50c1d58c5b" and len(c733["ground_truth"]) == 7
    assert c782["head_sha"] == "b36058ff18f625d1c900b1a0217201d6b097d131" and len(c782["ground_truth"]) == 9
    assert {g["severity"] for g in c782["ground_truth"]} == {"note"}
    assert c733["source"] == c782["source"] == "opus"
    claims = " ".join(c["summary"].lower() for c in c782["known_false_claims"])
    assert "quoted" in claims and "to_regclass" in claims and "limit+1" in claims
    assert all(g["category"] in m.CATEGORIES for c in (c733, c782) for g in c["ground_truth"])


@pytest.mark.parametrize("mutate", [
    lambda c: c.pop("repo"), lambda c: c.update(pr="7"), lambda c: c.update(head_sha="abc"),
    lambda c: c.update(source="gut-feeling"), lambda c: c.update(extra=1), lambda c: c.update(ground_truth={}),
    lambda c: c["ground_truth"][0].pop("summary"), lambda c: c["ground_truth"][0].update(severity="fatal"),
    lambda c: c["ground_truth"][0].update(line="x"), lambda c: c["ground_truth"][0].update(category="style"),
    lambda c: c.update(known_false_claims=[{"id": "x"}]),
])
def test_load_case_refuses_malformed_cases(tmp_path, mutate):
    c = json.loads((CORPUS / "pr733.json").read_text())
    mutate(c)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(c))
    with pytest.raises(load().Refused):
        load().load_case(p)


def results(score: dict) -> dict[str, str]:
    return {i["id"]: i["result"] for i in score["items"]}


def test_score_found_partial_missed_for_pr733():
    m = load()
    s = m.score_case(case("pr733.json"), findings("pr733-strong.json"))
    r = results(s)
    assert list(r.values()).count("FOUND") >= 5 and "MISSED" in r.values()
    assert s["found"] + s["partial"] + s["missed"] == 7 and s["recall"] == s["found"] / 7
    weak = results(m.score_case(case("pr733.json"), findings("pr733-weak.json")))
    assert "PARTIAL" in weak.values()                                          # right file, nothing else shared
    none = m.score_case(case("pr733.json"), [])
    assert none["recall"] == 0 and set(results(none).values()) == {"MISSED"} and none["false_blocker_rate"] is None


def test_found_by_line_proximity_or_by_two_keywords():
    m = load()
    gt = {"id": "g", "file": "a.py", "line": 100, "category": "code", "summary": "dedup uses event id alone",
          "severity": "note"}
    c = {"ground_truth": [gt], "known_false_claims": []}
    f = lambda line, why: [{"file": "a.py", "line": line, "severity": "note", "category": "code", "why": why}]
    assert m.score_case(c, f(110, "something else"))["found"] == 1               # within 10
    assert m.score_case(c, f(111, "something else"))["partial"] == 1
    assert m.score_case(c, f(500, "the dedup keys on event id"))["found"] == 1   # two keywords
    assert m.score_case(c, f(500, "the event is fine"))["partial"] == 1          # one keyword is not enough
    assert m.score_case(c, f(None, "dedup of the event id"))["found"] == 1       # no line: keywords only
    other = [{"file": "b.py", "line": 100, "severity": "note", "category": "code", "why": "dedup event id alone"}]
    assert m.score_case(c, other)["missed"] == 1                                 # other file never matches


def test_null_line_ground_truth_needs_keywords():
    m = load()
    c = {"ground_truth": [{"id": "g", "file": "CHANGELOG.md", "line": None, "category": "docs",
                           "summary": "gen2 default change undocumented", "severity": "blocking"}],
         "known_false_claims": []}
    near = [{"file": "CHANGELOG.md", "line": 3, "severity": "note", "category": "docs", "why": "unrelated"}]
    assert m.score_case(c, near)["partial"] == 1
    yes = [{"file": "CHANGELOG.md", "line": 3, "severity": "note", "category": "docs",
            "why": "the gen2 default change is not documented"}]
    assert m.score_case(c, yes)["found"] == 1


def test_stopwords_do_not_count_as_keywords():
    m = load()
    c = {"ground_truth": [{"id": "g", "file": "a.py", "line": None, "category": "code",
                           "summary": "the is not and when this", "severity": "note"}], "known_false_claims": []}
    f = [{"file": "a.py", "line": 1, "severity": "note", "category": "code", "why": "the is not and when this"}]
    assert m.score_case(c, f)["partial"] == 1


def test_unmatched_blocking_findings_are_false_blocker_candidates_never_confirmed():
    m = load()
    s = m.score_case(case("pr733.json"), findings("pr733-strong.json"))
    labels = {f["label"] for f in s["findings"]}
    assert "FALSE_BLOCKER_CANDIDATE" in labels and "FALSE_BLOCKER" not in labels
    assert s["candidates"] == 1 and s["false_blockers"] == 0
    assert s["blocking"] >= 2 and s["false_blocker_rate"] == s["candidates"] / s["blocking"]
    notes_only = [{**f, "severity": "note"} for f in findings("pr733-strong.json")]
    assert m.score_case(case("pr733.json"), notes_only)["false_blocker_rate"] is None   # no blocking findings


def test_a_blocker_matching_a_known_false_claim_is_a_confirmed_false_blocker():
    m = load()
    s = m.score_case(case("pr782.json"), findings("pr782-false-claims.json"))
    assert s["false_blockers"] == 3 and s["candidates"] == 1
    assert any(f["label"] == "FALSE_BLOCKER" and f["claim"] for f in s["findings"])
    # a note repeating a false claim is not a blocker, so it is never labelled a false blocker
    as_notes = [{**f, "severity": "note"} for f in findings("pr782-false-claims.json")]
    assert m.score_case(case("pr782.json"), as_notes)["false_blockers"] == 0


def test_a_real_blocker_near_a_false_claim_line_is_not_swept_up_by_proximity():
    m = load()
    f = [{"file": "herdr_engineering/control_plane/bridge.py", "line": 401, "severity": "blocking", "category": "tests",
          "why": "PostgresSource.inspector_page path has no test"}]
    s = m.score_case(case("pr782.json"), f)
    assert s["false_blockers"] == 0 and s["found"] >= 1 and s["candidates"] == 0


def test_unconfirmed_ground_truth_is_not_scored():
    m = load()
    c = {"ground_truth": [
        {"id": "a", "file": "a.py", "line": 1, "category": "code", "summary": "alpha beta bug", "severity": "note"},
        {"id": "b", "file": "a.py", "line": 50, "category": "code", "summary": "gamma delta bug", "severity": "note",
         "confirmed": False}], "known_false_claims": []}
    s = m.score_case(c, [])
    assert s["scored_items"] == 1 and s["unconfirmed"] == 1 and len(s["items"]) == 1


def test_aggregate_and_per_category_recall():
    m = load()
    a = m.score_case(case("pr733.json"), findings("pr733-strong.json"))
    b = m.score_case(case("pr782.json"), findings("pr782-false-claims.json"))
    agg = m.aggregate([{**a, "state": "scored", "cost_usd": 1.0, "duration_s": 10.0},
                       {**b, "state": "scored", "cost_usd": 0.5, "duration_s": 5.0},
                       {"state": "failed", "id": "x"}])
    assert agg["cases_scored"] == 2 and agg["cases_failed"] == 1
    assert agg["items"] == 16 and agg["found"] == a["found"] + b["found"]
    assert agg["recall"] == agg["found"] / 16
    assert agg["cost_usd"] == 1.5 and agg["duration_s"] == 15.0
    assert set(agg["by_category"]) >= {"tests", "docs", "code"}
    assert agg["by_category"]["tests"]["total"] == sum(
        1 for g in case("pr733.json")["ground_truth"] + case("pr782.json")["ground_truth"] if g["category"] == "tests")
    assert agg["false_blockers"] == b["false_blockers"] and agg["candidates"] == a["candidates"] + b["candidates"]


# ---------------- 6b. replay end to end ----------------

def write_case(corpus: Path, env, name="case7", **kw) -> Path:
    corpus.mkdir(exist_ok=True)
    c = {"repo": REPO, "pr": 7, "head_sha": env.head, "base_sha": env.base, "source": "opus", "notes": "t",
         "ground_truth": [{"id": "eval", "file": "src/a.py", "line": 2, "category": "security",
                           "summary": "eval of user input allows code execution", "severity": "blocking"},
                          {"id": "docs", "file": "README.md", "line": None, "category": "docs",
                           "summary": "missing documentation of the helper", "severity": "note"}], **kw}
    p = corpus / f"{name}.json"
    p.write_text(json.dumps(c))
    return p


def replay(env, corpus, *extra, tier="tier2", out=None):
    out = out or env.tmp / "report.json"
    rc = env.mod.main(["replay", "--corpus", str(corpus), "--tier", tier, "--config", str(env.cfg_path),
                       "--out", str(out), *extra])
    return rc, out


def test_replay_runs_the_tier_in_a_detached_worktree_scores_and_posts_nothing(env):
    env.config(post=True)                                              # even `post: true` cannot make replay post
    env.reviewer("claude", answer=block())
    write_case(env.tmp / "corpus", env)
    rc, out = replay(env, env.tmp / "corpus", "--post")
    assert rc == 0
    rep = json.loads(out.read_text())
    (c,) = rep["cases"]
    assert c["state"] == "scored" and c["found"] == 1 and c["missed"] == 1 and c["recall"] == 0.5
    assert c["cost_usd"] == 0.5 and c["duration_s"] >= 0 and c["id"] == "case7"
    assert rep["tier"] == "tier2" and rep["aggregate"]["recall"] == 0.5
    (call,) = env.reviewer_calls()
    assert call["head"] == env.head and call["porcelain"] == "" and "/worktrees/" in call["cwd"]
    assert not Path(call["cwd"]).exists() and len(env.worktrees()) == 1
    assert env.worktrees() and TA.sh("git", "for-each-ref", "refs/herdr-review", cwd=env.clone) == ""
    assert not (env.bin / "gh.log").exists()                           # replay never even calls gh
    assert not (env.tmp / "state/state.json").exists()                 # nor does it touch review state
    assert "--post" not in " ".join(call["argv"])


def test_replay_can_never_post_even_if_asked_to_inside_it(env):
    m = env.mod
    gh = m.ai.Gh(dry_run=True)
    v = m.parse_verdict({**APPROVE, "head_sha": "a" * 40}, "a" * 40)
    d = m.decide(v, {}, "x")
    with m.replay_guard():
        with pytest.raises(m.Refused):
            m.post_status(gh, REPO, "a" * 40, "Opus agent review", d)
        with pytest.raises(m.Refused):
            m.upsert_comment(gh, REPO, 7, "Opus agent review", "body")
        with pytest.raises(m.Refused):
            m.review_tier(env.mod.load_config(str(env.cfg_path)), REPO, 7, "tier2", True)
    assert gh.posted == []
    m.post_status(gh, REPO, "a" * 40, "Opus agent review", d)            # the guard is lifted afterwards
    assert gh.posted


def test_replay_is_resumable_and_skips_scored_cases(env):
    env.reviewer("claude", answer=block())
    corpus = env.tmp / "corpus"
    write_case(corpus, env, "case-a")
    rc, out = replay(env, corpus, "--limit", "1")
    assert rc == 0 and len(json.loads(out.read_text())["cases"]) == 1 and len(env.reviewer_calls()) == 1
    write_case(corpus, env, "case-b")
    rc, out = replay(env, corpus)
    ids = [c["id"] for c in json.loads(out.read_text())["cases"]]
    assert ids == ["case-a", "case-b"] and len(env.reviewer_calls()) == 2         # case-a was not run again


def test_limit_counts_new_cases_not_skipped_ones(env):
    corpus = env.tmp / "corpus"
    for n in ("a", "b", "c"):
        write_case(corpus, env, f"case-{n}")
    replay(env, corpus, "--limit", "1")
    replay(env, corpus, "--limit", "1")
    assert len(env.reviewer_calls()) == 2


def test_a_failed_case_is_recorded_and_retried_on_the_next_run(env):
    corpus = env.tmp / "corpus"
    write_case(corpus, env)
    env.reviewer("claude", answer=APPROVE, exit=2)
    rc, out = replay(env, corpus)
    rep = json.loads(out.read_text())
    assert rc == 1 and rep["cases"][0]["state"] == "failed" and rep["aggregate"]["cases_failed"] == 1
    env.reviewer("claude", answer=block())
    rc, out = replay(env, corpus)
    rep = json.loads(out.read_text())
    assert rc == 0 and [c["state"] for c in rep["cases"]] == ["scored"]


def test_replay_uses_the_configured_passes_and_prompt(env):
    env.config(PASSES + "prompt: {project_rules: " + json.dumps(RULES) + "}\n")
    write_case(env.tmp / "corpus", env)
    replay(env, env.tmp / "corpus")
    calls = env.reviewer_calls()
    assert len(calls) == 3 and all(RULES[0] in system_of(c) for c in calls)


def test_replay_one_at_a_time_lock(env):
    m = env.mod
    cfg = m.load_config(str(env.cfg_path))
    held = m.ai.lock(cfg, "always-on-replay")
    write_case(env.tmp / "corpus", env)
    try:
        rc, out = replay(env, env.tmp / "corpus")
    finally:
        held.close()
    assert rc == 4 and env.reviewer_calls() == []


def test_replay_scores_nothing_for_cases_without_confirmed_ground_truth(env):
    corpus = env.tmp / "corpus"
    p = write_case(corpus, env)
    c = json.loads(p.read_text())
    for g in c["ground_truth"]:
        g["confirmed"] = False
    p.write_text(json.dumps(c))
    rc, out = replay(env, corpus)
    assert json.loads(out.read_text())["cases"][0]["state"] == "skipped" and env.reviewer_calls() == []


def test_replay_refuses_a_bad_corpus_and_a_disabled_or_mismatched_report(env):
    corpus = env.tmp / "corpus"
    corpus.mkdir()
    (corpus / "bad.json").write_text("{}")
    assert replay(env, corpus)[0] == 2
    assert replay(env, env.tmp / "nowhere")[0] == 2
    write_case(env.tmp / "c2", env)
    rc, out = replay(env, env.tmp / "c2")
    assert rc == 0 and replay(env, env.tmp / "c2", tier="tier1", out=out)[0] == 2        # report is for tier2


def test_replay_with_another_clone_per_repo(env):
    other = env.tmp / "other-clone"
    TA.sh("git", "clone", "-q", str(env.origin), str(other), cwd=env.tmp)
    env.config("replay: {clones: [{repo: acme/widget, path: " + str(other) + "}]}\nclone: /nonexistent\n")
    write_case(env.tmp / "corpus", env)
    rc, out = replay(env, env.tmp / "corpus")
    assert rc == 0 and json.loads(out.read_text())["cases"][0]["state"] == "scored"


def test_replay_head_not_in_the_clone_fails_the_case_cleanly(env):
    corpus = env.tmp / "corpus"
    p = write_case(corpus, env)
    c = json.loads(p.read_text())
    c["head_sha"] = "1" * 40
    p.write_text(json.dumps(c))
    rc, out = replay(env, corpus)
    assert rc == 1 and json.loads(out.read_text())["cases"][0]["state"] == "failed"


# ---------------- 6c. compare, table ----------------

def two_reports(env):
    m = env.mod
    a = m.score_case(case("pr733.json"), findings("pr733-strong.json"))
    b = m.score_case(case("pr733.json"), findings("pr733-weak.json"))
    mk = lambda s, cost: {"version": 1, "tier": "tier2", "model": "opus", "cases": [
        {**s, "id": "pr733", "state": "scored", "cost_usd": cost, "duration_s": 3.0}],
        "aggregate": m.aggregate([{**s, "state": "scored", "cost_usd": cost, "duration_s": 3.0}])}
    pa, pb = env.tmp / "a.json", env.tmp / "b.json"
    pa.write_text(json.dumps(mk(a, 1.0)))
    pb.write_text(json.dumps(mk(b, 0.25)))
    return pa, pb, a, b


def test_compare_prints_aggregate_and_per_case_deltas(env, capsys):
    pa, pb, a, b = two_reports(env)
    assert env.mod.main(["replay", "compare", str(pa), str(pb)]) == 0
    out = capsys.readouterr().out
    assert "recall" in out and "false-blocker" in out.lower() and "pr733" in out
    assert f"{a['recall']:.2f}" in out and f"{b['recall']:.2f}" in out and "-0.75" in out      # cost delta
    assert env.mod.main(["replay", "compare", str(pa), str(env.tmp / "missing.json")]) == 2


def test_the_printed_table_has_per_case_aggregate_and_category_rows(env):
    pa, _, a, _ = two_reports(env)
    text = env.mod.format_report(json.loads(pa.read_text()))
    assert "pr733" in text and "AGGREGATE" in text and "recall" in text and "tests" in text and "cost" in text.lower()
    assert "FALSE_BLOCKER_CANDIDATE" in text and "unconfirmed" in text


def test_replay_prints_the_table(env, capsys):
    env.reviewer("claude", answer=block())
    write_case(env.tmp / "corpus", env)
    replay(env, env.tmp / "corpus")
    assert "AGGREGATE" in capsys.readouterr().out


# ---------------- 6d. corpus build (read-only gh) ----------------

FAKE_BUILD_GH = r'''#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
open(os.path.join(d, "gh-build.log"), "a").write(json.dumps(argv) + "\n")
st = json.load(open(os.path.join(d, "build-state.json")))
if argv[:2] == ["pr", "list"]:
    print(json.dumps(st["prs"]))
elif argv[0] == "api":
    path = argv[1].split("?")[0]
    print(json.dumps(st["api"].get(path, [])))
else:
    sys.stderr.write("unhandled " + " ".join(argv)); sys.exit(2)
'''

BUILD_STATE = {
    "prs": [
        {"number": 10, "title": "feat: a", "headRefOid": "a" * 40, "mergeCommit": {"oid": "m" * 40},
         "author": {"login": "dev"}, "mergedAt": "2026-10-02T00:00:00Z", "baseRefName": "main"},
        {"number": 11, "title": "feat: b", "headRefOid": "b" * 40, "mergeCommit": {"oid": "n" * 40},
         "author": {"login": "dev"}, "mergedAt": "2026-10-03T00:00:00Z", "baseRefName": "main"},
        {"number": 12, "title": "feat: c", "headRefOid": "c" * 40, "mergeCommit": {"oid": "o" * 40},
         "author": {"login": "dev"}, "mergedAt": "2026-10-04T00:00:00Z", "baseRefName": "main"},
        {"number": 13, "title": "feat: quiet", "headRefOid": "d" * 40, "mergeCommit": {"oid": "p" * 40},
         "author": {"login": "dev"}, "mergedAt": "2026-10-05T00:00:00Z", "baseRefName": "main"}],
    "api": {
        "repos/acme/widget/commits": [
            {"sha": "m" * 40, "commit": {"message": "feat: a (#10)"}},
            {"sha": "r" * 40, "commit": {"message": "Revert \"feat: a (#10)\"\n\nThis reverts commit " + "m" * 40}},
            {"sha": "f" * 40, "commit": {"message": "fix: crash after feat b (#11) was merged"}},
            {"sha": "n" * 40, "commit": {"message": "feat: b (#11)"}},
            {"sha": "e" * 40, "commit": {"message": "chore: docs only"}}],
        f"repos/acme/widget/commits/{'r' * 40}": {"sha": "r" * 40, "files": [{"filename": "src/a.py"}]},
        f"repos/acme/widget/commits/{'f' * 40}": {"sha": "f" * 40, "files": [{"filename": "src/b.py"},
                                                                          {"filename": "tests/test_b.py"}]},
        "repos/acme/widget/pulls/10": {"base": {"sha": "1" * 40}},
        "repos/acme/widget/pulls/11": {"base": {"sha": "2" * 40}},
        "repos/acme/widget/pulls/12": {"base": {"sha": "3" * 40}},
        "repos/acme/widget/pulls/13": {"base": {"sha": "4" * 40}},
        "repos/acme/widget/pulls/12/comments": [
            {"id": 501, "path": "src/c.py", "line": 9, "body": "This can crash when x is None.",
             "user": {"login": "reviewer", "type": "User"}},
            {"id": 502, "path": "src/c.py", "line": 20, "body": "<!-- always-on-review:opus-agent-review --> bug",
             "user": {"login": "someone", "type": "User"}}],
        "repos/acme/widget/pulls/12/reviews": [{"id": 601, "body": "", "user": {"login": "r", "type": "User"}}],
    }}


def build(env, *extra):
    (env.bin / "gh").write_text(FAKE_BUILD_GH)
    (env.bin / "build-state.json").write_text(json.dumps(BUILD_STATE))
    out = env.tmp / "built"
    rc = env.mod.main(["corpus", "build", "--repo", "acme/widget", "--since", "2026-10-01", "--out", str(out), *extra])
    return rc, out


def test_corpus_build_derives_revert_followup_and_review_comment_cases(env):
    rc, out = build(env)
    assert rc == 0
    files = sorted(p.name for p in out.glob("*.json"))
    assert files == ["acme-widget-pr10.json", "acme-widget-pr11.json", "acme-widget-pr12.json"]       # 13 has no signal
    m = env.mod
    c10, c11, c12 = (m.load_case(out / f) for f in files)
    assert c10["source"] == "revert" and c10["head_sha"] == "a" * 40 and c10["base_sha"] == "1" * 40
    assert c11["source"] == "follow-up-fix"
    assert c12["source"] == "opus" or c12["source"] == "human"
    assert {g["file"] for g in c11["ground_truth"]} == {"src/b.py", "tests/test_b.py"}
    assert all(g["confirmed"] is False for c in (c10, c11, c12) for g in c["ground_truth"])
    by = {g["id"]: g for g in c12["ground_truth"]}
    assert by["comment-501"]["file"] == "src/c.py" and by["comment-501"]["line"] == 9
    assert "human" in c12["notes"] and "opus" in c12["notes"]


def test_corpus_build_only_makes_read_only_gh_calls(env):
    build(env)
    for line in (env.bin / "gh-build.log").read_text().splitlines():
        argv = json.loads(line)
        assert argv[0] in ("pr", "api") and (argv[:2] == ["pr", "list"] or argv[0] == "api")
        assert not {"-X", "--method", "--input", "-f", "-F", "--field", "--raw-field", "POST", "PATCH", "DELETE"} & set(argv)


def test_corpus_build_is_resumable_and_never_overwrites(env):
    rc, out = build(env)
    p = out / "acme-widget-pr10.json"
    c = json.loads(p.read_text())
    c["ground_truth"][0]["confirmed"] = True                       # a human confirmed it
    p.write_text(json.dumps(c))
    rc, out = build(env)
    assert rc == 0 and json.loads(p.read_text())["ground_truth"][0]["confirmed"] is True


def test_corpus_build_validates_arguments(env):
    (env.bin / "gh").write_text(FAKE_BUILD_GH)
    for args in (["--repo", "not a repo", "--since", "2026-10-01"], ["--repo", "acme/widget", "--since", "yesterday"]):
        assert env.mod.main(["corpus", "build", *args, "--out", str(env.tmp / "o")]) == 2


# ---------------- findings in reviews.jsonl ----------------

def test_reviews_jsonl_keeps_the_findings_capped_and_redacted(env):
    many = [{"file": "src/a.py", "line": i + 1, "severity": "note", "category": "code", "why": "n" * 700,
             "evidence": "", "how_it_fails": ""} for i in range(25)]
    first = {**block()["findings"][0], "why": "key " + TA.SECRET + " leaked", "evidence": "return eval(x)"}
    env.mod.ai.REDACT.add(TA.SECRET)             # what the pi backend does when it reads its key
    env.reviewer("claude", answer={**block(), "findings": [first, *many]})
    assert env.review("tier2") == 0
    rec = logs(env)[0]
    assert len(rec["findings"]) == 20
    f0 = rec["findings"][0]
    assert set(f0) == {"file", "line", "severity", "category", "why", "evidence", "how_it_fails"}
    assert TA.SECRET not in json.dumps(rec) and f0["evidence"] == "return eval(x)" and f0["severity"] == "blocking"
    assert all(len(f["why"]) <= 600 for f in rec["findings"])


def test_a_downgraded_finding_is_logged_as_a_note(env):
    f = block()["findings"][0]
    f.pop("evidence")
    env.reviewer("claude", answer={**block(), "findings": [f]})
    env.review("tier2")
    assert logs(env)[0]["findings"][0]["severity"] == "note" and logs(env)[0]["findings"][0]["downgraded"] is True
