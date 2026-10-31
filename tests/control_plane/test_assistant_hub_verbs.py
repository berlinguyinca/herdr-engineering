import dataclasses
import json

import pytest

from herdr_engineering.control_plane.assistant_hub.verbs import (
    STRICTER,
    VERB_RISK,
    ActionKey,
    FlagClass,
    RiskClass,
    Verb,
)


def key(**kw):
    args = {"tool": "gh", "verb": Verb.PR_COMMENT, "resource_scope": "org/repo", "target": "42",
            "flag_class": FlagClass.NONE}
    args.update(kw)
    args.setdefault("risk_class", VERB_RISK[args["verb"]])
    return ActionKey(**args)


def test_canonical_is_stable_across_field_order_and_unicode_variants():
    a = key(tool="ＧＨ", resource_scope="Org/Repo", target="  42 ")
    b = ActionKey(target="42", risk_class=RiskClass.REMOTE_REVERSIBLE, flag_class=FlagClass.NONE,
                  resource_scope="org/repo", verb=Verb.PR_COMMENT, tool="gh")
    assert a.canonical() == b.canonical()
    assert a.key_id() == b.key_id()
    assert key(tool="ａ").canonical() == key(tool="a").canonical()
    assert key(target="caf\u00e9").key_id() == key(target="cafe\u0301").key_id()
    assert key(resource_scope="caf\u00e9").key_id() == key(resource_scope="cafe\u0301").key_id()


def test_compatibility_variants_of_target_and_scope_do_not_merge():
    assert key(target="４２").key_id() != key(target="42").key_id()
    assert key(target="\ufb01le").key_id() != key(target="file").key_id()
    assert key(resource_scope="ｏｒｇ/repo").key_id() != key(resource_scope="org/repo").key_id()


def test_target_is_case_sensitive_and_scope_is_case_insensitive():
    assert key(target="README.md").key_id() != key(target="readme.md").key_id()
    assert key(resource_scope="Org/Repo").key_id() == key(resource_scope="org/repo").key_id()


def _roundtrip(k):
    d = json.loads(k.canonical())
    return ActionKey(tool=d["tool"], verb=Verb(d["verb"]), resource_scope=d["resource_scope"], target=d["target"],
                     flag_class=FlagClass(d["flag_class"]), risk_class=RiskClass(d["risk_class"]))


@pytest.mark.parametrize("scope", ["org/repo", "\u0130stanbul", "Stra\u00dfe", "cafe\u0301"])
def test_canonical_is_idempotent(scope):
    k = key(tool="\u0130\uff27", resource_scope=scope, target=" Foo\u00df ")
    assert _roundtrip(k).canonical() == k.canonical()


def test_canonical_exact_string():
    assert key().canonical() == (
        '{"flag_class":"none","resource_scope":"org/repo","risk_class":"remote_reversible",'
        '"target":"42","tool":"gh","verb":"pr.comment"}')


def test_risk_class_must_match_verb():
    with pytest.raises(ValueError):
        key(verb=Verb.EMAIL_SEND, risk_class=RiskClass.LOCAL_REVERSIBLE)
    for v in Verb:
        assert ActionKey("gh", v, "s", "t", FlagClass.NONE, VERB_RISK[v]).risk_class is VERB_RISK[v]


def test_key_id_differs_for_every_field():
    base = key()
    variants = [key(tool="git"), key(verb=Verb.PR_MERGE), key(resource_scope="org/other"), key(target="43"),
                key(flag_class=FlagClass.FORCE), key(verb=Verb.EMAIL_SEND)]
    ids = {base.key_id(), *(v.key_id() for v in variants)}
    assert len(ids) == 7
    assert key(verb=Verb.PR_MERGE).risk_class is RiskClass.OUTWARD


def test_every_verb_has_a_risk_class():
    assert set(VERB_RISK) == set(Verb)


def test_unknown_verb_is_outward():
    assert VERB_RISK[Verb.UNKNOWN] is RiskClass.OUTWARD
    assert Verb.UNKNOWN == "unknown"
    assert Verb.EMAIL_DRAFT == "email.draft"


def test_verb_risk_classification():
    local = {Verb.EMAIL_DRAFT, Verb.ARTIFACT_CREATE, Verb.SHELL_READ_ONLY, Verb.CLUSTER_READ_ONLY}
    remote = {Verb.PR_COMMENT, Verb.GIT_PUSH}
    for v in Verb:
        want = (RiskClass.LOCAL_REVERSIBLE if v in local
                else RiskClass.REMOTE_REVERSIBLE if v in remote else RiskClass.OUTWARD)
        assert VERB_RISK[v] is want


def test_stricter_prefers_higher_risk_then_flag():
    low = key(verb=Verb.EMAIL_DRAFT, flag_class=FlagClass.HARD)
    high = key(verb=Verb.EMAIL_SEND)
    mid = key(verb=Verb.GIT_PUSH)
    assert STRICTER(low, mid) is mid and STRICTER(mid, high) is high
    assert STRICTER(low, high) is high
    assert STRICTER(high, low) is high
    order = [FlagClass.NONE, FlagClass.MULTIPLE, FlagClass.NO_VERIFY, FlagClass.RECURSIVE, FlagClass.FORCE,
             FlagClass.HARD]
    for i, weaker in enumerate(order[:-1]):
        a, b = key(flag_class=weaker), key(flag_class=order[i + 1])
        assert STRICTER(a, b) is b
        assert STRICTER(b, a) is b
    t1, t2 = key(target="1"), key(target="2")
    assert STRICTER(t1, t2) is t1


def test_action_key_is_immutable():
    k = key()
    with pytest.raises(dataclasses.FrozenInstanceError):
        k.target = "x"  # type: ignore[misc]


# ---- ActionKey.from_canonical -------------------------------------------------------------------------------------

import json as _json  # noqa: E402

import pytest as _pytest  # noqa: E402


def test_from_canonical_round_trips_the_canonical_form_and_key_id():
    from herdr_engineering.control_plane.assistant_hub.verbs import (
        VERB_RISK,
        ActionKey,
        FlagClass,
        Verb,
    )
    key = ActionKey("Git", Verb.GIT_PUSH, "Acme/Repo", " origin main ", FlagClass.NONE, VERB_RISK[Verb.GIT_PUSH])
    back = ActionKey.from_canonical(key.canonical())
    assert back.canonical() == key.canonical()
    assert back.key_id() == key.key_id()


@_pytest.mark.parametrize("text", [
    "not json", "[]", "{}",
    _json.dumps({"tool": "git", "verb": "git.nope", "resource_scope": "", "target": "", "flag_class": "none",
                 "risk_class": "outward"}),
    _json.dumps({"tool": "git", "verb": "git.push", "resource_scope": "", "target": "", "flag_class": "bogus",
                 "risk_class": "remote_reversible"}),
    _json.dumps({"tool": "git", "verb": "git.push", "resource_scope": "", "target": "", "flag_class": "none",
                 "risk_class": "local_reversible"}),
    _json.dumps({"tool": "git", "verb": "git.push", "resource_scope": 3, "target": "", "flag_class": "none",
                 "risk_class": "remote_reversible"}),
    _json.dumps({"tool": "git", "verb": "git.push", "resource_scope": "", "target": "", "flag_class": "none",
                 "risk_class": "remote_reversible", "extra": 1}),
])
def test_from_canonical_rejects_malformed_input(text):
    from herdr_engineering.control_plane.assistant_hub.verbs import ActionKey
    with _pytest.raises(ValueError):
        ActionKey.from_canonical(text)


def test_from_canonical_rejects_non_normalized_input():
    from herdr_engineering.control_plane.assistant_hub.verbs import (
        VERB_RISK,
        ActionKey,
        FlagClass,
        Verb,
    )
    key = ActionKey("git", Verb.GIT_PUSH, "acme/repo", "x", FlagClass.NONE, VERB_RISK[Verb.GIT_PUSH])
    spaced = _json.dumps(_json.loads(key.canonical()), indent=1)
    upper = key.canonical().replace("acme/repo", "ACME/Repo")
    for text in (spaced, upper):
        with _pytest.raises(ValueError):
            ActionKey.from_canonical(text)
