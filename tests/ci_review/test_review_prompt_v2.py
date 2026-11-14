"""Review prompt v2 and the replay harness of deploy/ci-review/always-on-review.py.

Same approach as test_always_on_review.py: a real temporary git origin and clone, stand-in `gh`/`claude`/`pi`
executables on PATH that log their calls and answer from a JSON script, real YAML and JSON files. Scoring tests use
fixture findings files from tests/ci_review/fixtures. Nothing here calls a model.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import shutil
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
    assert env.review("tier2", post=True) == 1                                   # the model said block: never success
    assert [s for s in env.statuses_posted() if s["state"] in ("success", "failure")] == []
    assert env.gh_state().get("comments", []) == []
    assert logs(env)[0]["downgraded"] == 1 and logs(env)[0]["blocking"] == 0 and logs(env)[0]["state"] == "unverified"


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
    d = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,2 @@\n x\n+unrelated line here\n"
    out, n = m.apply_evidence(v, d, tmp_path)
    assert n == 0 and out.findings[0].severity == "blocking"
    out, n = m.apply_evidence(v, d, None)                                           # no worktree: the diff only
    assert n == 1 and out.findings[0].severity == "note" and out.findings[0].downgraded


def test_apply_evidence_ignores_diff_line_prefixes_and_refuses_escaping_paths(tmp_path):
    m = load()
    diff = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n context line\n+    first_added(1)\n+    second_added(2)\n"
    two = m.Finding("a.py", 2, "blocking", "code", "w", "first_added(1)\nsecond_added(2)", "h")
    out, n = m.apply_evidence(m.Verdict("s", "block", (two,), ("code",)), diff, None)
    assert n == 0
    (tmp_path / "wt").mkdir()
    (tmp_path / "secret.txt").write_text("outside the worktree text")
    assert m._read_at_head(tmp_path / "wt", "../secret.txt") == ""
    assert m._read_at_head(tmp_path / "wt", "") == ""


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
    a = m.Verdict("s", "approve", (mk("note", "same problem"),), ("code",), ("x",))
    b = m.Verdict("s", "block", (mk("blocking", "Same  problem"), m.Finding("a.py", 3, "note", "tests", "t")), ("tests",), ("y",))
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
    assert env.review("tier2", post=True) == 1
    assert [s for s in env.statuses_posted() if s["state"] in ("success", "failure")] == []
    assert logs(env)[0]["downgraded"] == 3 and logs(env)[0]["findings"][0]["severity"] == "note"


def test_the_time_cap_covers_all_passes_together(env):
    env.reviewer("claude", answer=APPROVE, sleep=4)
    env.config("tiers: {tier2: {time_cap_s: 10, passes: [{name: a, focus: A}, {name: b, focus: B}, {name: c, focus: C}]}}\n",
               post=True)
    assert env.review("tier2", post=True) == 1                                   # each pass gets 10/3 s, not 4; pending
    assert [s for s in env.statuses_posted() if s["state"] in ("success", "failure")] == []
    assert len(env.reviewer_calls()) == 1 and "time cap" in logs(env)[-1]["why"]


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


# ---------------- review fixes: partial passes, unverified blockers, evidence, merge, usage ----------------

FLAKY = r"""#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(d, "flaky.json")))
cf = os.path.join(d, "flaky.count")
n = int(open(cf).read()) if os.path.exists(cf) else 0
open(cf, "w").write(str(n + 1))
cfg = json.load(open(os.path.join(d, "claude.json")))
cfg["sleep"] = plan.get("sleeps", [])[n] if n < len(plan.get("sleeps", [])) else 0
if n >= plan.get("fail_from", 10 ** 6):
    cfg["exit"] = 3
json.dump(cfg, open(os.path.join(d, "claude.json"), "w"))
exec(compile(open(os.path.join(d, "claude.real")).read(), "claude", "exec"))
"""


def flaky(env, **plan):
    real = env.bin / "claude"
    (env.bin / "claude.real").write_text(real.read_text())
    real.write_text(FLAKY)
    (env.bin / "flaky.json").write_text(json.dumps(plan))


def terminal(env):
    return [s for s in env.statuses_posted() if s["state"] in ("success", "failure")]


def test_a_later_pass_failing_keeps_the_earlier_blockers_and_posts_failure(env):
    env.reviewer("claude", answer=block())
    flaky(env, fail_from=1)
    env.config(PASSES, post=True)
    assert env.review("tier2", post=True) == 0
    assert terminal(env)[-1]["state"] == "failure"                      # the completed pass found a real blocker
    rec = logs(env)[0]
    assert rec["partial"]
    assert rec["blocking"] == 1 and rec["passes"][0]["name"] == "general" and rec["findings"]


def test_a_later_pass_failing_never_posts_success_but_logs_the_findings(env):
    note = {**APPROVE, "findings": [{"file": "src/a.py", "line": 2, "severity": "note", "category": "code",
                                     "why": "naming"}]}
    env.reviewer("claude", answer=note)
    flaky(env, fail_from=1)
    env.config(PASSES, post=True)
    assert env.review("tier2", post=True) == 1
    assert terminal(env) == []
    first = logs(env)[0]
    assert first["state"] == "partial" and first["findings"][0]["why"] == "naming" and "tests" in first["partial"]


def test_each_pass_gets_a_fair_share_of_the_cap(env):
    import time
    env.reviewer("claude", answer=APPROVE)
    flaky(env, sleeps=[0, 100, 0])
    env.config("tiers: {tier2: {time_cap_s: 10, passes: [{name: a, focus: A}, {name: b, focus: B}, {name: c, focus: C}]}}\n", post=True)
    t = time.monotonic()
    assert env.review("tier2", post=True) == 1
    assert time.monotonic() - t < 8.5                                   # pass b was cut at its ~5 s share, not 10 s
    assert terminal(env) == [] and logs(env)[0]["state"] == "partial"
    assert len(env.reviewer_calls()) == 3 and "pass b" in logs(env)[0]["partial"]      # pass c still ran


def test_the_example_config_documents_a_shared_cap():
    text = (CI / "always-on-review.example.yaml").read_text()
    assert "shared" in text.lower()
    assert yaml.safe_load(text)["tiers"]["tier2"]["time_cap_s"] == 3600
    readme = (CI / "README.md").read_text().lower()
    assert "shared" in readme and "equal share of the time left" in readme
    assert "shared" in (ROOT / "CHANGELOG.md").read_text().split("### Always-on review")[0].lower()


def test_all_blockers_downgraded_is_pending_for_tier2_and_a_noted_success_for_tier1(env):
    f = block()["findings"][0]
    f.pop("evidence")
    env.reviewer("claude", answer={**block(), "findings": [f]})
    env.reviewer("pi", answer={**block(), "findings": [f]})
    env.config(post=True)
    assert env.review("tier2", post=True) == 1 and terminal(env) == []
    assert env.review("tier1", post=True) == 0
    last = terminal(env)[-1]
    assert last["state"] == "success" and "1 unverified blocker" in last["description"]


def test_a_downgrade_beside_a_surviving_blocker_is_counted_in_the_failure_text(env):
    bad = {**block()["findings"][0], "line": 1, "category": "code", "evidence": ""}
    env.reviewer("claude", answer={**block(), "findings": [block()["findings"][0], bad]})
    env.config(post=True)
    assert env.review("tier2", post=True) == 0
    last = terminal(env)[-1]
    assert last["state"] == "failure" and "1 downgraded" in last["description"]


def diff_two_files(a_line="    return eval(x)", b_line="    secret_call(token_value)"):
    return (f"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n def f(x):\n+{a_line}\n"
            f"diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1,1 +1,2 @@\n x = 1\n+{b_line}\n"
            "-    removed_only_line_here(1, 2)\n")


def fnd(m, file="src/a.py", ev="return eval(x)", how="x runs code", sev="blocking"):
    return m.Verdict("s", "block", (m.Finding(file, 2, sev, "code", "w", ev, how),), ("code",))


def test_evidence_must_come_from_the_cited_files_hunks_not_the_whole_diff():
    m = load()
    out, n = m.apply_evidence(fnd(m, ev="secret_call(token_value)"), diff_two_files(), None)
    assert n == 1                                                       # it is in b.py's hunk, the finding cites a.py
    out, n = m.apply_evidence(fnd(m, file="src/b.py", ev="secret_call(token_value)"), diff_two_files(), None)
    assert n == 0
    out, n = m.apply_evidence(fnd(m, ev="removed_only_line_here(1, 2)", file="src/b.py"), diff_two_files(), None)
    assert n == 0                                                       # a removed line is valid evidence


def test_evidence_needs_twenty_characters_or_a_whole_line():
    m = load()
    d = diff_two_files(a_line="    return eval(x) + other_value_here")
    assert m.apply_evidence(fnd(m, ev="eval(x)"), d, None)[1] == 1                       # a fragment of a line
    assert m.apply_evidence(fnd(m, ev="return eval(x) + other_value_here"), d, None)[1] == 0     # a whole line
    assert m.apply_evidence(fnd(m, ev="eval(x) + other_value"), d, None)[1] == 0         # 20 characters
    d2 = diff_two_files(a_line="    go_somewhere()")
    assert m.apply_evidence(fnd(m, ev="go_somewhere()"), d2, None)[1] == 0               # 12+ chars, a whole line
    d3 = diff_two_files(a_line="    go()")
    assert m.apply_evidence(fnd(m, ev="go()"), d3, None)[1] == 1                         # under 12 chars proves nothing
    d4 = diff_two_files(a_line="    else:")
    assert m.apply_evidence(fnd(m, ev="else:"), d4, None)[1] == 1
    d5 = diff_two_files(a_line="    except Exception:")
    assert m.apply_evidence(fnd(m, ev="except Exception:"), d5, None)[1] == 1            # 17 chars but a trivial line


def test_a_blocker_needs_a_non_empty_how_it_fails():
    m = load()
    for how in ("", "   "):
        out, n = m.apply_evidence(fnd(m, how=how), diff_two_files(), None)
        assert n == 1 and out.findings[0].severity == "note"
    assert m.apply_evidence(fnd(m, how="", sev="note"), diff_two_files(), None)[1] == 0


def test_sum_usage_ignores_none_values():
    m = load()
    got = m._sum_usage([{"cost_usd": None, "turns": 1}, {"cost_usd": 0.5, "turns": None}, {"cost_usd": None, "x": "a"}])
    assert got == {"cost_usd": 0.5, "turns": 1, "x": "a"}
    assert m._sum_usage([{"a": None}]) == {"a": None}


def test_merge_keeps_findings_with_different_reasons_apart_and_prefers_the_one_with_evidence():
    m = load()
    mk = lambda why, ev="", sev="note": m.Finding("a.py", 3, sev, "code", why, ev, "h" if ev else "")
    a = m.Verdict("s", "approve", (mk("the loop never ends"), mk("a leaked handle in close()")), ("c",))
    b = m.Verdict("s", "approve", (mk("The  loop never ends", "while True: pass"),), ("c",))
    out = m.merge_verdicts([a, b], "s")
    assert len(out.findings) == 2
    kept = [f for f in out.findings if f.why.lower().startswith("the")][0]
    assert kept.evidence == "while True: pass"


def test_over_long_evidence_and_how_it_fails_are_truncated_not_fatal():
    m = load()
    ans = {**block(), "head_sha": "a" * 40}
    ans["findings"][0]["evidence"] = "e" * (m.ai.TEXT_MAX + 10)
    ans["findings"][0]["how_it_fails"] = "h" * (m.ai.TEXT_MAX + 10)
    v = m.parse_verdict(ans, "a" * 40)
    assert len(v.findings[0].evidence) == m.ai.TEXT_MAX and len(v.findings[0].how_it_fails) == m.ai.TEXT_MAX
    ans["findings"][0]["why"] = "w" * (m.ai.TEXT_MAX + 1)
    with pytest.raises(m.ReviewFailed):
        m.parse_verdict(ans, "a" * 40)


def test_replay_retries_skipped_cases_once_they_have_ground_truth(env):
    corpus = env.tmp / "corpus"
    p = write_case(corpus, env)
    c = json.loads(p.read_text())
    good = json.loads(p.read_text())
    for g in c["ground_truth"]:
        g["confirmed"] = False
    p.write_text(json.dumps(c))
    env.reviewer("claude", answer=block())
    rc, out = replay(env, corpus)
    assert json.loads(out.read_text())["cases"][0]["state"] == "skipped"
    p.write_text(json.dumps(good))
    rc, out = replay(env, corpus)
    assert json.loads(out.read_text())["cases"][0]["state"] == "scored" and len(env.reviewer_calls()) == 1


def test_replay_rescores_a_case_whose_ground_truth_changed_but_not_an_unchanged_one(env):
    corpus = env.tmp / "corpus"
    p = write_case(corpus, env)
    env.reviewer("claude", answer=block())
    replay(env, corpus)
    replay(env, corpus)
    assert len(env.reviewer_calls()) == 1
    c = json.loads(p.read_text())
    c["ground_truth"][0]["summary"] = "eval of the request body runs attacker code"
    p.write_text(json.dumps(c))
    rc, out = replay(env, corpus)
    assert len(env.reviewer_calls()) == 2 and len(json.loads(out.read_text())["cases"]) == 1


def test_skipped_cases_do_not_use_up_the_limit(env):
    corpus = env.tmp / "corpus"
    p = write_case(corpus, env, "case-a")
    c = json.loads(p.read_text())
    for g in c["ground_truth"]:
        g["confirmed"] = False
    p.write_text(json.dumps(c))
    write_case(corpus, env, "case-b")
    replay(env, corpus, "--limit", "1")
    assert len(env.reviewer_calls()) == 1


def build_with(env, mutate):
    import copy
    st = copy.deepcopy(BUILD_STATE)
    mutate(st)
    (env.bin / "gh").write_text(FAKE_BUILD_GH)
    (env.bin / "build-state.json").write_text(json.dumps(st))
    out = env.tmp / "built"
    rc = env.mod.main(["corpus", "build", "--repo", "acme/widget", "--since", "2026-10-01", "--out", str(out)])
    return rc, out


def comments12(st, comments):
    st["api"]["repos/acme/widget/pulls/12/comments"] = comments


def cm(i, line, commit, login="reviewer", **kw):
    return {"id": i, "path": "src/c.py", "body": f"problem number {i} in the handler", "original_line": line,
            "line": kw.pop("line", None), "original_commit_id": commit, "commit_id": kw.pop("commit_id", "c" * 40),
            "user": {"login": login, "type": "User"}, **kw}


def test_corpus_build_pins_the_case_to_the_commit_the_comment_was_made_on(env):
    first, last = "1" * 40, "c" * 40
    rc, out = build_with(env, lambda st: comments12(st, [cm(701, 9, first), cm(702, 11, first), cm(703, 40, last)]))
    c = env.mod.load_case(out / "acme-widget-pr12.json")
    assert c["head_sha"] == first                                       # most comments were made on it, not the final head
    by = {g["id"]: g for g in c["ground_truth"]}
    assert by["comment-701"]["line"] == 9 and by["comment-702"]["line"] == 11
    assert by["comment-703"]["line"] is None                            # its coordinate belongs to another commit
    assert "pinned" in c["notes"]


def test_corpus_build_uses_the_line_for_commit_id_when_there_is_no_original(env):
    rc, out = build_with(env, lambda st: comments12(st, [
        {"id": 710, "path": "src/c.py", "body": "x can be None here", "line": 14, "commit_id": "d" * 40,
         "user": {"login": "reviewer", "type": "User"}}]))
    c = env.mod.load_case(out / "acme-widget-pr12.json")
    assert c["head_sha"] == "d" * 40 and c["ground_truth"][0]["line"] == 14


def test_corpus_build_excludes_bots_unless_configured_replies_and_the_authors_own_comments(env):
    rc, out = build_with(env, lambda st: comments12(st, [
        cm(720, 5, "c" * 40),
        cm(721, 6, "c" * 40, login="ci-bot[bot]", user_type="Bot"),
        cm(722, 7, "c" * 40, in_reply_to_id=720),
        cm(723, 8, "c" * 40, login="dev"),
        cm(724, 9, "c" * 40, login="listed[bot]")]))
    ids = {g["id"] for g in env.mod.load_case(out / "acme-widget-pr12.json")["ground_truth"]}
    assert ids == {"comment-720"}


def test_corpus_build_counts_configured_reviewer_logins_even_if_bots(env):
    env.config("replay: {reviewers: ['listed[bot]']}\n")
    (env.bin / "gh").write_text(FAKE_BUILD_GH)
    import copy
    st = copy.deepcopy(BUILD_STATE)
    comments12(st, [cm(724, 9, "c" * 40, login="listed[bot]"), cm(721, 6, "c" * 40, login="ci-bot[bot]")])
    (env.bin / "build-state.json").write_text(json.dumps(st))
    out = env.tmp / "built"
    assert env.mod.main(["corpus", "build", "--repo", "acme/widget", "--since", "2026-10-01", "--out", str(out),
                         "--config", str(env.cfg_path)]) == 0
    ids = {g["id"] for g in env.mod.load_case(out / "acme-widget-pr12.json")["ground_truth"]}
    assert ids == {"comment-724"}


PLUS_DIFF = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,4 @@\n def f(x):\n"
             "+    first_added_line(1)\n+    second_added_line(2)\n-    removed_guard_check(x)\n")


def test_evidence_may_carry_diff_markers_and_the_path_a_prefix():
    m = load()
    ok = lambda f, ev: m.apply_evidence(fnd(m, file=f, ev=ev), PLUS_DIFF, None)[1] == 0
    assert ok("src/a.py", "+    first_added_line(1)")                               # one marker, one line
    assert ok("src/a.py", "+    first_added_line(1)\n+    second_added_line(2)")    # markers on every line
    assert ok("b/src/a.py", "first_added_line(1)\nsecond_added_line(2)")
    assert ok("./src/a.py", "first_added_line(1)\nsecond_added_line(2)")
    assert ok("a/src/a.py", "-    removed_guard_check(x)")
    assert ok("src/a.py", "removed_guard_check(x)")                                 # a removed line is evidence
    assert not ok("src/a.py", "+    never_in_the_diff(99)")


def test_evidence_for_a_deleted_file_comes_from_its_removed_lines():
    m = load()
    d = ("diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
         "-def gone():\n-    dangerous_removed_call(1)\n")
    assert m.apply_evidence(fnd(m, file="old.py", ev="dangerous_removed_call(1)"), d, None)[1] == 0
    assert m.apply_evidence(fnd(m, file="old.py", ev="made_up_call_somewhere(1)"), d, None)[1] == 1


def test_a_blocker_outside_the_pr_is_dropped_but_an_invented_in_pr_blocker_stays_pending(env):
    env.config(post=True)
    env.reviewer("claude", answer=block(file="src/other.py"))
    assert env.review("tier2", post=True) == 0
    last = terminal(env)[-1]
    assert last["state"] == "success" and "1 finding dropped" in last["description"]
    f = block()["findings"][0]
    f["evidence"] = "totally_invented_call(1, 2, 3)"
    env.reviewer("claude", answer={**block(), "findings": [f]})
    assert env.review("tier2", post=True, *["--force"]) == 1
    assert "unverified" in [x["state"] for x in logs(env)[1:]]


def test_corpus_build_records_the_final_head_as_a_fallback_when_it_pins_an_earlier_commit(env):
    first = "1" * 40
    rc, out = build_with(env, lambda st: comments12(st, [cm(701, 9, first), cm(702, 11, first)]))
    c = env.mod.load_case(out / "acme-widget-pr12.json")
    assert c["head_sha"] == first and c["head_sha_fallback"] == "c" * 40
    shutil.rmtree(out)
    rc, out = build_with(env, lambda st: comments12(st, [cm(701, 9, "", commit_id="")]))
    assert "head_sha_fallback" not in env.mod.load_case(out / "acme-widget-pr12.json")


def test_replay_falls_back_to_the_final_head_when_the_pinned_commit_is_gone(env):
    env.reviewer("claude", answer=block())
    corpus = env.tmp / "corpus"
    p = write_case(corpus, env, head_sha_fallback=env.head)
    c = json.loads(p.read_text())
    c["head_sha"] = "1" * 40
    p.write_text(json.dumps(c))
    rc, out = replay(env, corpus)
    (rec,) = json.loads(out.read_text())["cases"]
    assert rc == 0 and rec["state"] == "scored" and rec["head_sha_used"] == env.head
    assert "fallback" in rec["note"] and "1" * 12 in rec["note"]


def test_replay_a_partial_result_is_not_scored_and_is_retried(env):
    env.reviewer("claude", answer=block())
    flaky(env, fail_from=1)
    env.config(PASSES)
    corpus = env.tmp / "corpus"
    write_case(corpus, env)
    rc, out = replay(env, corpus)
    rep = json.loads(out.read_text())
    (rec,) = rep["cases"]
    assert rc == 1 and rec["state"] == "partial" and "tests" in rec["partial"] and rec["passes"]
    agg = rep["aggregate"]
    assert agg["cases_scored"] == 0 and agg["cases_partial"] == 1 and agg["items"] == 0 and "recall" not in rec
    (env.bin / "flaky.json").write_text("{}")                                   # the pass works now
    env.reviewer("claude", answer=block())
    rc, out = replay(env, corpus)
    (rec,) = json.loads(out.read_text())["cases"]
    assert rc == 0 and rec["state"] == "scored"


@pytest.mark.parametrize("path", ["./src/a.py", "a/src/a.py", "b/src/a.py", " src/a.py "])
def test_a_verified_blocker_with_a_prefixed_path_still_blocks_end_to_end(env, path):
    f = {**block()["findings"][0], "file": path}
    env.reviewer("claude", answer={**block(), "findings": [f]})
    env.config(post=True)
    assert env.review("tier2", post=True) == 0
    assert terminal(env)[-1]["state"] == "failure"


@pytest.mark.parametrize("quote", ["except Exception as e:", "from __future__ import annotations",
                                   'if __name__ == "__main__":', "raise NotImplementedError()"])
def test_trivial_lines_are_not_evidence_even_when_long(quote):
    m = load()
    d = diff_two_files(a_line="    " + quote)
    assert m.apply_evidence(fnd(m, ev=quote), d, None)[1] == 1


def test_the_stoplist_is_case_insensitive():
    m = load()
    quote = "Except Exception As E:"
    d = diff_two_files(a_line="    " + quote)
    assert m.apply_evidence(fnd(m, ev=quote), d, None)[1] == 1


def test_a_verified_blocker_on_a_non_ascii_path_stays_blocking():
    m = load()
    d = ('diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n'
         "@@ -1,1 +1,2 @@\n x = 1\n+    return eval(user_input)\n")
    out, n = m.apply_evidence(fnd(m, file="café.py", ev="return eval(user_input)"), d, None)
    assert n == 0 and out.findings[0].severity == "blocking"


def test_quote_search_in_a_large_file_is_fast_and_finds_a_multiline_quote(tmp_path):
    import time
    m = load()
    body = ["    pass"] * 60000 + ["    first_line_of_it = 1", "    second_line_of_it = 2"] + ["    pass"] * 60000
    (tmp_path / "big.py").write_text("\n".join(body))
    d = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
    ev = "first_line_of_it = 1\nsecond_line_of_it = 2"
    t = time.monotonic()
    ok = m.apply_evidence(fnd(m, file="big.py", ev=ev), d, tmp_path)[1] == 0
    miss = m.apply_evidence(fnd(m, file="big.py", ev="first_line_of_it = 1\nnot_there_at_all_x"), d, tmp_path)[1] == 1
    assert ok and miss and time.monotonic() - t < 2
