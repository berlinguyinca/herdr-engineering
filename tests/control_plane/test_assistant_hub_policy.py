"""Decision engine, owner decisions and activation gates (real AssistantStore, no doubles)."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore, _grants_config
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.policy import (
    ActivationRefused,
    Decision,
    activate_grant,
    decide,
    in_shadow,
    record_owner_decision,
    revoke_action_grant,
)
from herdr_engineering.control_plane.assistant_hub.workers import Worker

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=20)                      # shadow (14 days) is over
T, U, W = "acme", "owner", "w1"
SECRET = "sk-ant-supersecret-0123456789abcdef"


def _cfg(**over):
    return _grants_config({"shadow_started_at": T0.isoformat(), "min_sessions": 2, "min_tasks": 2,
                           "required_approvals": {"local_reversible": 2, "remote_reversible": 2, "outward": 2},
                           **over})


def _worker(worker_id: str = W, tenant: str = T, user: str = U) -> Worker:
    return Worker(worker_id=worker_id, tenant_id=tenant, user_id=user, name="n", harness="claude", device_id="d",
                  secret_digest="x", status="active", created_at=T0, expires_at=T0 + timedelta(days=365))


def _push(cmd: str = "git push origin main", repo: str = "acme/repo-a") -> RawAction:
    return RawAction(tool="Bash", command=cmd, repo=repo)


def _active_grant(store, cfg, raw: RawAction, worker_id: str = W, tenant: str = T, user: str = U,
                  now: datetime = NOW) -> ActionGrant:
    key = derive(raw).key
    assert key is not None
    store.put_action_grant(ActionGrant(
        grant_id=f"ag_{worker_id}_{key.key_id()[:8]}", tenant_id=tenant, user_id=user, worker_id=worker_id,
        resource_scope=key.resource_scope, key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(),
        state=GrantState.PROPOSED, created_at=now, proposed_at=now, expires_at=now + timedelta(days=30),
        policy_version=cfg["policy_version"]))
    return activate_grant(store, cfg, now, tenant, user, f"ag_{worker_id}_{key.key_id()[:8]}",
                          confirmed_by="owner", judge_gate_satisfied=True)


def _decide(store, cfg, raw, worker=None, session="s1", task="t1", now=NOW) -> Decision:
    return decide(store, cfg, worker or _worker(), raw, session, task, now)


# ------------------------------------------------------------------ vetoes

def test_veto_denies_even_with_active_grant():
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push("git push --force origin main"))
    d = _decide(store, cfg, _push("git push --force origin main"))
    assert (d.outcome, d.source, d.veto_code) == ("denied", "veto", "force_push")
    row = store.get_action_request(d.request_id, tenant_id=T, user_id=U)
    assert row.status == "vetoed"
    assert store.list_action_evidence(T, U) == []


def test_force_push_hidden_in_a_chain_is_denied():
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, _push("git status && echo ok | cat; git push --force origin main"))
    assert (d.outcome, d.veto_code) == ("denied", "force_push")


def test_extra_veto_from_config_is_applied():
    store = AssistantStore()
    cfg = _cfg(extra_vetoes=[{"code": "no_pushes", "reason": "pushes are paused", "verb": "git.push"}])
    d = _decide(store, cfg, _push())
    assert (d.outcome, d.veto_code) == ("denied", "no_pushes")


def test_vetoed_audit_names_code_and_key_but_never_the_command():
    store, cfg = AssistantStore(), _cfg()
    raw = _push(f"git push --force origin main # {SECRET}")
    d = _decide(store, cfg, raw)
    entries = [e for e in store._audit if e["action"] == "assistant.action.vetoed"]
    assert len(entries) == 1 and entries[0]["actor"] == "hub:policy"
    assert "force_push" in entries[0]["detail"] and derive(raw).key.key_id() in entries[0]["detail"]
    assert "git push" not in str(store._audit) and SECRET not in str(store._audit)
    assert d.request_id


# ------------------------------------------------------------------ grants

def test_active_grant_authorizes_the_exact_key():
    store, cfg = AssistantStore(), _cfg()
    g = _active_grant(store, cfg, _push())
    d = _decide(store, cfg, _push())
    assert (d.outcome, d.source, d.grant_id, d.request_id) == ("authorized", "grant", g.grant_id, None)
    audit = [e for e in store._audit if e["action"] == "assistant.action.authorized"]
    assert len(audit) == 1 and g.grant_id in audit[0]["subject_id"] + audit[0]["detail"]
    assert derive(_push()).key.key_id() in audit[0]["detail"]


@pytest.mark.parametrize("variant,worker", [
    (_push(repo="acme/repo-b"), W),                                  # other repo
    (_push(), "w2"),                                                 # other worker
    (_push("git push origin Main"), W),                              # case in target
    (_push("git push origin ｍain"), W),                             # unicode look-alike target
    (_push("git push origin main2"), W),
    (_push("git reset --hard", ), W),                                # other verb
    (RawAction(tool="Bash", command="rm x.txt", repo="acme/repo-a"), W),
])
def test_active_grant_authorizes_exact_key_only(variant, worker):
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push())
    d = _decide(store, cfg, variant, worker=_worker(worker))
    assert d.outcome == "pending" and d.source == "owner" and d.grant_id is None


def test_flag_class_change_is_pending():
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, RawAction(tool="Bash", command="rm x.txt", repo="acme/repo-a"))
    d = _decide(store, cfg, RawAction(tool="Bash", command="rm -r x.txt", repo="acme/repo-a"))
    assert d.outcome == "pending"


def test_other_tenant_or_user_grant_never_authorizes():
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push(), tenant="other", user=U)
    _active_grant(store, cfg, _push(), tenant=T, user="someone-else")
    assert _decide(store, cfg, _push()).outcome == "pending"


@pytest.mark.parametrize("raw", [
    RawAction(tool="Bash", command="frobnicate --x"),
    RawAction(tool="Bash", command="echo $(("),
    RawAction(tool="Zzz"),
])
def test_unknown_or_unparseable_is_pending(raw):
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, raw)
    assert (d.outcome, d.source) == ("pending", "unknown")
    assert store.get_action_request(d.request_id, tenant_id=T, user_id=U).status == "pending"


def test_owner_decides_never_authorizes_even_when_a_grant_matches_the_key():
    store, cfg = AssistantStore(), _cfg()
    raw = RawAction(tool="Bash", command="frobnicate --x")
    key = derive(raw).key
    store.put_action_grant(ActionGrant(
        grant_id="ag_x", tenant_id=T, user_id=U, worker_id=W, resource_scope=key.resource_scope, key_id=key.key_id(),
        verb=key.verb.value, key_canonical=key.canonical(), state=GrantState.ACTIVE, created_at=NOW,
        activated_at=NOW, expires_at=NOW + timedelta(days=5), policy_version=1, confirmed_by="owner"))
    assert _decide(store, cfg, raw).outcome == "pending"


def test_shadow_period_blocks_authorization():
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push())
    early = T0 + timedelta(days=3)
    assert in_shadow(store, cfg, early) is True and in_shadow(store, cfg, NOW) is False
    d = _decide(store, cfg, _push(), now=early)
    assert (d.outcome, d.source) == ("pending", "shadow")


def test_expired_and_revoked_grants_do_not_authorize_on_next_call():
    store, cfg = AssistantStore(), _cfg()
    g = _active_grant(store, cfg, _push())
    assert _decide(store, cfg, _push()).outcome == "authorized"
    assert _decide(store, cfg, _push(), now=NOW + timedelta(days=31)).outcome == "pending"
    revoke_action_grant(store, NOW, T, U, g.grant_id, "owner")
    assert _decide(store, cfg, _push()).outcome == "pending"
    kinds = [e["action"] for e in store._audit]
    assert "assistant.grant.revoked" in kinds


def test_policy_version_bump_blocks_old_grant():
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push())
    assert _decide(store, cfg, _push()).outcome == "authorized"
    assert _decide(store, _cfg(policy_version=2), _push()).outcome == "pending"


def test_revoke_unknown_or_cross_tenant_grant_is_keyerror():
    store, cfg = AssistantStore(), _cfg()
    g = _active_grant(store, cfg, _push())
    with pytest.raises(KeyError):
        revoke_action_grant(store, NOW, "other", U, g.grant_id, "owner")
    with pytest.raises(KeyError):
        revoke_action_grant(store, NOW, T, U, "nope", "owner")
    assert _decide(store, cfg, _push()).outcome == "authorized"


# ------------------------------------------------------------------ pending requests

def test_identical_pending_requests_share_one_row():
    store, cfg = AssistantStore(), _cfg()
    a, b = _decide(store, cfg, _push()), _decide(store, cfg, _push())
    assert a.request_id == b.request_id and a.request_id.startswith("ar_")
    assert len(store.list_action_requests(T, U)) == 1
    c = _decide(store, cfg, _push(), session="s2")
    assert c.request_id != a.request_id
    assert len([e for e in store._audit if e["action"] == "assistant.action.requested"]) == 2


SECRET_CMDS = [
    "curl https://bob:pw0rd-userinfo@example.com/x --token=tok-abc-123",
    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 git push origin main",
    'curl -H "Authorization: Bearer bearer-xyz-987" https://example.com',
]
SECRET_PARTS = ("pw0rd-userinfo", "tok-abc-123", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "bearer-xyz-987")


def test_persisted_state_holds_no_command_text_and_display_is_masked():
    store, cfg = AssistantStore(), _cfg()
    for cmd in SECRET_CMDS + ["git push --force origin main  # hunter2-canary", "frobnicate --x $(((("]:
        d = _decide(store, cfg, RawAction(tool="Bash", command=cmd, repo="acme/repo-a"))
        record = store.get_action_request(d.request_id, tenant_id=T, user_id=U)
        for word in ("curl", "Bearer", "frobnicate", "hunter2", "origin"):
            assert word not in record.key_canonical
        assert "argv:" in record.key_canonical or "raw:" in record.key_canonical or record.verb != "unknown"
        for part in SECRET_PARTS:
            assert part not in record.display
        assert len(record.display) <= 300
    rows = store.list_action_requests(T, U)
    assert any("https://***@example.com" in r.display and "--token=***" in r.display for r in rows)
    assert any("GITHUB_TOKEN=***" in r.display for r in rows)
    assert any("Authorization: ***" in r.display for r in rows)
    approvable = next(r for r in rows if r.status == "pending" and r.verb == "git.push")
    record_owner_decision(store, cfg, NOW, T, U, approvable.request_id, True, "owner")
    store.put_action_grant(ActionGrant(grant_id="ag_s", tenant_id=T, user_id=U, worker_id=W, key_id="k",
                                       key_canonical=approvable.key_canonical, state=GrantState.PROPOSED))
    others = repr(store._audit) + repr(store.list_action_evidence(T, U)) + repr(store.list_action_grants(T, U))
    snap = store.snapshot() if hasattr(store, "snapshot") else {}
    others += repr(snap.get("action_grants", [])) + repr(snap.get("action_evidence", []))
    for part in (*SECRET_PARTS, "hunter2-canary", "curl", "display"):
        assert part not in others


def test_display_is_truncated():
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, RawAction(tool="Bash", command="echo " + "a" * 2000))
    assert len(store.get_action_request(d.request_id, tenant_id=T, user_id=U).display) == 300


# ---- F2: a grant covers one plain command, and the key binds the full argv

GRANT_CMD = "git push origin feature"
WIDER = [
    "git push --repo=evil origin feature", "git push -u origin feature", "git push --tags origin feature",
    "git push --no-verify origin feature", "git push origin feature &> /tmp/x", "git push origin feature; ls",
    "git push origin feature && ls", "git push origin feature && git diff", "git push origin feature || true",
    "git push origin feature; cd /tmp", "git push origin feature && echo hunter2-canary", "git push origin feature &",
    "FOO=1 git push origin feature", "(git push origin feature)", "{ git push origin feature; }",
    "git push origin feature\nid", "git push origin other", "git push upstream feature",
    "git push origin feature > /tmp/x", "git push origin feature | cat", "git push origin feature `id`",
]


@pytest.mark.parametrize("cmd", WIDER)
def test_active_grant_never_covers_a_wider_or_different_command(cmd):
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push(GRANT_CMD))
    d = _decide(store, cfg, _push(cmd))
    assert d.outcome == "pending" and d.grant_id is None


@pytest.mark.parametrize("cmd", [GRANT_CMD, "git  push origin 'feature'", "git push   origin feature  "])
def test_active_grant_covers_the_same_argv(cmd):
    store, cfg = AssistantStore(), _cfg()
    g = _active_grant(store, cfg, _push(GRANT_CMD))
    d = _decide(store, cfg, _push(cmd))
    assert (d.outcome, d.grant_id) == ("authorized", g.grant_id)


# ------------------------------------------------------------------ owner decisions

def test_owner_decision_is_idempotent_and_records_owner_api_evidence():
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, _push(), session="s9", task="t9")
    row = record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "owner")
    assert (row.status, row.decided_by, row.decided_at) == ("approved", "owner", NOW)
    ev = store.list_action_evidence(T, U)
    assert len(ev) == 1
    assert (ev[0].provenance, ev[0].outcome, ev[0].worker_id, ev[0].session_id, ev[0].task_id) == (
        "owner_api", "approved", W, "s9", "t9")
    assert ev[0].key_id == derive(_push()).key.key_id()
    with pytest.raises(ValueError, match="already decided"):
        record_owner_decision(store, cfg, NOW, T, U, d.request_id, False, "owner")
    assert len(store.list_action_evidence(T, U)) == 1
    assert any(e["action"] == "assistant.action.decided" and e["actor"] == "owner" for e in store._audit)


def test_owner_denial_records_denied_evidence():
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, _push())
    row = record_owner_decision(store, cfg, NOW, T, U, d.request_id, False, "owner")
    assert row.status == "denied"
    assert store.list_action_evidence(T, U)[0].outcome == "denied"


def test_decision_input_validation_and_tenant_isolation():
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, _push())
    with pytest.raises(KeyError):
        record_owner_decision(store, cfg, NOW, "other", U, d.request_id, True, "owner")
    with pytest.raises(KeyError):
        record_owner_decision(store, cfg, NOW, T, "someone-else", d.request_id, True, "owner")
    with pytest.raises(KeyError):
        record_owner_decision(store, cfg, NOW, T, U, "ar_missing", True, "owner")
    with pytest.raises(ValueError):
        record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "  ")
    assert store.get_action_request(d.request_id, tenant_id=T, user_id=U).status == "pending"
    assert store.list_action_evidence(T, U) == []


def test_vetoed_requests_cannot_be_decided():
    store, cfg = AssistantStore(), _cfg()
    d = _decide(store, cfg, _push("git push --force origin main"))
    with pytest.raises(ValueError):
        record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "owner")
    assert store.list_action_evidence(T, U) == []


def test_unknown_verb_approval_records_evidence_but_proposes_nothing():
    store, cfg = AssistantStore(), _cfg()
    for i in range(4):
        d = _decide(store, cfg, RawAction(tool="Bash", command="frobnicate --x"), session=f"s{i}", task=f"t{i}")
        record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "owner")
    assert store.list_action_grants(T, U) == []


def test_ready_evidence_creates_proposed_not_active():
    store, cfg = AssistantStore(), _cfg()
    for i in range(2):
        d = _decide(store, cfg, _push(), session=f"s{i}", task=f"t{i}")
        record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "owner")
    grants = store.list_action_grants(T, U)
    assert len(grants) == 1 and grants[0].state is GrantState.PROPOSED and grants[0].worker_id == W
    assert _decide(store, cfg, _push(), session="s3", task="t3").outcome == "pending"       # still not trusted


# ------------------------------------------------------------------ activation

def _proposed(store, cfg, now=NOW) -> ActionGrant:
    for i in range(2):
        d = _decide(store, cfg, _push(), session=f"s{i}", task=f"t{i}", now=now)
        record_owner_decision(store, cfg, now, T, U, d.request_id, True, "owner")
    return store.list_action_grants(T, U)[0]


def test_activate_refused_without_judge_gate():
    store, cfg = AssistantStore(), _cfg()
    g = _proposed(store, cfg)
    with pytest.raises(ActivationRefused) as exc:
        activate_grant(store, cfg, NOW, T, U, g.grant_id, "owner", False)
    assert exc.value.code == "judge_gate_not_satisfied" and isinstance(exc.value, ValueError)
    assert store.list_action_grants(T, U)[0].state is GrantState.PROPOSED


def test_activate_requires_first_grant_confirmation():
    store, cfg = AssistantStore(), _cfg()
    g = _proposed(store, cfg)
    for who in ("", "   "):
        with pytest.raises(ActivationRefused) as exc:
            activate_grant(store, cfg, NOW, T, U, g.grant_id, who, True)
        assert exc.value.code == "confirmation_missing"


def test_activate_refusals_for_state_shadow_and_ownership():
    store, cfg = AssistantStore(), _cfg()
    g = _proposed(store, cfg)
    with pytest.raises(ActivationRefused) as exc:
        activate_grant(store, cfg, T0 + timedelta(days=2), T, U, g.grant_id, "owner", True)
    assert exc.value.code == "still_in_shadow"
    for tenant, user, gid in (("other", U, g.grant_id), (T, "x", g.grant_id), (T, U, "nope")):
        with pytest.raises(ActivationRefused) as exc:
            activate_grant(store, cfg, NOW, tenant, user, gid, "owner", True)
        assert exc.value.code == "not_found"
    with pytest.raises(ActivationRefused) as exc:                       # policy bump sends it back to shadow
        activate_grant(store, _cfg(policy_version=2), NOW, T, U, g.grant_id, "owner", True)
    assert exc.value.code == "not_proposed"
    done = activate_grant(store, cfg, NOW, T, U, g.grant_id, "owner", True)
    with pytest.raises(ActivationRefused) as exc:
        activate_grant(store, cfg, NOW, T, U, g.grant_id, "owner", True)
    assert exc.value.code == "not_proposed"
    assert done.state is GrantState.ACTIVE


def test_activation_sets_state_expiry_and_audit_then_grant_is_honored():
    store, cfg = AssistantStore(), _cfg()
    g = _proposed(store, cfg)
    done = activate_grant(store, cfg, NOW, T, U, g.grant_id, "owner", True)
    assert (done.state, done.activated_at, done.confirmed_by) == (GrantState.ACTIVE, NOW, "owner")
    assert done.expires_at == NOW + timedelta(days=cfg["grant_ttl_days"])
    assert store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.ACTIVE
    assert any(e["action"] == "assistant.grant.activated" and e["actor"] == "owner" for e in store._audit)
    assert _decide(store, cfg, _push()).outcome == "authorized"


# ------------------------------------------------------------------ audit

def test_audit_chain_verifies_after_a_full_cycle():
    store, cfg = AssistantStore(), _cfg()
    _decide(store, cfg, _push("git push --force origin main"))
    g = _proposed(store, cfg)
    activate_grant(store, cfg, NOW, T, U, g.grant_id, "owner", True)
    assert _decide(store, cfg, _push()).outcome == "authorized"
    revoke_action_grant(store, NOW, T, U, g.grant_id, "owner")
    assert _decide(store, cfg, _push()).outcome == "pending"
    assert store.verify_audit(T) is True
    actions = {e["action"] for e in store._audit}
    assert {"assistant.action.vetoed", "assistant.action.requested", "assistant.action.decided",
            "assistant.grant.activated", "assistant.action.authorized", "assistant.grant.revoked"} <= actions


def test_decision_is_a_frozen_dataclass():
    d = Decision("pending", "r", None, None, None, "owner")
    with pytest.raises(AttributeError):
        replace(d).outcome = "authorized"            # type: ignore[misc]


@pytest.mark.parametrize("cmd", ["exec git push origin feature", "command git push origin feature",
                                 "nohup git push origin feature", "timeout 5 git push origin feature",
                                 "nice -n 19 git push origin feature", "env git push origin feature",
                                 "stdbuf -o0 git push origin feature", "sh -c 'git push origin feature'",
                                 "git push origin feature # note"])
def test_wrapped_command_is_not_covered_by_the_plain_commands_grant(cmd):
    store, cfg = AssistantStore(), _cfg()
    _active_grant(store, cfg, _push(GRANT_CMD))
    assert _decide(store, cfg, _push(cmd)).outcome == "pending"
    assert derive(_push(cmd)).key.key_id() != derive(_push(GRANT_CMD)).key.key_id()


@pytest.mark.parametrize("raw,masked,leaked", [
    ("curl -u bob:pw1 https://x", "-u ***", "pw1"),
    ("curl --user bob:pw2 https://x", "--user ***", "pw2"),
    ("mysql -h db -pSECRET3 app", "-p***", "SECRET3"),
    ("sshpass -p PW4 ssh host ls", "sshpass -p ***", "PW4"),
    ('curl -H "X-Api-Key: key5" https://x', 'X-Api-Key: ***"', "key5"),
    ('curl -H "Cookie: sid=cook6" https://x', "Cookie: ***", "cook6"),
    ('curl -H "Set-Cookie: sid=cook7" https://x', "Set-Cookie: ***", "cook7"),
    ('curl -H "Proxy-Authorization: Basic abc8" https://x', "Proxy-Authorization: ***", "abc8"),
    ("DB_PWD=pw9 AWS_SECRET=s10 MY_KEY=k11 X_PASS=p12 run", "DB_PWD=***", "pw9"),
    ("docker login -p pw13 reg", "-p ***", "pw13"),
    ("podman login --password pw14 reg", "--password ***", "pw14"),
    ('curl -H "Authorization: Bearer tok15" https://x', 'Authorization: ***"', "tok15"),
])
def test_display_masks_each_secret_shape(raw, masked, leaked):
    from herdr_engineering.control_plane.assistant_hub.policy import scrub_display
    out = scrub_display(raw)
    assert masked in out and leaked not in out
    assert "ls -pfoo" not in raw and scrub_display("ls -pfoo") == "ls -pfoo"    # -p is only a secret for db clients
