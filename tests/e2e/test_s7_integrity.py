"""ACCEPTANCE SCENARIO 7 (non-visual part, Herdr half): the integrity checks around review decisions.

(b) Plannotator digest echo. The REAL Pi plan CLI (`scripts/pi-engineering.ts plan --review require`) launches the REAL
    Herdr bridge (`python -m herdr_engineering.plannotator`) which talks to a REAL control-plane process backed by a
    throwaway PostgreSQL. Only the upstream `plannotator` binary (a browser UI plus a human) is a FAKE shim.
    The server's stored review must carry exactly the digest Pi computed and the git HEAD the bridge found.
(c) PR head SHA validity, at the ingest level: which `head_sha` values the quorum accepts, who may set it, what happens
    to old verdicts when it moves, and which review receipts are counted. (The AutoSpec-binary flow is scenario 2.)

Every refusal below is paired with a positive control that proves the same path works for a legitimate caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.e2e import harness, pg
from tests.e2e.harness import ROOT, Plane, sha40


def sha_hex(b: bytes | str) -> str:
    return hashlib.sha256(b.encode() if isinstance(b, str) else b).hexdigest()


# ------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    cfg = harness.load_acceptance()
    bins = pg.find_bins(cfg)
    if bins is None:
        pytest.skip("no PostgreSQL server binaries under acceptance.yaml postgres.bin_dirs")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.fail("asyncpg is a declared dependency (pyproject.toml) and is missing from this python")
    db = pg.ThrowawayPostgres(bins, tmp_path_factory.mktemp("pg7")).start()
    yield db
    db.stop()


def _plane(tmp: Path, name: str, **kw) -> Plane:
    cfg = harness.load_acceptance()
    return Plane(tmp, name=name, request_timeout=float(cfg["request_timeout_seconds"]),
                 startup_timeout=float(cfg["startup_timeout_seconds"]), **kw)


# ================================================================================================ (b) Plannotator
PLAN = "# Plan\n\n1. add the endpoint\n2. add the tests\n"


def register_body(review_id: str, plan: str = PLAN, **over) -> dict:
    return {"review_id": review_id, "kind": "plan", "title": "Plan review", "instance_id": "inst-" + review_id,
            "revision": sha40("rev-" + review_id), "plan": plan, "repository": "github.com/acme/widgets",
            "mission_id": "M-" + review_id, **over}


def binding_of(body: dict) -> dict:
    return {"instance_id": body["instance_id"], "revision": body["revision"], "plan_digest": sha_hex(body["plan"])}


@pytest.fixture
def review_plane(planes, postgres):
    return planes("reviews", keys={"host": "operator", "ingest": "ingest"}, pg_dsn=postgres.dsn)


def test_reviews_are_fail_closed_without_a_durable_store_and_work_with_one(planes, review_plane):
    """No PostgreSQL behind the plane: no launch or decision is acknowledged (503). Control: with PostgreSQL it is."""
    memory_only = planes("memory-only", keys={"host": "operator"})
    refused = memory_only.post("/api/v1/reviews", register_body("rev_nomem"), key="host")
    assert refused.status == 503, (refused.status, refused.body)
    assert "persistence" in json.dumps(refused.body).lower()
    ok = review_plane.post("/api/v1/reviews", register_body("rev_mem"), key="host")
    assert ok.status in (200, 201), (ok.status, ok.body)
    assert ok.body.get("review_id") == "rev_mem" or ok.body.get("review", {}).get("review_id") == "rev_mem"


def _review(plane: Plane, rid: str) -> dict:
    r = plane.get(f"/api/v1/reviews/{rid}", key="host")
    assert r.status == 200, (r.status, r.body)
    return r.body.get("review", r.body)


def test_server_binds_each_receipt_to_instance_revision_and_the_sha256_of_the_exact_plan(review_plane):
    p = review_plane
    body = register_body("rev_bind")
    assert p.post("/api/v1/reviews", body, key="host").status in (200, 201)
    good = binding_of(body)
    stored = _review(p, "rev_bind")
    assert stored["plan_digest"] == good["plan_digest"] == sha_hex(PLAN), "the server hashed the exact plan bytes"
    assert stored["revision"] == body["revision"] and stored["status"] == "starting"

    tampered = {
        "wrong plan digest (another plan)": {**good, "plan_digest": sha_hex(PLAN + "\n3. drop the table\n")},
        "wrong revision": {**good, "revision": sha40("someone-elses-commit")},
        "wrong instance (another launch)": {**good, "instance_id": "inst-other"},
        "missing digest": {k: v for k, v in good.items() if k != "plan_digest"},
    }
    for label, receipt in tampered.items():
        for suffix, extra in (("heartbeat", {}), ("result", {"result": {"decision": "approved"}}),
                              ("failure", {"error": "x"})):
            r = p.post(f"/api/v1/reviews/rev_bind/{suffix}", {**receipt, **extra}, key="host")
            assert r.status == 400, (label, suffix, r.status, r.body)
    still = _review(p, "rev_bind")
    assert still["status"] == "starting" and still["decision"] is None, "no tampered receipt changed the review"

    # positive control: the same call with the matching binding is accepted and finalises the review
    done = p.post("/api/v1/reviews/rev_bind/result", {**good, "result": {"decision": "approved"}}, key="host")
    assert done.status == 200, (done.status, done.body)
    final = _review(p, "rev_bind")
    assert final["status"] == "approved" and final["decision"] == "approved"
    assert final["plan_digest"] == good["plan_digest"]


def test_a_finished_review_is_idempotent_only_for_the_identical_outcome(review_plane):
    p = review_plane
    body = register_body("rev_final")
    p.post("/api/v1/reviews", body, key="host")
    receipt = {**binding_of(body), "result": {"decision": "approved"}}
    assert p.post("/api/v1/reviews/rev_final/result", receipt, key="host").status == 200
    assert p.post("/api/v1/reviews/rev_final/result", receipt, key="host").status == 200            # replay: same outcome
    flip = p.post("/api/v1/reviews/rev_final/result",
                  {**binding_of(body), "result": {"decision": "denied", "feedback": "changed my mind"}}, key="host")
    assert flip.status == 400, (flip.status, flip.body)
    late_fail = p.post("/api/v1/reviews/rev_final/failure", {**binding_of(body), "error": "late"}, key="host")
    assert late_fail.status == 200 and _review(p, "rev_final")["status"] == "approved", "a failure cannot undo an approval"
    # the same review id cannot be re-registered for another plan or revision
    assert p.post("/api/v1/reviews", register_body("rev_final", plan=PLAN + "x"), key="host").status == 400
    assert p.post("/api/v1/reviews", body, key="host").status in (200, 201), "re-registering the identical launch is fine"


def test_only_the_operator_role_may_launch_or_decide_a_review(review_plane):
    p = review_plane
    body = register_body("rev_role")
    assert p.post("/api/v1/reviews", body, key="ingest").status == 403, "an ingest key is not a review launcher"
    assert p.post("/api/v1/reviews", body, key="viewer").status == 403
    assert p.post("/api/v1/reviews", body, key="host").status in (200, 201)                          # control
    receipt = {**binding_of(body), "result": {"decision": "approved"}}
    assert p.post("/api/v1/reviews/rev_role/result", receipt, key="ingest").status == 403
    assert _review(p, "rev_role")["status"] == "starting"
    assert p.post("/api/v1/reviews/rev_role/result", receipt, key="host").status == 200               # control


# ---- the real Pi plan CLI -> the real Herdr bridge -> the real control plane
FAKE_UPSTREAM = """#!/bin/sh
# FAKE - test shim. Stands in for the upstream `plannotator` binary (a browser UI plus a human reviewer).
# The bridge calls:  plannotator annotate <frozen plan> --gate --json
here="$(dirname "$0")/.."
[ "$1" = annotate ] || exit 64
sha256sum "$2" | cut -d' ' -f1 >> "$here/reviewed.log"
case "$(head -n1 "$here/mode")" in
  approve) printf '{"decision":"approved"}\\n' ;;
  annotate) printf '{"decision":"annotated","feedback":"split the auth change"}\\n' ;;
esac
"""

WRAPPER = """#!/bin/sh
# Launcher for Pi's --plannotator-command: the REAL Herdr bridge, pointed at the test control plane.
# The bridge reads its control-plane key from ~/.config/herdr-engineering/control-plane-key (HOME below).
here="$(dirname "$0")"
start="$(pwd)"
HOME="$here/home"; export HOME
PATH="$here/fakebin:$PATH"; export PATH
cd "{herdr_repo}" || exit 70
python3 -m herdr_engineering.plannotator "$@" --cwd "$start" --control-plane "{url}" 2>"$here/bridge.err"\nrc=$?
cat "$here/bridge.err" >&2
exit $rc
"""


class Bridge:
    def __init__(self, tmp: Path, plane: Plane, operator_key: str):
        self.dir = tmp / "bridge"
        (self.dir / "fakebin").mkdir(parents=True)
        cfgdir = self.dir / "home" / ".config" / "herdr-engineering"
        cfgdir.mkdir(parents=True)
        keyfile = cfgdir / "control-plane-key"
        keyfile.write_text(operator_key + "\n")
        keyfile.chmod(0o600)
        up = self.dir / "fakebin" / "plannotator"
        up.write_text(FAKE_UPSTREAM)
        up.chmod(0o700)
        (self.dir / "mode").write_text("approve\n")
        self.command = self.dir / "launch-bridge"
        self.command.write_text(WRAPPER.format(herdr_repo=ROOT, url=plane.url))
        self.command.chmod(0o700)

    def set_mode(self, mode: str):
        (self.dir / "mode").write_text(mode + "\n")

    def errors(self) -> str:
        f = self.dir / "bridge.err"
        return f.read_text() if f.exists() else ""

    def reviewed(self) -> list[str]:
        f = self.dir / "reviewed.log"
        return f.read_text().split() if f.exists() else []


def _git_repo(path: Path) -> str:
    path.mkdir(parents=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    def git(*a):
        return subprocess.run(["git", *a], cwd=path, env=env, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (path / "README.md").write_text("e2e\n")
    git("add", "README.md")
    git("-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid", "commit", "-q", "-m", "seed")
    return git("rev-parse", "HEAD")


def _pi_plan(pi_repo: Path, tmp: Path, cwd: Path, command: Path, body: dict) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    assert node, "node is required for the Pi side of this scenario"
    agent = tmp / "pi-agent"
    agent.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp / "pi-home"), "PI_CODING_AGENT_DIR": str(agent),
           "PI_ENGINEERING_STATE_DIR": str(tmp / "pi-state"), "PI_ENGINEERING_HOME": str(tmp / "pi-eng-home")}
    return subprocess.run([node, str(pi_repo / "scripts/pi-engineering.ts"), "plan", "--store", str(tmp / "pi-store"),
                           "--plannotator-command", str(command)],
                          input=json.dumps(body), capture_output=True, text=True, env=env, cwd=cwd, timeout=120)


def high_risk(tag: str) -> dict:
    goal = f"change login token handling ({tag})"
    return {"goal": goal, "tasks": [{"objective": goal, "write_domains": ["src/auth/**"]}]}


def _stored_reviews(plane: Plane, plan_digest: str) -> list[dict]:
    """Stored reviews of exactly this plan (the database is shared by the module's tests)."""
    r = plane.get("/api/v1/reviews", key="host")
    assert r.status == 200, (r.status, r.body)
    return [x for x in r.body.get("reviews", []) if x.get("plan_digest") == plan_digest]


def test_pi_plan_gate_through_the_real_bridge_stores_pis_digest_and_the_git_head(planes, review_plane, pi_repo, tmp_path):
    p = review_plane
    head = _git_repo(tmp_path / "work", )
    bridge = Bridge(tmp_path, p, p.keys["host"])

    # control: the high-risk plan really is gated (no bridge -> Pi blocks)
    out = _pi_plan(pi_repo, tmp_path, tmp_path / "work", tmp_path / "does-not-exist", high_risk("approve"))
    assert out.returncode == 1 and json.loads(out.stdout.strip().splitlines()[-1])["result"] == "blocked", out.stdout

    bridge.set_mode("approve")
    out = _pi_plan(pi_repo, tmp_path, tmp_path / "work", bridge.command, high_risk("approve"))
    assert out.returncode == 0, (out.stdout, out.stderr, bridge.errors())
    pi = json.loads(out.stdout.strip().splitlines()[-1])
    assert pi["result"] == "may_proceed" and pi["outcome"] == "approved"

    stored = _stored_reviews(p, pi["planDigest"])
    assert len(stored) == 1, stored
    review = _review(p, stored[0]["review_id"])
    assert review["status"] == "approved" and review["decision"] == "approved"
    assert review["plan_digest"] == pi["planDigest"], "the server holds the digest Pi computed for its plan"
    assert review["revision"] == head, "the bridge bound the review to the repository's git HEAD"
    assert review["mission_id"] == pi["missionId"]
    assert bridge.reviewed() == [pi["planDigest"]], "the upstream reviewer saw exactly the plan Pi digested"


def test_a_changes_requested_review_is_stored_as_such_and_pi_does_not_proceed(planes, review_plane, pi_repo, tmp_path):
    p = review_plane
    _git_repo(tmp_path / "work")
    bridge = Bridge(tmp_path, p, p.keys["host"])
    bridge.set_mode("annotate")
    out = _pi_plan(pi_repo, tmp_path, tmp_path / "work", bridge.command, high_risk("annotate"))
    pi = json.loads(out.stdout.strip().splitlines()[-1])
    assert out.returncode != 0 and pi["result"] != "may_proceed", out.stdout
    review = _review(p, _stored_reviews(p, pi["planDigest"])[0]["review_id"])
    assert review["status"] == "changes_requested" and review["decision"] == "annotated"
    assert review["plan_digest"] == pi["planDigest"]
    assert "split the auth change" in (review.get("feedback") or "")


# ================================================================================================ (c) PR head validity
REPO_PR = "pr:github.com/o/r#7"
AUTHOR = "someone-else"
H1, H2, H3 = sha40("head-1"), sha40("head-2"), sha40("head-3")


def receipt_binding(pr: str, commit: str, reviewer_pid: str, digest: str) -> str:
    return sha_hex(f"pr-review-receipt-v1\0{pr.lower()}\0{commit}\0{reviewer_pid}\0{digest}")


class QuorumWorld:
    def __init__(self, planes):
        self.plane = planes(
            "quorum",
            keys={"pi": "ingest", "r1": "ingest", "r2": "ingest", "attacker": "ingest"},
            quorum={"required": 2, "trusted_head_writers": ["@pi"], "reviewers": {"@r1": {}, "@r2": {}}})
        self.seq: dict[str, int] = {}
        self.n = 0

    def _next(self, label: str) -> int:
        self.seq[label] = self.seq.get(label, 0) + 1
        return self.seq[label]

    def send(self, label: str, entity_id: str, event_type: str, payload: dict, seq: int | None = None):
        s = seq if seq is not None else self._next(label)
        env = self.plane.envelope(source=f"{label}-e2e", seq=s, entity_id=entity_id, event_type=event_type, payload=payload)
        return self.plane.ingest(label, env)

    def set_head(self, head, *, pr=REPO_PR, label="pi", author: str | None = AUTHOR, seq: int | None = None):
        attrs = {"head_sha": head}
        if author is not None:
            attrs["author"] = author
        r = self.send(label, pr, "node.upsert", {"node_type": "pr", "node_id": pr, "label": "PR", "attrs": attrs}, seq)
        assert r.status == 200, (r.status, r.body)
        return r

    def review(self, label: str, commit: str, verdict: str = "lgtm", *, pr: str = REPO_PR, digest: str | None = None,
               binding: str | None = "auto"):
        self.n += 1
        digest = digest if digest is not None else sha_hex(f"receipt-{label}-{self.n}")
        node = f"review:{label}-{self.n}"
        attrs = {"commit": commit, "verdict": verdict, "receipt_digest": digest}
        if binding == "auto":
            attrs["receipt_binding"] = receipt_binding(pr, commit, self.plane.pid(label), digest)
        elif binding is not None:
            attrs["receipt_binding"] = binding
        a = self.send(label, node, "node.upsert", {"node_type": "review", "node_id": node, "label": "review"})
        assert a.status == 200, (a.status, a.body)
        e = self.send(label, node, "edge.add", {"edge_type": "reviewed", "from_id": node, "to_id": pr, "attrs": attrs})
        assert e.status == 200, (e.status, e.body)

    def quorum(self, pr: str = REPO_PR) -> dict:
        r = self.plane.get(f"/api/v1/graph/nodes/{quote(pr, safe='')}/review-quorum", key="operator")
        assert r.status == 200, (r.status, r.body)
        return r.body


@pytest.fixture
def qw(planes):
    return QuorumWorld(planes)


def test_a_valid_full_head_is_adopted_and_review_counts_need_it(qw):
    qw.set_head(H1)
    q = qw.quorum()
    assert q["head"] == H1 and q["status"] == "insufficient" and q["rereview_required"] is True
    qw.review("r1", H1)
    qw.review("r2", H1)
    q = qw.quorum()
    assert q["status"] == "satisfied" and sorted(q["counted"]) == sorted([qw.plane.pid("r1"), qw.plane.pid("r2")])


@pytest.mark.parametrize("label,value,expected_head", [
    ("short sha", H1[:7], None),
    ("39 hex", H1[:39], None),
    ("41 hex", H1 + "a", None),
    ("non-hex 40", "z" * 40, None),
    ("branch name", "main", None),
    ("empty", "", None),
    ("sha1: prefix", "sha1:" + H1, None),
    ("uppercase 40", H1.upper(), H1),          # documented: ASCII-lowercased before matching
    ("64 hex (sha-256 object id)", sha_hex("tree"), sha_hex("tree")),
])
def test_only_a_full_hex_object_id_is_a_usable_pr_head(qw, label, value, expected_head):
    pr = "pr:github.com/o/r#" + str(100 + sum(map(ord, label)))
    qw.set_head(H2, pr=pr + "0", author=AUTHOR)                          # control: a legitimate head on a sibling PR works
    assert qw.quorum(pr + "0")["head"] == H2
    r = qw.plane.ingest("pi", qw.plane.envelope(
        source="pi-e2e", seq=qw._next("pi"), entity_id=pr, event_type="node.upsert",
        payload={"node_type": "pr", "node_id": pr, "label": "PR", "attrs": {"head_sha": value, "author": AUTHOR}}))
    assert r.status in (200, 400), (label, r.status, r.body)             # stored as a value, or refused at ingest
    if r.status == 400:
        return
    q = qw.quorum(pr)
    assert q["head"] == expected_head, (label, q)
    if expected_head is None:
        assert q["status"] == "unknown_head" and q["counted"] == [], "nothing can pass without a usable head"


def test_only_a_configured_head_writer_can_set_the_head(qw):
    qw.set_head(H1)
    qw.review("r1", H1)
    qw.review("r2", H1)
    assert qw.quorum()["status"] == "satisfied"
    qw.set_head(H2, label="attacker", author=None)                        # an ingest principal that is not a trusted writer
    q = qw.quorum()
    assert q["head"] == H1 and q["status"] == "satisfied", "the attacker's head did not move the PR head"
    other = "pr:github.com/o/r#8"
    qw.set_head(H1, pr=other, label="attacker", author=AUTHOR)
    assert qw.quorum(other)["status"] == "unknown_head", "with no trusted writer there is no head at all"
    qw.set_head(H2)                                                       # control: the trusted writer does move it
    assert qw.quorum()["head"] == H2


def test_a_new_trusted_head_invalidates_old_verdicts_until_the_reviewers_look_again(qw):
    qw.set_head(H1)
    qw.review("r1", H1)
    qw.review("r2", H1)
    assert qw.quorum()["status"] == "satisfied"
    qw.set_head(H2)                                                       # a repair pushed a new head
    q = qw.quorum()
    assert q["head"] == H2 and q["status"] == "insufficient" and q["counted"] == []
    assert q["rereview_required"] is True and q["rereview_needed"] == 2
    assert sorted(q["stale_reviewers"]) == sorted([qw.plane.pid("r1"), qw.plane.pid("r2")])
    assert {x["reason"] for x in q["rejected"]} == {"stale_head"}
    qw.review("r1", H2)
    assert qw.quorum()["status"] == "insufficient", "one fresh verdict of two is not a quorum"
    qw.review("r2", H2)
    q = qw.quorum()
    assert q["status"] == "satisfied" and len(q["counted"]) == 2, "fresh verdicts at the new head count; the old ones stay history"


def test_a_stale_blocker_stops_blocking_but_a_current_one_blocks(qw):
    qw.set_head(H1)
    qw.review("r1", H1, "blocked")
    assert qw.quorum()["status"] == "blocked"
    qw.set_head(H2)
    q = qw.quorum()
    assert q["status"] != "blocked" and qw.plane.pid("r1") in q["stale_reviewers"], "the block was about the old head"
    qw.review("r1", H2, "blocked")
    assert qw.quorum()["status"] == "blocked"                             # control: the same verdict at the new head blocks


def test_receipts_must_carry_a_digest_and_a_binding_to_this_pr_head_and_reviewer(qw):
    qw.set_head(H1)
    pid1, pid2 = qw.plane.pid("r1"), qw.plane.pid("r2")
    qw.review("r1", H1, binding=None)                                     # no binding
    qw.review("r2", H1, binding=receipt_binding(REPO_PR, H2, pid2, sha_hex("x")), digest=sha_hex("x"))   # bound to another commit
    q = qw.quorum()
    assert q["counted"] == [] and {(x["reviewer"], x["reason"]) for x in q["rejected"]} == {
        (pid1, "bad_receipt_binding"), (pid2, "bad_receipt_binding")}
    qw.review("r1", H1, digest="not-a-digest", binding="0" * 64)
    assert "bad_receipt_digest" in {x["reason"] for x in qw.quorum()["rejected"]}
    qw.review("r1", H1[:12])                                              # a short commit prefix is not a full object id
    assert (pid1, "invalid_commit") in {(x["reviewer"], x["reason"]) for x in qw.quorum()["rejected"]}
    assert qw.quorum()["status"] == "insufficient" and qw.quorum()["counted"] == []
    # control: well-formed receipts are counted
    qw.review("r1", H1)
    qw.review("r2", H1)
    assert qw.quorum()["status"] == "satisfied"


def test_one_receipt_digest_cannot_be_replayed_by_two_reviewers(qw):
    qw.set_head(H1)
    shared = sha_hex("one receipt")
    qw.review("r1", H1, digest=shared)
    assert qw.quorum()["counted"] == [qw.plane.pid("r1")]                # control: alone it counts
    qw.review("r2", H1, digest=shared)
    q = qw.quorum()
    assert q["counted"] == [] and q["status"] == "insufficient"
    assert {x["reason"] for x in q["rejected"]} == {"replayed_receipt"}


def test_a_principal_that_is_not_a_configured_reviewer_is_ignored(qw):
    qw.set_head(H1)
    qw.review("attacker", H1)                                             # well-formed, but nobody configured this key
    qw.review("attacker", H1, "blocked")
    q = qw.quorum()
    assert q["counted"] == [] and q["blocking"] == [] and q["status"] == "insufficient", "neither approves nor blocks"
    qw.review("r1", H1, "blocked")                                        # control: a configured reviewer's block does bite
    assert qw.quorum()["status"] == "blocked"


def test_a_late_head_from_the_trusted_writer_is_never_adopted_silently(qw):
    qw.set_head(H1, seq=5)
    qw.set_head(H2, seq=6)
    assert qw.quorum()["head"] == H2
    qw.set_head(H3, seq=4)                                                # lower sequence, a value never reported before
    q = qw.quorum()
    assert q["status"] == "head_unverified" and q["head_unverified"] is True and q["counted"] == []
    assert q["alert"]
    qw.set_head(H3, seq=7)                                                # the writer writes past the applied sequence
    q = qw.quorum()
    assert q["head"] == H3 and q["head_unverified"] is False and q["status"] != "head_unverified"
