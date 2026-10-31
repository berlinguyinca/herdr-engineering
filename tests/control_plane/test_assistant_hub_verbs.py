import dataclasses

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
            "flag_class": FlagClass.NONE, "risk_class": RiskClass.REMOTE_REVERSIBLE}
    args.update(kw)
    return ActionKey(**args)


def test_canonical_is_stable_across_field_order_and_unicode_variants():
    a = key(tool="ＧＨ", resource_scope="Org/Repo", target="  ４２ ")
    b = ActionKey(target="42", risk_class=RiskClass.REMOTE_REVERSIBLE, flag_class=FlagClass.NONE,
                  resource_scope="org/repo", verb=Verb.PR_COMMENT, tool="gh")
    assert a.canonical() == b.canonical()
    assert a.key_id() == b.key_id()
    assert " " not in a.canonical()
    assert key(tool="ａ").canonical() == key(tool="a").canonical()


def test_key_id_differs_for_every_field():
    base = key()
    variants = [key(tool="git"), key(verb=Verb.PR_MERGE), key(resource_scope="org/other"), key(target="43"),
                key(flag_class=FlagClass.FORCE), key(risk_class=RiskClass.OUTWARD)]
    ids = {base.key_id(), *(v.key_id() for v in variants)}
    assert len(ids) == 7


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
    low = key(risk_class=RiskClass.LOCAL_REVERSIBLE, flag_class=FlagClass.HARD)
    high = key(risk_class=RiskClass.OUTWARD)
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
