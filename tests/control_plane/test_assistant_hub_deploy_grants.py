"""Standing deploy grants: created only through the real gates, matched by scope (not sha), and never authorizing alone.

Real store, real judge runtime over a real socket.  Preconditions are Task 8: until they exist a standing-grant deploy
must end ``pending``/``deploy_preconditions`` (fail closed)."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantStore,
    _grants_config,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.deploy import match_deploy, scope_key
from herdr_engineering.control_plane.assistant_hub.policy import (
    ActivationRefused,
    create_standing_grant,
    current_judge_version,
    decide,
    record_owner_decision,
    revoke_action_grant,
)
from herdr_engineering.control_plane.assistant_hub.workers import Worker
from tests.control_plane.judge_support import (
    RISKY,
    SAFE,
    WireListener,
    judge_cfg,
    make_runtime,
    satisfy_gate,
    success_body,
    write_key,
)

CMD = "deploy/control-plane/deploy.sh"
REPO = "berlinguyinca/herdr-engineering"
SHA, SHA2 = "0123456789abcdef0123456789abcdef01234567", "fedcba9876543210fedcba9876543210fedcba98"
T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=2)
T, U = "acme", "owner"
DEPLOY = assistant_config({"personal_assistant": {"deploy": {"projects": {"herdr-engineering": {
    "repo": REPO, "scopes": {"control-plane": {"command": CMD, "mode": "auto"},
                             "other": {"command": "deploy/other/run.sh"}}},
    "second": {"repo": "acme/other-repo", "scopes": {"cp": {"command": CMD}}}}}}})["deploy"]
RISK = "outward"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def worker(wid="w1", tenant=T, user=U) -> Worker:
    return Worker(worker_id=wid, tenant_id=tenant, user_id=user, name="n", harness="claude", device_id="d",
                  secret_digest="x", status="active", created_at=T0, expires_at=T0 + timedelta(days=365))


class DWorld:
    def __init__(self, tmp_path, *, body=None, shadow_days=1):
        self.store = AssistantStore()
        self.cfg = _grants_config({"shadow_started_at": T0.isoformat(), "shadow_days": shadow_days})
        self.clock = Clock()
        self.listener = WireListener(success_body(body or SAFE))
        self.jcfg = judge_cfg(f"http://127.0.0.1:{self.listener.port}", write_key(tmp_path), backend_confirmed=True)
        self.judge = make_runtime(self.store, self.jcfg, self.clock)
        self.worker = worker()

    def gate(self, tenant=T, user=U):
        satisfy_gate(self.store, self.judge, RISK, tenant, user)

    def key(self, repo=REPO, cmd=CMD, sha=SHA):
        return scope_key(match_deploy(RawAction(tool="Bash", command=f"{cmd} --ref {sha}", repo=repo), DEPLOY))

    def stand(self, *, worker_id="w1", tenant=T, user=U, key=None, by="slack:U0OWNER01", version=None):
        return create_standing_grant(self.store, self.cfg, self.clock.now, tenant, user, worker_id,
                                     key or self.key(), by,
                                     current_judge_version(self.judge) if version is None else version, self.judge)

    def deploy(self, *, sha=SHA, cmd=CMD, repo=REPO, who=None, session="s1", task="t1", deploy_cfg=DEPLOY):
        return decide(self.store, self.cfg, who or self.worker,
                      RawAction(tool="Bash", command=f"{cmd} --ref {sha}", repo=repo), session, task, self.clock.now,
                      judge=self.judge, deploy_cfg=deploy_cfg)

    def audits(self, action):
        return [e for e in self.store.audit if e["action"] == action]

    def grants(self, tenant=T, user=U):
        return self.store.list_action_grants(tenant, user)


@pytest.fixture
def dw(tmp_path):
    w = DWorld(tmp_path)
    try:
        yield w
    finally:
        w.listener.close()


def refusal(dw, **kw) -> str:
    with pytest.raises(ActivationRefused) as exc:
        dw.stand(**kw)
    assert dw.grants() == []
    return exc.value.code


# ------------------------------------------------------------------------------------- creation
def test_standing_grant_is_active_at_once_with_the_owner_facts(dw):
    dw.gate()
    g = dw.stand()
    assert g.state is GrantState.ACTIVE and g.source == "owner_standing"
    assert g.confirmed_by == "slack:U0OWNER01" and g.activated_at == NOW
    assert g.expires_at == NOW + timedelta(days=int(dw.cfg["grant_ttl_days"]))
    assert g.judge_version == current_judge_version(dw.judge) != ""
    assert g.key_canonical == dw.key().canonical() and g.resource_scope == REPO and g.verb == "deploy.prod"
    assert g.key_id == dw.key().key_id() and dw.key().target.startswith("scope:herdr-engineering/control-plane")
    assert (g.tenant_id, g.user_id, g.worker_id) == (T, U, "w1")
    assert g.effective_state(NOW, int(dw.cfg["policy_version"]), current_judge_version(dw.judge)) is GrantState.ACTIVE
    (a,) = dw.audits("assistant.grant.activated")
    assert a["actor"] == "slack:U0OWNER01" and g.grant_id in a["subject_id"]
    assert SHA not in str(a) and "deploy.sh" not in str(a)


def test_a_second_creation_returns_the_same_live_grant(dw):
    dw.gate()
    first = dw.stand()
    assert dw.stand().grant_id == first.grant_id and len(dw.grants()) == 1
    assert len(dw.audits("assistant.grant.activated")) == 1


def test_a_revoked_or_expired_one_can_be_replaced(dw):
    dw.gate()
    first = dw.stand()
    revoke_action_grant(dw.store, NOW, T, U, first.grant_id, "owner", policy_version=int(dw.cfg["policy_version"]),
                        judge_version=current_judge_version(dw.judge))
    second = dw.stand()
    assert second.grant_id != first.grant_id and second.state is GrantState.ACTIVE


def test_creation_in_the_shadow_period_is_refused(tmp_path):
    w = DWorld(tmp_path, shadow_days=14)
    try:
        w.gate()
        assert refusal(w) == "still_in_shadow"
        (r,) = w.audits("assistant.grant.activate_refused")
        assert "still_in_shadow" in r["detail"] and r["actor"] == "slack:U0OWNER01"
        assert w.audits("assistant.grant.activated") == []
    finally:
        w.listener.close()


@pytest.mark.parametrize("code, setup", [
    ("insufficient_labels", lambda w: None),
    ("backend_unconfirmed", lambda w: (w.gate(), w.jcfg.update(backend_confirmed=False))),
    ("judge_disabled", lambda w: (w.gate(), w.jcfg.update(enabled=False))),
])
def test_the_judge_gate_refuses_creation_with_its_codes(dw, code, setup):
    setup(dw)
    assert refusal(dw) == code
    (r,) = dw.audits("assistant.grant.activate_refused")
    assert code in r["detail"]


def test_no_judge_runtime_refuses_creation(dw):
    dw.gate()
    with pytest.raises(ActivationRefused) as exc:
        create_standing_grant(dw.store, dw.cfg, NOW, T, U, "w1", dw.key(), "slack:U1", "", None)
    assert exc.value.code == "judge_disabled" and dw.grants() == []


def test_insufficient_diversity_and_no_discrimination_and_fn_bound_are_refused(dw):
    from tests.control_plane.judge_support import labeled_records
    for r in labeled_records(dict(dw.jcfg), RISK, keys=2):
        dw.store.add_judge_record(r)
    assert refusal(dw) == "insufficient_diversity"


def test_no_discrimination_is_refused(dw):
    from tests.control_plane.judge_support import labeled_records
    for r in labeled_records(dict(dw.jcfg), RISK, n_flagged=0):
        dw.store.add_judge_record(r)
    assert refusal(dw) == "no_discrimination"


def test_fn_bound_is_refused(dw):
    from tests.control_plane.judge_support import labeled_records
    for r in labeled_records(dict(dw.jcfg), RISK, n_fn=40):
        dw.store.add_judge_record(r)
    assert refusal(dw) == "fn_bound_exceeded"


def test_a_judge_that_cannot_be_evaluated_is_judge_error(dw):
    from herdr_engineering.control_plane.assistant_hub.judge import JudgeRuntime
    dw.gate()
    dw.judge = JudgeRuntime(cfg_getter=lambda: {}, client_factory=dw.judge.client_factory, recorder=dw.judge.recorder,
                            now=dw.judge.now)
    assert refusal(dw, version="") == "judge_error"


@pytest.mark.parametrize("by", ["", "   "])
def test_creation_needs_a_confirmation(dw, by):
    dw.gate()
    assert refusal(dw, by=by) == "confirmation_missing"


def test_a_caller_judge_version_that_is_not_current_is_refused(dw):
    dw.gate()
    assert refusal(dw, version="not-the-current-version") == "stale_report"


def test_only_a_deploy_scope_key_can_be_stored(dw):
    dw.gate()
    for key in (derive(RawAction(tool="Bash", command="git push origin main", repo="a/b")).key,
                derive(RawAction(tool="deploy", repo=REPO, environment="prod")).key,
                derive(RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO), DEPLOY).key):
        with pytest.raises(ValueError, match="scope"):
            dw.stand(key=key)
    assert dw.grants() == []


def test_the_grant_cap_is_a_refusal_not_a_crash(dw):
    dw.gate()
    dw.store.max_action_grants_per_tenant = 0
    assert refusal(dw) == "grant_cap"


# ------------------------------------------------------------------------------------- use
def test_a_standing_grant_deploy_is_pending_until_preconditions_exist(dw):
    dw.gate()
    g = dw.stand()
    d = dw.deploy()
    assert (d.outcome, d.source) == ("pending", "deploy_preconditions") and d.grant_id is None
    assert dw.audits("assistant.action.authorized") == []
    row = dw.store.get_action_request(d.request_id, tenant_id=T, user_id=U)
    assert row.status == "pending" and row.verb == "deploy.prod"
    assert dw.deploy().request_id == d.request_id                      # a re-ask is the same pending row
    assert g.grant_id                                                    # (the grant itself is untouched)


def test_the_standing_grant_covers_every_sha_but_each_request_binds_one(dw):
    dw.gate()
    dw.stand()
    a, b = dw.deploy(sha=SHA), dw.deploy(sha=SHA2)
    assert a.source == b.source == "deploy_preconditions" and a.request_id != b.request_id
    assert a.key_id != b.key_id


def test_without_a_standing_grant_a_matched_deploy_is_an_ordinary_owner_ask(dw):
    dw.gate()
    d = dw.deploy()
    assert (d.outcome, d.source) == ("pending", "owner") and d.verb == "deploy.prod"


def test_unmatched_forms_stay_unknown_even_with_a_live_standing_grant(dw):
    dw.gate()
    dw.stand()
    for command in (f"{CMD} --ref={SHA}", f"{CMD} --ref main", f"./{CMD} --ref {SHA}", f"{CMD} --ref {SHA} && ls",
                    f"env {CMD} --ref {SHA}", f"{CMD} --ref {SHA}\n"):
        d = decide(dw.store, dw.cfg, dw.worker, RawAction(tool="Bash", command=command, repo=REPO), "s", "t", NOW,
                   judge=dw.judge, deploy_cfg=DEPLOY)
        assert (d.outcome, d.source) == ("pending", "unknown"), command
    d = dw.deploy(repo="acme/unlisted")
    assert (d.outcome, d.source) == ("pending", "unknown")


def test_nothing_matches_when_the_config_is_absent(dw):
    dw.gate()
    dw.stand()
    d = dw.deploy(deploy_cfg=None)
    assert (d.outcome, d.source) == ("pending", "unknown")


def test_the_grant_is_per_worker_scope_repo_tenant_and_user(dw):
    dw.gate()
    dw.gate("other", U)
    dw.gate(T, "someone")
    dw.stand()
    assert dw.deploy(who=worker("w2")).source == "owner"                          # another worker
    assert dw.deploy(cmd="deploy/other/run.sh").source == "owner"                 # another scope, same repo
    assert dw.deploy(cmd=CMD, repo="acme/other-repo").source == "owner"           # same command, another repo/project
    assert dw.deploy(who=worker("w1", tenant="other")).source == "owner"          # another tenant
    assert dw.deploy(who=worker("w1", user="someone")).source == "owner"          # another user
    assert dw.deploy().source == "deploy_preconditions"                            # and the right one still matches


def test_revoking_returns_the_next_request_to_pending_owner(dw):
    dw.gate()
    g = dw.stand()
    assert dw.deploy().source == "deploy_preconditions"
    revoke_action_grant(dw.store, NOW, T, U, g.grant_id, "owner", policy_version=int(dw.cfg["policy_version"]),
                        judge_version=current_judge_version(dw.judge))
    d = dw.deploy(sha=SHA2)
    assert (d.outcome, d.source) == ("pending", "owner")
    assert dw.store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.REVOKED


def test_expiry_policy_and_judge_changes_end_the_standing_grant(dw):
    dw.gate()
    dw.stand()
    dw.clock.now = NOW + timedelta(days=int(dw.cfg["grant_ttl_days"]), seconds=1)
    assert dw.deploy().source == "owner"                                           # expired
    dw.clock.now = NOW
    assert dw.deploy(sha=SHA2).source == "deploy_preconditions"
    dw.cfg["policy_version"] = int(dw.cfg["policy_version"]) + 1                   # a policy change sends it back
    assert dw.deploy(sha="1" * 40).source == "owner"
    dw.cfg["policy_version"] -= 1
    dw.jcfg["model"] = "another-model"                                              # trust earned under another judge
    assert dw.deploy(sha="2" * 40).source == "owner"


def test_the_judge_still_escalates_a_standing_grant_deploy(tmp_path):
    w = DWorld(tmp_path, body=RISKY)
    try:
        w.gate()
        w.stand()
        d = w.deploy()
        assert (d.outcome, d.source) == ("pending", "judge")
    finally:
        w.listener.close()


def test_an_unreachable_judge_leaves_the_owner_to_decide(dw):
    dw.gate()
    dw.stand()
    dw.jcfg["enabled"] = False
    d = dw.deploy()
    assert d.outcome == "pending" and d.source in ("judge_unavailable", "owner")


def test_a_veto_wins_over_a_standing_grant(dw):
    dw.gate()
    dw.stand()
    cfg = {**dw.cfg, "extra_vetoes": [{"code": "no_deploys", "reason": "frozen", "verb": "deploy.prod"}]}
    d = decide(dw.store, cfg, dw.worker, RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO), "s", "t",
               NOW, judge=dw.judge, deploy_cfg=DEPLOY)
    assert (d.outcome, d.source, d.veto_code) == ("denied", "veto", "no_deploys")
    by_text = {**dw.cfg, "extra_vetoes": [{"code": "no_ref", "reason": "frozen", "argv_contains": "--ref"}]}
    d = decide(dw.store, by_text, dw.worker, RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO), "s",
               "t", NOW, judge=dw.judge, deploy_cfg=DEPLOY)
    assert (d.outcome, d.source, d.veto_code) == ("denied", "veto", "no_ref")


def test_in_shadow_at_use_the_owner_decides(tmp_path):
    w = DWorld(tmp_path, shadow_days=1)
    try:
        w.gate()
        w.stand()
        w.cfg["shadow_days"] = 14                                                 # the owner lengthens the period
        d = w.deploy()
        assert (d.outcome, d.source) == ("pending", "shadow")
    finally:
        w.listener.close()


def test_ordinary_grants_never_authorize_a_matched_deploy(dw):
    dw.gate()
    req = derive(RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO), DEPLOY).key
    for key, source in ((req, ""), (dw.key(), "")):
        dw.store.put_action_grant(ActionGrant(
            grant_id="ag_manual" + str(len(dw.grants())), tenant_id=T, user_id=U, worker_id="w1",
            resource_scope=REPO, key_id=key.key_id(), verb="deploy.prod", key_canonical=key.canonical(),
            state=GrantState.ACTIVE, created_at=NOW, activated_at=NOW, expires_at=NOW + timedelta(days=30),
            policy_version=int(dw.cfg["policy_version"]), judge_version=current_judge_version(dw.judge),
            confirmed_by="owner", source=source))
    d = dw.deploy()
    assert (d.outcome, d.source) == ("pending", "owner")


def test_a_standing_grant_authorizes_nothing_but_matched_deploys(dw):
    dw.gate()
    dw.stand()
    for command in ("git push origin main", f"{CMD} --ref {SHA} --dry-run", "frobnicate"):
        d = decide(dw.store, dw.cfg, dw.worker, RawAction(tool="Bash", command=command, repo=REPO), "s", "t", NOW,
                   judge=dw.judge, deploy_cfg=DEPLOY)
        assert d.outcome == "pending"
    d = decide(dw.store, dw.cfg, dw.worker, RawAction(tool="deploy", repo=REPO, environment="prod"), "s", "t", NOW,
               judge=dw.judge, deploy_cfg=DEPLOY)
    assert (d.outcome, d.source) == ("pending", "owner")                           # the deploy TOOL key is another key


def test_a_one_shot_approved_deploy_is_authorized_once_without_preconditions(dw):
    d = dw.deploy()
    assert d.source == "owner"
    record_owner_decision(dw.store, dw.cfg, NOW, T, U, d.request_id, True, "slack:U0OWNER01", provenance="owner_slack")
    again = dw.deploy()
    assert (again.outcome, again.source) == ("authorized", "owner")
    assert dw.deploy().outcome == "pending"                                          # spent
    assert dw.deploy(sha=SHA2).outcome == "pending"                                  # and bound to one sha


def test_old_snapshots_without_a_source_restore(dw):
    dw.gate()
    g = dw.stand()
    snap = dw.store.snapshot()
    for row in snap["action_grants"]:
        row.pop("source", None)
    fresh = AssistantStore()
    fresh.restore(snap)
    (restored,) = fresh.list_action_grants(T, U)
    assert restored.source == "" and restored.grant_id == g.grant_id
    snap2 = dw.store.snapshot()
    other = AssistantStore()
    other.restore(snap2)
    assert other.list_action_grants(T, U)[0].source == "owner_standing"


def test_default_grant_has_no_source():
    assert ActionGrant().source == "" and replace(ActionGrant(), source="x").source == "x"


# ------------------------------------------------------------------------------------- mode is a kill switch at use
def with_mode(mode, scope="control-plane"):
    import json
    cfg = json.loads(json.dumps(DEPLOY))
    cfg["projects"]["herdr-engineering"]["scopes"][scope]["mode"] = mode
    return cfg


def test_a_mode_flipped_to_ask_ignores_the_standing_grant_without_touching_it(dw):
    dw.gate()
    g = dw.stand()
    assert dw.deploy().source == "deploy_preconditions"
    d = dw.deploy(deploy_cfg=with_mode("ask"))
    assert (d.outcome, d.source) == ("pending", "deploy_mode") and d.grant_id is None
    assert dw.store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.ACTIVE
    assert dw.audits("assistant.grant.revoked") == []
    d = dw.deploy(deploy_cfg=with_mode("auto"))                      # flipped back: the normal path again
    assert (d.outcome, d.source) == ("pending", "deploy_preconditions")


def test_a_scope_or_config_removed_never_authorizes(dw):
    dw.gate()
    g = dw.stand()
    for cfg in ({"projects": {}}, None, {}):
        d = dw.deploy(deploy_cfg=cfg)
        assert d.outcome == "pending" and d.source in ("unknown", "deploy_mode")
    moved = with_mode("auto")
    moved["projects"]["herdr-engineering"]["scopes"]["control-plane"]["command"] = "deploy/moved.sh"
    assert dw.deploy(deploy_cfg=moved).source in ("unknown", "deploy_mode")
    assert dw.store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.ACTIVE


def test_without_a_grant_a_mode_ask_scope_is_an_ordinary_owner_ask(dw):
    dw.gate()
    assert dw.deploy(deploy_cfg=with_mode("ask")).source == "owner"
