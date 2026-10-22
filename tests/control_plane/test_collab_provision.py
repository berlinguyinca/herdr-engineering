"""Auto-provisioning: the people the operator invites are added to the Cognito group."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "collab_provision", Path(__file__).parents[2] / "deploy/control-plane/collab-provision.py")
prov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prov)

NOW = 1_800_000_000.0
OP = "operator-uuid"


class FakeCognito:
    def __init__(self, existing=None, fail=False):
        self.users = dict(existing or {})
        self.groups, self.created, self.fail = [], [], fail

    def find_user(self, email):
        if self.fail:
            raise RuntimeError("aws down")
        return self.users.get(email)

    def create_user(self, email):
        self.created.append(email)
        self.users[email] = "sub-" + email
        return self.users[email]

    def add_to_group(self, username):
        self.groups.append(username)


def invite(iid, email, actor=OP, exp=NOW + 3600):
    return {"event_type": "invite_created", "payload": {
        "invite_id": iid, "email": email, "actor_user_id": actor, "expires_at_epoch": exp}}


def closed(iid):
    return {"event_type": "invite_closed", "payload": {"invite_id": iid, "reason": "accepted"}}


@pytest.fixture
def env(tmp_path):
    log, ledger = tmp_path / "collab.jsonl", tmp_path / "led" / "ledger.jsonl"

    def go(events, cog=None, **kw):
        log.write_text("".join(json.dumps(e) + "\n" for e in events))
        cog = cog or FakeCognito()
        rows = prov.run(log_path=str(log), ledger_path=str(ledger), operator=OP, cognito=cog,
                        now=kw.pop("now", NOW), say=lambda m: None, **kw)
        return cog, rows
    go.ledger = ledger
    go.log = log
    return go


def test_operators_invitee_is_created_and_added(env):
    cog, rows = env([invite("i1", "new@example.org")])
    assert cog.created == ["new@example.org"] and cog.groups == ["sub-new@example.org"]
    assert rows[0]["result"] == "added" and rows[0]["created"] is True


def test_existing_account_is_added_without_creating_one(env):
    cog, _ = env([invite("i1", "old@example.org")], FakeCognito({"old@example.org": "sub-old"}))
    assert cog.created == [] and cog.groups == ["sub-old"]


def test_only_the_operators_invites_count(env):
    cog, rows = env([invite("i1", "guest-invitee@example.org", actor="some-guest"),
                     invite("i2", "forged@example.org", actor="")])
    assert cog.groups == [] and rows == []


def test_closed_expired_and_cancelled_invites_are_skipped(env):
    cog, _ = env([invite("i1", "a@example.org"), closed("i1"),
                  invite("i2", "b@example.org", exp=NOW - 1)])
    assert cog.groups == []


def test_idempotent_across_runs_and_emails(env):
    env([invite("i1", "a@example.org")])
    cog, rows = env([invite("i1", "a@example.org"), invite("i9", "a@example.org")])
    assert cog.groups == [] and rows == []          # no second invitation email


@pytest.mark.parametrize("bad", ['x"@example.org', "a b@example.org", "a@b", "ａ@example.org",
                                 'x" or email = "@example.org', "a@exam;ple.org", "a" * 70 + "@example.org"])
def test_hostile_or_malformed_emails_never_reach_aws(env, bad):
    cog, rows = env([invite("i1", bad)])
    assert cog.groups == [] and cog.created == [] and rows == []


def test_domain_allowlist_is_honoured_when_set(env):
    cog, _ = env([invite("i1", "a@ucdavis.edu"), invite("i2", "b@other.org")], domains=("ucdavis.edu",))
    assert cog.groups == ["sub-a@ucdavis.edu"]


def test_no_auto_create_waits_for_the_person_to_sign_up(env):
    cog, rows = env([invite("i1", "a@example.org")], auto_create=False)
    assert cog.created == [] and cog.groups == [] and rows == []
    cog, _ = env([invite("i1", "a@example.org")], FakeCognito({"a@example.org": "sub-a"}), auto_create=False)
    assert cog.groups == ["sub-a"]


def test_daily_cap_bounds_a_flood(env):
    events = [invite(f"i{n:02d}", f"u{n}@example.org", exp=NOW + 10 ** 6) for n in range(25)]
    cog, rows = env(events)
    assert len(cog.groups) == prov.MAX_PER_DAY
    cog, _ = env(events, now=NOW + 86400 + 1)         # a day later the next batch is allowed
    assert len(cog.groups) == prov.MAX_PER_DAY


def test_failures_back_off_and_stop_after_a_few_attempts(env):
    ev = [invite("i1", "a@example.org", exp=NOW + 10 ** 7)]
    _, rows = env(ev, FakeCognito(fail=True))
    assert rows[0]["result"] == "failed"
    assert env(ev, FakeCognito(fail=True), now=NOW + 60)[1] == []          # too soon
    t = NOW
    for _ in range(prov.MAX_ATTEMPTS + 2):
        t += prov.RETRY_AFTER + 1
        env(ev, FakeCognito(fail=True), now=t)
    fails = [json.loads(line) for line in env.ledger.read_text().splitlines()]
    assert len(fails) == prov.MAX_ATTEMPTS
    cog, rows = env(ev, now=t + 10 ** 5)
    assert cog.groups == [] and rows == []                                  # gave up; needs a human


def test_a_failed_attempt_recovers_when_aws_comes_back(env):
    ev = [invite("i1", "a@example.org", exp=NOW + 10 ** 7)]
    env(ev, FakeCognito(fail=True))
    cog, rows = env(ev, now=NOW + prov.RETRY_AFTER + 1)
    assert rows[0]["result"] == "added" and cog.groups


def test_dry_run_changes_nothing(env):
    cog, rows = env([invite("i1", "a@example.org")], dry_run=True)
    assert cog.groups == [] and cog.created == [] and rows == [] and not env.ledger.exists()


def test_garbage_and_torn_log_lines_are_ignored(env):
    env([invite("i1", "a@example.org")])
    env.log.write_text("not json\n{\"event_type\":\"invite_created\"}\n" + env.log.read_text() + '{"event_ty')
    out = prov.open_invites(str(env.log), OP, NOW)
    assert [i["email"] for i in out] == ["a@example.org"]


def test_ledger_is_private(env):
    env([invite("i1", "a@example.org")])
    assert oct(env.ledger.stat().st_mode & 0o777) == "0o600"


def test_no_operator_configured_does_nothing(monkeypatch):
    # An operator in the environment is ignored: configuration is flags only (central config
    # -> collab slice -> collab-sync.sh flags).
    monkeypatch.setenv("HERDR_COLLAB_OPERATOR_USER", OP)
    monkeypatch.setattr(prov, "run", lambda **kw: pytest.fail("run without --operator"))
    assert prov.main([]) == 0


def test_flags_carry_the_slice_values_and_env_is_ignored(tmp_path, monkeypatch):
    for k, v in {"HERDR_COLLAB_POOL": "us-east-1_ENV", "HERDR_COLLAB_GROUP": "env.group",
                 "HERDR_COLLAB_DATA_DIR": "/env/data", "HERDR_COLLAB_MAX_PER_DAY": "99",
                 "HERDR_COLLAB_AUTO_CREATE": "0", "HERDR_COLLAB_AUTO_DOMAINS": "env.org"}.items():
        monkeypatch.setenv(k, v)
    seen = {}
    monkeypatch.setattr(prov, "run", lambda **kw: seen.update(kw))
    assert prov.main(["--operator", OP, "--pool", "us-west-2_SLICE", "--group", "team.test",
                      "--data-dir", str(tmp_path / "data"), "--ledger-dir", str(tmp_path / "led")]) == 0
    assert seen["operator"] == OP and seen["log_path"] == str(tmp_path / "data" / "collab.jsonl")
    assert seen["ledger_path"] == str(tmp_path / "led" / "ledger.jsonl")
    assert (seen["cognito"].pool, seen["cognito"].group, seen["cognito"].region) == \
        ("us-west-2_SLICE", "team.test", "us-west-2")
    assert seen["auto_create"] is True and seen["domains"] == () and seen["max_per_day"] == prov.MAX_PER_DAY
    assert seen["dry_run"] is False


def test_policy_flags(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(prov, "run", lambda **kw: seen.update(kw))
    prov.main(["--operator", OP, "--pool", "us-west-2_X", "--no-auto-create", "--domain", "UCDavis.edu",
               "--domain", "x.org", "--max-per-day", "2", "--dry-run"])
    assert seen["auto_create"] is False and seen["domains"] == ("ucdavis.edu", "x.org")
    assert seen["max_per_day"] == 2 and seen["dry_run"] is True


def test_operator_without_pool_is_refused():
    with pytest.raises(SystemExit):
        prov.main(["--operator", OP])


# ---- review findings

class StubAws(prov.AwsCognito):
    def __init__(self, users):
        super().__init__("us-west-2_X", "local.dev")
        self.users, self.calls = users, []

    def _aws(self, *args):
        self.calls.append(args)
        return {"Users": self.users}


def _user(name, verified):
    return {"Username": name, "Attributes": [{"Name": "email_verified", "Value": verified}]}


def test_an_unverified_self_signup_with_the_invitees_email_is_never_trusted():
    with pytest.raises(RuntimeError, match="not verified"):
        StubAws([_user("attacker", "false")]).find_user("victim@example.org")
    with pytest.raises(RuntimeError, match="not verified"):
        StubAws([{"Username": "attacker", "Attributes": []}]).find_user("victim@example.org")
    assert StubAws([_user("real", "true")]).find_user("v@example.org") == "real"
    assert StubAws([]).find_user("v@example.org") is None


def test_ambiguous_matches_are_refused():
    with pytest.raises(RuntimeError, match="more than one"):
        StubAws([_user("a", "true"), _user("b", "true")]).find_user("v@example.org")


def test_values_are_passed_in_option_equals_form():
    c = StubAws([])
    c.find_user("a@example.org")
    assert all(not a.startswith("-") or "=" in a or a in ("--limit",) for a in c.calls[0] if a.startswith("-"))
    assert "--filter=email = \"a@example.org\"" in c.calls[0]


def test_an_unverified_account_leaves_the_invite_unprovisioned_and_bounded(env):
    class Unverified(FakeCognito):
        def find_user(self, email):
            raise RuntimeError("an account with this email exists but its email is not verified")
    cog, rows = env([invite("i1", "v@example.org")], Unverified())
    assert cog.groups == [] and rows[0]["result"] == "failed"


def test_leading_dash_and_non_alnum_start_are_rejected(env):
    cog, rows = env([invite("i1", "-x@example.org"), invite("i2", ".x@example.org")])
    assert cog.groups == [] and rows == []


def test_log_that_is_a_fifo_or_symlink_or_huge_is_ignored(tmp_path, monkeypatch):
    import os
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    assert prov.open_invites(str(fifo), OP, NOW) == []          # returns instead of blocking
    real = tmp_path / "real.jsonl"
    real.write_text(json.dumps(invite("i1", "a@example.org")) + "\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert prov.open_invites(str(link), OP, NOW) == []
    assert len(prov.open_invites(str(real), OP, NOW)) == 1
    monkeypatch.setattr(prov, "MAX_LOG_BYTES", 10)
    assert prov.open_invites(str(real), OP, NOW) == []


def test_one_run_makes_a_bounded_number_of_aws_attempts(env):
    events = [invite(f"i{n:02d}", f"u{n}@example.org", exp=NOW + 10 ** 6) for n in range(30)]
    cog, rows = env(events, FakeCognito(fail=True), max_per_day=100)
    assert len(rows) == prov.MAX_ATTEMPTS_PER_RUN
