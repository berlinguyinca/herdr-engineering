"""deploy/ci-review/always-on-review.py: the tier-1 ensemble (open-weight reviewers) checked by Opus, and the poll scope.

Real dependencies at the boundary, as in test_always_on_review.py (real git origin and clone, stand-in `gh`/`claude`
executables on PATH). The OpenAI-compatible reviewers talk to REAL local HTTP servers (http.server, streaming SSE like
llama.cpp), never to a stub of the code under test.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def _sibling():
    spec = importlib.util.spec_from_file_location("sibling_test_always_on_review",
                                                  Path(__file__).with_name("test_always_on_review.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sibling_test_always_on_review"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _sibling()
REPO, APPROVE = B.REPO, B.APPROVE
KEY = "sk-MUSEKEY-0123456789abcdefSECRET"


def finding(file="src/a.py", line=2, sev="high", title="eval of untrusted input",
            expl="eval(x) executes attacker-controlled code"):
    return {"file": file, "line": line, "severity": sev, "title": title, "explanation": expl}


class LlamaServer:
    """A real streaming OpenAI-compatible server. `answer` is the model's content; reasoning chunks come first."""

    def __init__(self, answer: str | dict | None = None, delay: float = 0.0, status: int = 200,
                 reasoning: str = "thinking about the diff"):
        self.answer = answer if isinstance(answer, str) else json.dumps(answer or {"findings": []})
        self.delay, self.status, self.reasoning = delay, status, reasoning
        self.requests: list[dict] = []
        self.active = self.max_active = 0
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer.lock:
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                    outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                try:
                    time.sleep(outer.delay)
                    if outer.status != 200:
                        self.send_response(outer.status)
                        self.end_headers()
                        self.wfile.write(b'{"error": "busy"}')
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [{"choices": [{"delta": {"reasoning_content": outer.reasoning}}]},
                              {"choices": [{"delta": {"content": outer.answer[:20]}}]},
                              {"choices": [{"delta": {"content": outer.answer[20:]}, "finish_reason": "stop"}]},
                              {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}]
                    for c in chunks:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                finally:
                    with outer.lock:
                        outer.active -= 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers():
    made: list[LlamaServer] = []

    def make(*a, **kw):
        made.append(LlamaServer(*a, **kw))
        return made[-1]

    yield make
    for s in made:
        s.close()


def dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def reviewers_yaml(*rs: dict) -> str:
    lines = ["tiers:", "  tier1:", "    reviewers:"]
    for r in rs:
        lines.append("      - " + json.dumps(r))
    return "\n".join(lines) + "\n"


def openai(name, url, **kw):
    return {"name": name, "backend": "openai", "model": f"{name}-model", "base_url": url, **kw}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return B.Env(tmp_path, monkeypatch)


# ---------------- pure functions ----------------

def test_parse_eval_findings_takes_prose_fences_and_drops_malformed_items(env):
    m = env.mod
    text = "Sure!\n```json\n" + json.dumps({"findings": [
        finding(), {"file": "", "title": "x"}, "nope", {"file": "b.py", "line": "7", "severity": "bogus",
                                                         "summary": "s", "why": "w"}]}) + "\n```"
    f = m.parse_eval_findings(text)
    assert [(x["file"], x["line"], x["severity"]) for x in f] == [("src/a.py", 2, "high"), ("b.py", 7, "medium")]
    assert m.parse_eval_findings('{"findings": []}') == []
    for bad in ("", "no json", '{"other": 1}', '{"findings": "x"}'):
        with pytest.raises(m.ReviewFailed):
            m.parse_eval_findings(bad)


def test_dedupe_merges_the_same_defect_across_reviewers_and_keeps_distinct_ones(env):
    m = env.mod
    a = [finding(line=10, title="SQL injection in query builder", expl="user input is concatenated into SQL"),
         finding(line=80, sev="low", title="Resource leak on error path", expl="the file handle is never closed")]
    b = [finding(line=12, sev="critical", title="SQL injection via string concatenation",
                 expl="input from the request is concatenated into the SQL query, so an attacker controls it"),
         finding(file="other.py", line=10, title="SQL injection in query builder", expl="same words, other file")]
    out = m.dedupe_findings([("qwen", a), ("muse", b)])
    assert [(c["id"], c["file"], c["line"], c["found_by"]) for c in out] == [
        ("T1", "other.py", 10, ["muse"]), ("T2", "src/a.py", 10, ["qwen", "muse"]), ("T3", "src/a.py", 80, ["qwen"])]
    merged = next(c for c in out if c["id"] == "T2")
    assert merged["severity"] == "critical" and "attacker controls" in merged["explanation"]


def test_dedupe_keeps_findings_apart_when_the_lines_are_far_or_the_text_differs(env):
    m = env.mod
    out = m.dedupe_findings([("a", [finding(line=10, title="Missing authorization check", expl="anyone can call it")]),
                             ("b", [finding(line=11, title="Integer overflow in length", expl="int32 wraps")]),
                             ("c", [finding(line=300, title="Missing authorization check", expl="anyone can call it")])])
    assert len(out) == 3


def test_gateway_findings_become_candidates(env):
    m = env.mod
    v = m.parse_verdict({**B.block(), "head_sha": "a" * 40}, "a" * 40)
    [c] = m._gateway_findings(v)
    assert c["file"] == "src/a.py" and c["severity"] == "high" and "Fails when" in c["explanation"]


# ---------------- configuration ----------------

@pytest.mark.parametrize("extra", [
    "tiers: {tier1: {reviewers: nope}}\n",
    reviewers_yaml({"name": "x y", "backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1"}),
    reviewers_yaml({"name": "a", "backend": "pi", "model": "m"}),
    reviewers_yaml({"name": "a", "backend": "openai", "model": "m"}),                                   # no base_url
    reviewers_yaml({"name": "a", "backend": "openai", "model": "m", "base_url": "http://example.com/v1"}),   # plain http
    reviewers_yaml({"name": "a", "backend": "openai", "model": "m", "base_url": "https://u:p@example.com/v1"}),
    reviewers_yaml({"name": "a", "backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "x": 1}),
    reviewers_yaml({"name": "a", "backend": "gateway", "model": "m", "base_url": "https://example.com/v1"}),
    reviewers_yaml(openai("a", "http://127.0.0.1:1/v1"), openai("a", "http://127.0.0.1:2/v1")),         # same name
    reviewers_yaml(openai("a", "http://127.0.0.1:1/v1", max_tokens=0)),
    reviewers_yaml(openai("a", "http://127.0.0.1:1/v1")) + "    comment: true\n",                          # comment + ensemble
    "only_prs_updated_after: yesterday\n", "max_comments_per_hour: -1\n", "max_comments_per_hour: true\n",
    "skip_forks: 1\n", "repos: [not-a-repo]\n",
])
def test_config_refuses_bad_reviewers_and_scope(env, extra):
    env.config(extra)
    assert env.review("tier1") == 2 and env.reviewer_calls() == []


def test_config_accepts_the_documented_reviewers_and_scope(env):
    env.config(reviewers_yaml({"name": "qwen", "backend": "gateway", "model": "qwen3.8-27b"},
                              openai("muse", "http://127.0.0.1:28931/v1", api_key_file="~/k"))
               + "skip_archived: true\nskip_forks: true\nskip_drafts: true\nonly_prs_updated_after: '2026-11-17'\n"
                 "max_comments_per_hour: 6\n")
    cfg = env.mod.load_config(str(env.cfg_path))
    muse = cfg["tiers"]["tier1"]["reviewers"][1]
    assert muse["max_tokens"] == 32000 and muse["api_key_file"] == "~/k" and cfg["max_comments_per_hour"] == 6


# ---------------- the OpenAI-compatible reviewer against a real server ----------------

def reviewer_cfg(env, **kw):
    env.config(reviewers_yaml(openai("muse", kw.pop("url"), **kw)))
    cfg = env.mod.load_config(str(env.cfg_path))
    return cfg, cfg["tiers"]["tier1"], cfg["tiers"]["tier1"]["reviewers"][0]


def request(env, diff="diff --git a/src/a.py b/src/a.py\n+x\n"):
    return env.mod.ReviewRequest(REPO, 7, "a" * 40, "t", "b", "", diff, ["src/a.py"])


def test_openai_reviewer_streams_sends_the_key_and_reads_the_findings(env, servers):
    key_file = env.tmp / "muse.key"
    key_file.write_text(KEY + "\n")
    srv = servers({"findings": [finding(expl=f"echo {KEY}")]})
    cfg, tier, r = reviewer_cfg(env, url=srv.url, api_key_file=str(key_file))
    res = env.mod.run_tier1_reviewer(r, cfg, tier, request(env))
    assert res["ok"] and res["findings"][0]["file"] == "src/a.py" and res["usage"] == {
        "prompt_tokens": 11, "completion_tokens": 7}
    [seen] = srv.requests
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == f"Bearer {KEY}"
    b = seen["body"]
    assert b["model"] == "muse-model" and b["stream"] is True and b["max_tokens"] == 32000
    assert "senior engineer doing a pre-merge code review" in b["messages"][0]["content"]
    assert "## Diff under review" in b["messages"][1]["content"]


def test_openai_reviewer_without_a_key_file_sends_no_authorization(env, servers):
    srv = servers({"findings": []})
    cfg, tier, r = reviewer_cfg(env, url=srv.url)
    assert env.mod.run_tier1_reviewer(r, cfg, tier, request(env))["ok"] and srv.requests[0]["auth"] is None


def test_openai_reviewer_takes_an_answer_stranded_in_reasoning(env, servers):
    srv = servers("", reasoning='thinking... {"findings": [{"file": "a.py", "line": 1, "title": "t", "explanation": "e"}]}')
    cfg, tier, r = reviewer_cfg(env, url=srv.url)
    res = env.mod.run_tier1_reviewer(r, cfg, tier, request(env))
    assert res["ok"] and res["findings"][0]["file"] == "a.py"


@pytest.mark.parametrize("make", ["down", "http500", "prose"])
def test_a_reviewer_that_is_down_or_answers_badly_is_recorded_not_raised(env, servers, make):
    url = {"down": dead_url, "http500": lambda: servers({}, status=500).url,
           "prose": lambda: servers("I could not review this").url}[make]()
    cfg, tier, r = reviewer_cfg(env, url=url)
    res = env.mod.run_tier1_reviewer(r, cfg, tier, request(env))
    assert res["ok"] is False and res["error"] and res["findings"] == []


def test_requests_to_the_one_slot_server_are_serialized(env, servers):
    srv = servers({"findings": []}, delay=0.4)
    cfg, tier, r = reviewer_cfg(env, url=srv.url)
    out = []
    ts = [threading.Thread(target=lambda: out.append(env.mod.run_tier1_reviewer(r, cfg, tier, request(env))))
          for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [x["ok"] for x in out] == [True] * 3 and srv.max_active == 1 and len(srv.requests) == 3


def test_a_request_waiting_for_the_slot_obeys_the_time_cap(env, servers):
    srv = servers({"findings": []}, delay=3)
    cfg, tier, r = reviewer_cfg(env, url=srv.url, time_cap_s=2)
    first = threading.Thread(target=lambda: env.mod.run_tier1_reviewer(r, cfg, tier, request(env)))
    first.start()
    time.sleep(0.5)
    res = env.mod.run_tier1_reviewer(r, cfg, tier, request(env))
    first.join()
    assert res["ok"] is False and "slot" in res["error"]


# ---------------- tier 1 then tier 2, end to end ----------------

CHECKS = {"tier1_checks": [{"id": "T1", "decision": "confirm", "reason": "line 2 really evals the input"},
                           {"id": "T2", "decision": "reject", "reason": "the handle is closed by the caller"}]}


def ensemble_env(env, servers, post=True, extra=""):
    a = servers({"findings": [finding(), finding(line=40, sev="low", title="Resource leak on error path",
                                                 expl="the file handle is never closed")]})
    b = servers({"findings": [finding(sev="critical", title="eval of untrusted input is code execution",
                                      expl="eval(x) on user input lets an attacker run code")]})
    env.config(reviewers_yaml(openai("muse", a.url), openai("second", b.url), openai("down", dead_url()))
               + "tiers:\n  tier2: {comment: true}\n" * 0 + extra, post=post)
    return a, b


def test_tier1_runs_reviewers_in_parallel_posts_nothing_and_survives_a_down_reviewer(env, servers):
    a, b = ensemble_env(env, servers)
    assert env.review("tier1", post=True) == 0
    assert B.sh("true", cwd=env.tmp) == "" and not (env.bin / "reviewer.log").exists()      # no subprocess reviewer
    assert env.gh_writes() == [] and env.gh_state().get("comments", []) == []               # no status, no comment
    [path] = list((env.tmp / "state/tier1").glob("*.json"))
    d = json.loads(path.read_text())
    assert d["sha"] == env.head and [r["ok"] for r in d["reviewers"]] == [True, True, False]
    assert "down" in d["reviewers"][2]["name"] and d["reviewers"][2]["error"]
    assert [(c["id"], c["line"], c["found_by"]) for c in d["candidates"]] == [
        ("T1", 2, ["muse", "second"]), ("T2", 40, ["muse"])]
    assert env.review("tier1", post=True) == env.mod.SKIPPED                                # once per head
    assert len(a.requests) == 1 and len(b.requests) == 1


def test_tier1_with_every_reviewer_down_fails_the_run_but_not_the_pr(env, servers):
    env.config(reviewers_yaml(openai("muse", dead_url()), openai("second", dead_url())))
    assert env.review("tier1") == 1
    d = json.loads(next((env.tmp / "state/tier1").glob("*.json")).read_text())
    assert d["candidates"] == [] and not any(r["ok"] for r in d["reviewers"])
    env.reviewer("claude", answer=APPROVE)
    assert env.review("tier2") == 0 and len(env.reviewer_calls()) == 1                      # Opus still reviews


def test_poll_runs_tier1_then_opus_which_checks_every_finding_and_posts_one_comment(env, servers):
    ensemble_env(env, servers)
    env.reviewer("claude", answer={**APPROVE, **CHECKS})
    assert env.run("poll", "--repo", REPO, post=True) == 0
    [call] = env.reviewer_calls()
    assert call["tool"] == "claude" and "TIER-1 FINDINGS" in call["stdin"] and '"id": "T2"' in call["stdin"]
    assert "tier1_checks" in call["argv"][call["argv"].index("--json-schema") + 1]
    [comment] = env.gh_state()["comments"]
    body = comment["body"]
    assert body.startswith(f"<!-- always-on-review:opus-checked:{env.head[:12]} -->")
    assert "Confirmed by Opus (1)" in body and "eval of untrusted input" in body
    assert "found by muse, second" in body
    assert "<details><summary>Rejected by Opus (1)</summary>" in body and "handle is closed by the caller" in body
    assert body.index("Confirmed by Opus") < body.index("<details>") < body.index("Resource leak")
    assert "down" in body and "unavailable" in body
    assert [s["state"] for s in env.statuses_posted() if s["context"] == "Opus agent review"][-1] == "success"
    # a second poll changes nothing: one comment per head
    assert env.run("poll", "--repo", REPO, post=True) == 0 and len(env.gh_state()["comments"]) == 1


def test_opus_may_leave_a_finding_unanswered_and_it_is_never_counted_confirmed(env, servers):
    ensemble_env(env, servers)
    env.reviewer("claude", answer={**APPROVE, "tier1_checks": CHECKS["tier1_checks"][:1]})
    assert env.run("poll", "--repo", REPO, post=True) == 0
    body = env.gh_state()["comments"][0]["body"]
    assert "Confirmed by Opus (1)" in body and "Not checked by Opus (1)" in body and "<details>" not in body


def test_a_malformed_check_fails_the_opus_review_and_posts_no_success(env, servers):
    ensemble_env(env, servers)
    env.reviewer("claude", answer={**APPROVE, "tier1_checks": [{"id": "T1", "decision": "maybe", "reason": "x"}]})
    assert env.review("tier2", post=True) == 1
    assert {s["state"] for s in env.statuses_posted()} == {"pending"} and env.gh_state().get("comments", []) == []


def test_opus_blocking_finding_still_decides_the_status_and_is_in_the_same_comment(env, servers):
    ensemble_env(env, servers)
    env.reviewer("claude", answer={**B.block(), **CHECKS})
    assert env.run("poll", "--repo", REPO, post=True) == 0
    [comment] = env.gh_state()["comments"]
    assert "Blocking (1)" in comment["body"] and "Confirmed by Opus" in comment["body"]
    assert [s["state"] for s in env.statuses_posted() if s["context"] == "Opus agent review"][-1] == "failure"


def test_tier1_finding_nothing_still_runs_opus_and_adds_no_comment(env, servers):
    srv = servers({"findings": []})
    env.config(reviewers_yaml(openai("muse", srv.url)), post=True)
    env.reviewer("claude", answer=APPROVE)
    assert env.run("poll", "--repo", REPO, post=True) == 0
    [call] = env.reviewer_calls()
    assert "TIER-1 FINDINGS" not in call["stdin"] and "tier1_checks" not in call["stdin"]
    assert env.gh_state().get("comments", []) == []
    assert [s["state"] for s in env.statuses_posted() if s["context"] == "Opus agent review"][-1] == "success"


def test_max_comments_per_hour_skips_the_comment_but_not_the_status(env, servers):
    ensemble_env(env, servers, extra="max_comments_per_hour: 0\n")
    env.reviewer("claude", answer={**APPROVE, **CHECKS})
    assert env.run("poll", "--repo", REPO, post=True) == 0
    assert env.gh_state().get("comments", []) == []
    assert [s["state"] for s in env.statuses_posted() if s["context"] == "Opus agent review"][-1] == "success"


def test_the_comment_cap_counts_posted_comments_in_the_rolling_hour(env, servers):
    ensemble_env(env, servers, extra="max_comments_per_hour: 1\n")
    env.reviewer("claude", answer={**APPROVE, **CHECKS})
    assert env.run("poll", "--repo", REPO, post=True) == 0 and len(env.gh_state()["comments"]) == 1
    state = json.loads((env.tmp / "state/state.json").read_text())
    assert len(state["_comments"]["at"]) == 1
    # a new head is a new PR comment, but the cap of 1 per hour is spent
    st = env.gh_state()
    sh = B.sh
    (env.clone / "src/a.py").write_text("def f(x):\n    return eval(x) + 1\n")
    sh("git", "commit", "-qam", "again", cwd=env.clone)
    head2 = sh("git", "rev-parse", "HEAD", cwd=env.clone)
    sh("git", "push", "-q", "origin", f"{head2}:refs/pull/7/head", cwd=env.clone)
    st["prs"]["7"]["head"] = head2
    st["prs"]["7"]["diff"] = sh("git", "diff", f"{env.base}...{head2}", cwd=env.clone) + "\n"
    ours = {"Open-weight pre-review", "Opus agent review"}
    st["statuses"][head2] = [x for x in st["statuses"][env.head] if x["context"] not in ours]   # CI is green on head2
    st["check_runs"][head2] = list(st["check_runs"][env.head])
    env.write_gh(st)
    opus_before = len([c for c in env.reviewer_calls() if c["tool"] == "claude"])
    assert env.run("poll", "--repo", REPO, post=True) == 0
    assert len([c for c in env.reviewer_calls() if c["tool"] == "claude"]) == opus_before + 1   # Opus really ran on head2
    assert [s["state"] for s in env.statuses_posted() if s["context"] == "Opus agent review"][-1] == "success"
    assert len(env.gh_state()["comments"]) == 1                                                  # ... but the cap held the comment
    assert len(json.loads((env.tmp / "state/state.json").read_text())["_comments"]["at"]) == 1


def test_the_comment_cap_window_rolls_over_after_an_hour(env):
    env.config("max_comments_per_hour: 1\n")
    cfg = env.mod.load_config(str(env.cfg_path))
    state = env.mod.State(cfg)
    env.mod.record_comment(state)
    assert env.mod.comment_allowed(cfg, state) is False
    state.put("_comments", at=[time.time() - 3601])
    assert env.mod.comment_allowed(cfg, state) is True


def test_the_api_key_never_reaches_the_tier1_file_or_the_log(env, servers):
    key_file = env.tmp / "k"
    key_file.write_text(KEY)
    srv = servers({"findings": [finding(expl=f"leaked {KEY}")]})
    env.config(reviewers_yaml(openai("muse", srv.url, api_key_file=str(key_file))))
    assert env.review("tier1") == 0
    blob = "".join(p.read_text() for p in (env.tmp / "state").rglob("*") if p.is_file())
    assert KEY not in blob


def test_show_command_lists_the_reviewers(env, servers, capsys):
    env.config(reviewers_yaml(openai("muse", "http://127.0.0.1:28931/v1")))
    assert env.run("show-command", "--tier", "tier1") == 0
    assert "tier1 reviewer muse: backend openai" in capsys.readouterr().out


# ---------------- poll scope: repos, archived, forks, drafts, dates, backfill ----------------

OLD, NEW = "2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z"


def set_pr(env, **kw):
    st = env.gh_state()
    st["prs"]["7"].update(kw)
    env.write_gh(st)


def polled(env, *args, cfg="", fresh=False) -> bool:
    """One dry-run poll; True when a reviewer ran. `fresh` forgets state.json first: a dry run records dry_sha for every
    head it reviewed, which would make a later poll skip the PR for that reason and hide the filter under test."""
    if fresh:
        (env.tmp / "state/state.json").unlink(missing_ok=True)
    env.config(cfg)
    n = len(env.reviewer_calls())
    assert env.run("poll", "--repo", REPO, *args) == 0
    return len(env.reviewer_calls()) > n


def test_enrolling_a_repo_does_not_backfill_old_prs_but_reviews_later_updates(env):
    set_pr(env, updated_at=OLD)
    assert polled(env) is False                                       # first poll: enrolled, the old PR is left alone
    enrolled = json.loads((env.tmp / "state/state.json").read_text())[f"repo:{REPO}"]["enrolled_at"]
    assert enrolled.endswith("Z")
    assert polled(env) is False                                       # still not updated since
    set_pr(env, updated_at=NEW)
    assert polled(env) is True                                        # a push after enrollment is reviewed
    assert json.loads((env.tmp / "state/state.json").read_text())[f"repo:{REPO}"]["enrolled_at"] == enrolled


def test_backfill_true_reviews_every_open_pr_of_a_new_repo(env):
    set_pr(env, updated_at=OLD)
    assert polled(env, cfg="backfill: true\n") is True


def test_a_pr_without_updated_at_is_skipped_when_a_cutoff_is_in_force(env):
    set_pr(env, updated_at="")
    assert polled(env) is False


def test_only_prs_updated_after_is_an_absolute_floor(env):
    set_pr(env, updated_at="2029-06-01T00:00:00Z")
    assert polled(env, cfg="backfill: true\nonly_prs_updated_after: '2030-01-01'\n") is False
    set_pr(env, updated_at="2031-06-01T00:00:00Z")
    assert polled(env, cfg="backfill: true\nonly_prs_updated_after: '2030-01-01'\n") is True


def set_repo(env, **flags):
    st = env.gh_state()
    st["repos"] = {REPO: {"isArchived": False, "isFork": False, **flags}}
    env.write_gh(st)


def test_archived_repos_are_skipped_by_default(env):
    set_repo(env, isArchived=True)
    assert polled(env, fresh=True) is False                           # skip_archived defaults to true
    assert polled(env, cfg="skip_archived: false\n", fresh=True) is True


def test_fork_repos_are_skipped_only_when_skip_forks_is_on(env):
    set_repo(env, isFork=True)
    assert polled(env, fresh=True) is True                            # skip_forks defaults to false: reviewed
    assert polled(env, cfg="skip_forks: true\n", fresh=True) is False


def test_drafts_are_skipped_by_default_and_reviewed_when_skip_drafts_is_false(env):
    set_pr(env, draft=True)
    assert polled(env, fresh=True) is False
    assert polled(env, cfg="skip_drafts: false\n", fresh=True) is True


def test_repos_in_the_config_are_what_poll_watches(env):
    set_pr(env, updated_at=NEW)
    env.config(f"repos: [{REPO}]\n")
    assert env.run("poll") == 0 and len(env.reviewer_calls()) == 2


def test_enrolling_a_later_repo_does_not_erase_what_an_earlier_repos_reviews_saved(env):
    other = "acme/other"
    state = env.tmp / "state/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({f"repo:{REPO}": {"enrolled_at": "2020-01-01T00:00:00Z"}}))   # A enrolled, B is new
    env.config()
    assert env.run("poll", "--repo", REPO, "--repo", other) == 0
    saved = json.loads(state.read_text())
    assert saved[f"{REPO}#7#tier1"]["dry_sha"] == env.head and saved[f"{REPO}#7#tier2"]["dry_sha"] == env.head
    assert f"repo:{other}" in saved and saved[f"repo:{REPO}"]["enrolled_at"] == "2020-01-01T00:00:00Z"
    calls = len(env.reviewer_calls())
    assert env.run("poll", "--repo", REPO, "--repo", other) == 0
    assert len(env.reviewer_calls()) == calls                         # nothing is reviewed twice


def test_two_state_holders_never_clobber_each_others_keys(env):
    cfg = env.mod.load_config(str(env.cfg_path))
    a, b = env.mod.State(cfg), env.mod.State(cfg)                     # both snapshots taken before either writes
    a.put("x", v=1)
    b.put("y", v=2)
    a.put("x", w=3)
    assert json.loads((env.tmp / "state/state.json").read_text()) == {"x": {"v": 1, "w": 3}, "y": {"v": 2}}
    assert b.get("x") == {"v": 1, "w": 3}                             # readers see the other holder's writes


def test_a_repo_the_state_already_knows_is_grandfathered_not_newly_enrolled(env):
    set_pr(env, updated_at=OLD)                                       # in progress: CI moved, updatedAt did not
    state = env.tmp / "state/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({f"{REPO}#7#tier2": {"failures": 1, "failure_sha": "0" * 40}}))
    assert polled(env) is True                                        # an upgraded deployment keeps its PRs in scope
    saved = json.loads(state.read_text())[f"repo:{REPO}"]["enrolled_at"]
    assert saved.startswith("1970-01-01")                             # recorded as "always enrolled": no cutoff
    # a keys-for-another-repo state does not grandfather this repo
    state.write_text(json.dumps({"acme/other#7#tier2": {"failures": 1}}))
    assert polled(env) is False
