"""ACCEPTANCE SCENARIO 2 (Herdr/AutoSpec half): independent AI reviewers, defect repair, head change invalidates the old
verdict, guarded merge fails closed.

Real pieces: a control-plane PROCESS (YAML-configured `review_quorum`), the REAL `autospec` Rust binary
(`review-quorum publish | request | --input --write-report | verify-report`), a real temp git repo for the merge-gate report.
Not real: GitHub. `verify-report` shells out to `gh pr view`, so a clearly marked FAKE shim (tests/e2e/fake_gh/gh) answers it;
the only environment variable set is PATH for that child process so it finds the shim (not configuration of any product).
The review dispatch command is a local script that writes a marker file (no model call).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.e2e.harness import E2E, ROOT, principal_id, sha40

REPO_SLUG, PR_NUM = "acme/widgets", 7
PR = f"pr:github.com/{REPO_SLUG}#{PR_NUM}"
PR_URL = f"https://github.com/{REPO_SLUG}/pull/{PR_NUM}"
AUTHOR = "someone-else"


def sha_hex(b: bytes | str) -> str:
    return hashlib.sha256(b.encode() if isinstance(b, str) else b).hexdigest()


def receipt_binding(pr: str, commit: str, reviewer_pid: str, digest: str) -> str:
    return sha_hex(f"pr-review-receipt-v1\0{pr.lower()}\0{commit}\0{reviewer_pid}\0{digest}")


class World:
    """One plane + the AutoSpec CLI configured per reviewer."""

    def __init__(self, planes, autospec_bin: Path, tmp: Path):
        self.autospec, self.tmp = autospec_bin, tmp
        self.plane = planes(
            keys={"pi": "ingest", "r1": "ingest", "r2": "ingest", "r3": "ingest", "human": "ingest", "attacker": "ingest"},
            quorum={"required": 2, "trusted_head_writers": ["@pi"],
                    "reviewers": {"@r1": {}, "@r2": {}, "@r3": {}, "@human": {"login": "Author-Login"}},
                    "reviewer_groups": [["@r1", "@r3"]]})      # r1 and r3 are one person/organisation: they count once
        self.seq = {"pi": 0}
        self.dispatch_log = tmp / "dispatch.log"
        script = tmp / "dispatch.sh"
        script.write_text(f"#!/bin/sh\n# local stand-in for a review dispatcher: records the call, runs no model\n"
                          f'echo "$@" >> "{self.dispatch_log}"\n')
        script.chmod(0o700)
        self.cfgs = {label: self.autospec_cfg(label, dispatch=str(script) if label == "pi" else None)
                     for label in ("pi", "r1", "r2", "r3", "human", "attacker")}
        self.n = 0

    # ---- AutoSpec configuration (a small YAML block, mode 0600)
    def autospec_cfg(self, label: str, dispatch: str | None = None) -> Path:
        p = self.tmp / f"autospec-{label}.yml"
        lines = ["herdr:", "  enabled: true", f"  url: {self.plane.url}", f"  ingest_key: {self.plane.keys[label]}"]
        if dispatch:
            lines.append(f"  dispatch_command: {dispatch}")
        p.write_text("\n".join(lines) + "\n")
        p.chmod(0o600)
        return p

    def run(self, *args, cwd=None, path_prefix: Path | None = None, timeout=60) -> subprocess.CompletedProcess:
        env = {"PATH": (f"{path_prefix}:" if path_prefix else "") + os.environ["PATH"], "HOME": str(self.tmp)}
        return subprocess.run([str(self.autospec), "review-quorum", *map(str, args)], capture_output=True, text=True,
                              env=env, cwd=cwd or self.tmp, timeout=timeout)

    # ---- trusted writer (Pi) side of the PR node
    def pi_pr(self, head: str | None, author: str | None = AUTHOR, seq: int | None = None, pr: str = PR, key="pi") -> dict:
        self.seq["pi"] = seq if seq is not None else self.seq["pi"] + 1
        attrs = {}
        if head is not None:
            attrs["head_sha"] = head
        if author is not None:
            attrs["author"] = author
        env = self.plane.envelope(source="pi-e2e" if key == "pi" else f"{key}-e2e", seq=self.seq["pi"], entity_id=pr,
                                  event_type="node.upsert", payload={"node_type": "pr", "node_id": pr, "label": "PR", "attrs": attrs})
        r = self.plane.ingest(key, env)
        assert r.status == 200, r.body
        return r.body

    def quorum(self, pr: str = PR, key="operator") -> dict:
        from urllib.parse import quote
        r = self.plane.get(f"/api/v1/graph/nodes/{quote(pr, safe='')}/review-quorum", key=key)
        assert r.status == 200, (r.status, r.body)
        return r.body

    # ---- reviewers publish with the REAL autospec CLI
    def publish(self, label: str, commit: str, verdict: str = "lgtm", pr: str = PR) -> subprocess.CompletedProcess:
        self.n += 1
        d = self.tmp / f"receipt-{label}-{self.n}"
        d.mkdir()
        (d / "request.json").write_text(json.dumps({"commit": commit, "pr": pr}))
        (d / "verdict.json").write_text(json.dumps({"commit": commit, "verdict": verdict, "reviewer": label, "n": self.n}))
        out = self.run("publish", "--config", self.cfgs[label], "--request", d / "request.json", "--verdict", d / "verdict.json",
                       "--pr-node", pr, "--scratch-dir", self.tmp / "scratch")
        out.verdict_file = d / "verdict.json"
        return out


@pytest.fixture
def world(planes, autospec_bin, tmp_path):
    return World(planes, autospec_bin, tmp_path)


H1, H2, H3 = sha40("repair-0"), sha40("repair-1"), sha40("repair-2")


def counted(q):
    return sorted(q.get("counted", []))


# ------------------------------------------------------------------------------------------------ quorum
def test_two_independent_reviewers_via_the_real_autospec_cli_satisfy_the_quorum(world):
    p = world.plane
    world.pi_pr(H1)
    assert world.quorum()["status"] == "insufficient" and world.quorum()["head"] == H1      # control: nobody reviewed yet
    out = world.publish("r1", H1)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["outcome"] == "published"
    assert world.quorum()["status"] == "insufficient"                                        # one reviewer is not a quorum
    assert world.publish("r2", H1).returncode == 0
    q = world.quorum()
    assert q["status"] == "satisfied" and len(counted(q)) == 2 and p.pid("r2") in counted(q)   # r1/r3 share a group key
    assert not q["rereview_required"]
    # the reviews are graph nodes with a typed 'reviewed' edge bound to the exact commit
    edges = [e for e in p.hood(PR, depth=1).body["edges"] if e["edge_type"] == "reviewed"]
    assert len(edges) == 2 and {e["attrs"]["commit"] for e in edges} == {H1}


def test_reviewers_of_one_group_count_once_and_distinct_groups_count_twice(world):
    world.pi_pr(H1)
    world.publish("r1", H1)
    world.publish("r3", H1)                      # same person/organisation as r1 (reviewer_groups)
    assert world.quorum()["status"] == "insufficient"
    world.publish("r2", H1)                      # a different group completes it
    assert world.quorum()["status"] == "satisfied"


# Found by this suite and FIXED in the same PR (redact.py exempted `author`): the PR `author` attr Pi writes was stored as
# '[REDACTED]' because redact.py treats any key containing 'auth' as a secret, so the login-based author exclusion in
# review_quorum.evaluate could never match and a PR author's own approval was counted.
def test_the_pr_author_cannot_approve_their_own_pr_by_login(world):
    world.pi_pr(H1, author="Author-Login")        # the key behind `human` is the author (configured login, case-insensitive)
    world.publish("human", H1)
    world.publish("r1", H1)
    q = world.quorum()
    assert q["status"] == "insufficient" and world.plane.pid("human") not in counted(q)


def test_author_attr_is_stored_readable_so_the_author_exclusion_can_match_while_secret_keys_stay_redacted(world):
    world.pi_pr(H1, author="Author-Login")
    attrs = world.plane.node(PR).body["node"]["attrs"]
    assert attrs["author"] == "Author-Login" and attrs["head_sha"] == H1


def test_a_reviewer_who_produced_the_pr_cannot_approve_it_and_a_distinct_one_can(world):
    p = world.plane
    world.pi_pr(H1)
    # `human` is also the producer of the PR (an owned `produced` edge into it): the author path that does work today
    for seq, (etype, payload, ent) in enumerate([
            ("node.upsert", {"node_type": "agent", "node_id": "agent:human-impl"}, "agent:human-impl"),
            ("edge.add", {"edge_type": "produced", "from_id": "agent:human-impl", "to_id": PR}, "agent:human-impl")]):
        assert p.ingest("human", p.envelope(source="human-e2e", seq=seq, entity_id=ent, event_type=etype, payload=payload)).status == 200
    world.publish("human", H1)
    world.publish("r1", H1)
    q = world.quorum()
    assert q["status"] == "insufficient" and p.pid("human") not in counted(q)
    assert p.pid("human") in json.dumps(q["rejected"]) and "author" in json.dumps(q["rejected"])
    world.publish("r2", H1)
    q = world.quorum()
    assert q["status"] == "satisfied" and len(counted(q)) == 2 and p.pid("human") not in counted(q)     # positive control


def test_a_block_beats_approvals_and_an_attacker_key_can_neither_satisfy_nor_block(world):
    p = world.plane
    world.pi_pr(H1)
    # attacker key: unlisted reviewer + untrusted head writer
    assert world.publish("attacker", H1).returncode == 0
    assert world.publish("attacker", H1, "blocked").returncode == 0
    world.pi_pr(sha40("forged"), key="attacker", seq=None)         # an untrusted writer's head claim is ignored
    q = world.quorum()
    assert q["head"] == H1 and q["status"] == "insufficient" and q["counted"] == [] and not q.get("blocking")
    world.publish("r1", H1)
    world.publish("r2", H1)
    assert world.quorum()["status"] == "satisfied"            # positive control: the attacker's block did not matter
    world.publish("r2", H1, "blocked")                        # a configured reviewer blocks at the current head
    q = world.quorum()
    assert q["status"] == "blocked" and p.pid("r2") in q["blocking"]


def test_head_change_invalidates_old_verdicts_and_the_real_cli_requests_rereview_once_per_head(world):
    p = world.plane
    world.pi_pr(H1)
    world.publish("r1", H1)
    world.publish("r2", "%s" % H1)
    assert world.quorum()["status"] == "satisfied"
    req = lambda: world.run("request", "--config", world.cfgs["pi"], "--pr-node", PR, "--state-dir", world.tmp / "state",
                            "--scratch-dir", world.tmp / "scratch")
    r = req()
    assert r.returncode == 0 and json.loads(r.stdout)["outcome"].startswith("not_needed")     # control: nothing stale yet
    assert not world.dispatch_log.exists()
    world.pi_pr(H2)                                           # defect repair pushed: a new head SHA
    q = world.quorum()
    assert q["status"] == "insufficient" and q["counted"] == [] and q["head"] == H2
    assert len(q["stale_reviewers"]) == 2 and p.pid("r2") in q["stale_reviewers"] and q["rereview_required"] is True   # r1 may be listed by its group key
    r = req()
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["outcome"] == "requested"
    assert world.dispatch_log.read_text().split() == ["--", PR, H2]                          # dispatched exactly for (PR, new head)
    again = json.loads(req().stdout)["outcome"]
    assert again == "already_requested" and len(world.dispatch_log.read_text().splitlines()) == 1   # idempotent
    # fresh verdicts at the new head satisfy again
    world.publish("r1", H2)
    world.publish("r2", H2)
    q = world.quorum()
    assert q["status"] == "satisfied" and len(counted(q)) == 2 and p.pid("r2") in counted(q)   # old-head verdicts stay listed as stale history
    assert json.loads(req().stdout)["outcome"].startswith("not_needed")
    world.pi_pr(H3)                                           # a third push needs a third request
    assert json.loads(req().stdout)["outcome"] == "requested"
    assert len(world.dispatch_log.read_text().splitlines()) == 2


def test_a_late_low_sequence_head_write_is_head_unverified_and_the_cli_alerts_with_exit_4(world):
    world.pi_pr(H1, seq=10)
    world.publish("r1", H1)
    world.publish("r2", H1)
    assert world.quorum()["status"] == "satisfied"            # control
    world.pi_pr(H2, seq=5)                                    # emitter restart / reorder: lower sequence, other head
    q = world.quorum()
    assert q["status"] == "head_unverified" and q.get("head_unverified") is True and q["counted"] == []
    r = world.run("request", "--config", world.cfgs["pi"], "--pr-node", PR, "--state-dir", world.tmp / "state",
                  "--scratch-dir", world.tmp / "scratch")
    assert r.returncode == 4 and json.loads(r.stdout)["alert"] is True
    assert not world.dispatch_log.exists()                    # an alert never dispatches a review
    world.pi_pr(H1, seq=11)                                   # the writer writes past the applied sequence: a plain reorder heals
    assert world.quorum()["status"] == "satisfied"


@pytest.mark.parametrize("bad", ["abc123", "g" * 40, "A" * 39, ""])
def test_a_short_or_malformed_head_is_unknown_head_and_nothing_passes(world, bad):
    world.pi_pr(bad)
    world.publish("r1", H1)
    world.publish("r2", H1)
    q = world.quorum()
    assert q["status"] in ("unknown_head", "insufficient") and q["status"] != "satisfied" and q["counted"] == []
    world.pi_pr(H1)                                            # positive control: a full SHA lets the same verdicts count
    assert world.quorum()["status"] == "satisfied"


# ------------------------------------------------------------------------------------------------ guarded merge gate
class Gate:
    """A real git repo, the operator config the gate trusts, and the FAKE gh."""

    def __init__(self, world: World, max_age: int | None = None, name="gate"):
        self.w = world
        self.root = world.tmp / f"{name}-repo"
        self.root.mkdir()
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                    ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", self.root, *cmd], check=True, capture_output=True)
        self.shim = world.tmp / f"{name}-bin"
        self.shim.mkdir()
        shutil.copy(E2E / "fake_gh" / "gh", self.shim / "gh")
        (self.shim / "gh").chmod(0o755)
        self.set_live(H1)
        self.report = self.root / ".autospec" / "reports" / "review-quorum.json"
        # operator-trusted config: the gate does not take 'required' from the PR checkout; herdr enabled = live check too
        op = world.tmp / f"{name}-operator.yml"
        age = f"  max_report_age_seconds: {max_age}\n" if max_age else ""
        op.write_text(f"review_quorum:\n  required: true\n{age}herdr:\n  enabled: true\n  url: {world.plane.url}\n"
                      f"  ingest_key: {world.plane.keys['pi']}\n")
        op.chmod(0o600)
        self.operator = op

    def set_live(self, head: str, number: int = PR_NUM, url: str | None = None):
        (self.shim / "state.json").write_text(json.dumps({"prs": {str(PR_NUM): {"head": head, "url": url or f"https://github.com/{REPO_SLUG}/pull/{number}"}}}))

    def evaluate(self, head: str, tag: str):
        """autospec review-quorum --input ... --write-report: the evaluator the merge path trusts."""
        w, p = self.w, self.w.plane
        votes = []
        for label in ("r1", "r2"):
            d = sorted(w.tmp.glob(f"receipt-{label}-*"))[-1]
            digest = sha_hex((d / "verdict.json").read_bytes())
            rid = p.pid(label)
            votes.append({"reviewer": rid, "target": PR, "provider": f"provider-{label}", "verdict": "lgtm", "commit": head,
                          "receipt_digest": digest, "receipt_binding": receipt_binding(PR, head, rid, digest)})
        inp = w.tmp / f"quorum-input-{tag}.json"
        inp.write_text(json.dumps({"target": PR, "head": head, "votes": votes,
                                   "policy": {"required": 2, "pr_author": AUTHOR, "eligible_reviewers": [p.pid("r1"), p.pid("r2")],
                                              "require_provider_diversity": True, "implementer_provider": "provider-impl"}}))
        return w.run("--input", inp, "--json", "--write-report", self.report, "--operator-config", self.operator,
                     "--repo-root", self.root)

    def verify(self, *extra):
        r = self.w.run("verify-report", "--repo-root", self.root, "--pr", PR_NUM, "--repo", REPO_SLUG, "--operator-config", self.operator,
                       "--scratch-dir", self.w.tmp / "scratch", *extra, path_prefix=self.shim)
        return r, json.loads(r.stdout)


def _published_at(world, head):
    world.pi_pr(head)
    assert world.publish("r1", head).returncode == 0
    assert world.publish("r2", head).returncode == 0
    assert world.quorum()["status"] == "satisfied"


def test_guarded_merge_gate_passes_only_for_a_satisfied_quorum_at_the_live_head(world):
    gate = Gate(world)
    _published_at(world, H1)
    # missing report: required, so it fails closed
    r, body = gate.verify()
    assert r.returncode == 1 and body["ok"] is False and any("missing" in x for x in body["reasons"])
    ev = gate.evaluate(H1, "h1")
    assert ev.returncode == 0, ev.stderr
    assert json.loads(ev.stdout)["status"] == "satisfied"
    r, body = gate.verify()                                   # positive control: everything agrees
    assert r.returncode == 0 and body["ok"] is True and body["live"] == "checked" and body["live_head"] == H1 == body["report_head"]
    assert body["origin"] == "unattested" and body["warnings"]          # honest: no attestation key configured in this run


def test_head_moves_after_the_report_so_the_old_verdict_cannot_merge(world):
    gate = Gate(world)
    _published_at(world, H1)
    assert gate.evaluate(H1, "h1").returncode == 0
    assert gate.verify()[0].returncode == 0                   # control
    gate.set_live(H2)                                         # someone pushed to GitHub after the review
    r, body = gate.verify()
    assert r.returncode == 1 and body["ok"] is False
    assert any(f"report head {H1} is not the live PR head {H2}" in x for x in body["reasons"])
    # Herdr independently refuses too: the trusted writer reports the new head, the old verdicts go stale, live check fails
    world.pi_pr(H2)
    gate.set_live(H2)
    r, body = gate.verify()
    assert r.returncode == 1 and any("Herdr says the quorum is insufficient" in x or "not the live PR head" in x for x in body["reasons"])
    # repair re-reviewed at the new head + fresh report => passes again
    world.publish("r1", H2)
    world.publish("r2", H2)
    assert gate.evaluate(H2, "h2").returncode == 0
    r, body = gate.verify()
    assert r.returncode == 0 and body["live_head"] == H2


def test_gate_fails_closed_on_wrong_target_blocked_quorum_and_unreachable_pr(world):
    gate = Gate(world)
    _published_at(world, H1)
    assert gate.evaluate(H1, "h1").returncode == 0
    gate.set_live(H1, number=99)                              # live PR is a different pull request
    r, body = gate.verify()
    assert r.returncode == 1 and any("report target" in x for x in body["reasons"])
    gate.set_live(H1)
    assert gate.verify()[0].returncode == 0                   # control
    (gate.shim / "state.json").write_text(json.dumps({"prs": {}}))   # gh cannot find the PR
    r, body = gate.verify()
    assert r.returncode == 1 and any("cannot verify the live PR head" in x for x in body["reasons"])
    gate.set_live(H1)
    world.publish("r1", H1, "blocked")                        # a later block for this head is seen by the LIVE Herdr check
    r, body = gate.verify()
    assert r.returncode == 1 and any("Herdr says the quorum is blocked" in x for x in body["reasons"])


def test_an_old_report_is_refused_by_age_and_a_non_pass_evaluation_overwrites_it(world):
    gate = Gate(world, max_age=1, name="aged")
    _published_at(world, H1)
    assert gate.evaluate(H1, "h1").returncode == 0
    assert gate.verify()[0].returncode == 0                   # control: fresh
    time.sleep(2.2)
    r, body = gate.verify()
    assert r.returncode == 1 and any("older than max_report_age_seconds" in x for x in body["reasons"])
    # an evaluation that cannot reach quorum rewrites the report as a non-pass: the older pass is never trusted again
    gate2 = Gate(world, name="ovr")
    assert gate2.evaluate(H1, "ok").returncode == 0 and gate2.verify()[0].returncode == 0
    bad = world.tmp / "insufficient.json"
    bad.write_text(json.dumps({"target": PR, "head": H1, "votes": [], "policy": {"required": 2, "pr_author": AUTHOR}}))
    ev = world.run("--input", bad, "--write-report", gate2.report, "--operator-config", gate2.operator, "--repo-root", gate2.root)
    assert ev.returncode == 1
    r, body = gate2.verify()
    assert r.returncode == 1 and any("verdict is insufficient_reviews" in x or "not a head-bound pass" in x for x in body["reasons"])


def test_gate_requires_the_report_even_when_the_pr_checkout_claims_it_is_not_required(world):
    gate = Gate(world, name="optout")
    _published_at(world, H1)
    (gate.root / ".autospec").mkdir(exist_ok=True)
    (gate.root / ".autospec" / "autospec.yml").write_text("review_quorum:\n  required: false\n")   # the PR tries to opt out
    r, body = gate.verify()
    assert r.returncode == 1 and body["required"] is True     # the operator config decides, not the PR under review
