"""The rules-based model picker. Every model id here is a made-up fixture."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab_models import pick


def m(id, price=None, family="fam", vision=False, context=None, local=False):
    return {"id": id, "family": family, "vision": vision, "price_out_per_mtok": price,
            "context": context, "local": local}


CAT = [m("m-cheap", 1.0), m("m-mid", 5.0), m("m-mid2", 6.0), m("m-top", 30.0), m("m-eye", 10.0, vision=True)]
SHORT = "rename a variable in one file"


def test_trivial_picks_the_cheapest():
    p = pick("worker", SHORT, None, None, CAT, None, None)
    assert (p.model, p.tier) == ("m-cheap", "trivial")


def test_hard_word_makes_debug_the_strongest():
    p = pick("debugger", "debug this deadlock", None, None, CAT, None, None)
    assert (p.model, p.tier) == ("m-top", "hard")


def test_long_review_is_hard_and_short_one_is_standard():
    assert pick("reviewer", "x" * 3001, None, None, CAT, None, None).tier == "hard"
    p = pick("reviewer", SHORT, None, None, CAT, None, None)
    assert p.tier == "standard" and p.model == "m-mid"


def test_worker_with_hard_word_or_long_text_is_not_trivial():
    assert pick("worker", "fix the race", None, None, CAT, None, None).tier == "standard"
    assert pick("worker", "x" * 400, None, None, CAT, None, None).tier == "standard"
    assert pick("successor", SHORT, None, None, CAT, None, None).tier == "standard"


def test_reviewer_and_debugger_never_get_the_author_model():
    for role in ("reviewer", "debugger"):
        p = pick(role, "security audit", None, None, CAT, None, "m-top")
        assert p.model != "m-top" and p.model is not None
    assert pick("worker", "security audit", None, None, CAT, None, "m-mid").model == "m-mid"


def test_only_the_author_model_available_is_blocked():
    p = pick("reviewer", SHORT, None, None, [m("m-a", 1.0)], None, "m-a")
    assert p.model is None and p.reason.startswith("blocked:")


@pytest.mark.parametrize("task", ["look at this Screenshot", "the DIAGRAM is wrong", "a photo", "picture", "image"])
def test_vision_words_need_a_vision_model(task):
    p = pick("worker", task, None, None, CAT, None, None)
    assert (p.model, p.tier) == ("m-eye", "vision")


def test_vision_role_without_a_vision_model_is_blocked():
    p = pick("vision", SHORT, None, None, [m("m-a", 1.0)], None, None)
    assert p.model is None and p.tier == "vision" and p.reason.startswith("blocked:")


def test_model_hint_outside_allowed_is_ignored():
    p = pick("worker", SHORT, None, "m-top", CAT, ["m-cheap", "m-mid"], None)
    assert p.model == "m-cheap" and "model hint ignored" in p.reason


def test_model_hint_honoured_when_allowed_and_capable():
    assert pick("worker", SHORT, None, "m-mid", CAT, None, None).model == "m-mid"
    p = pick("worker", "look at the image", None, "m-cheap", CAT, None, None)    # not vision capable
    assert p.model == "m-eye" and "model hint ignored" in p.reason
    p = pick("reviewer", SHORT, None, "m-top", CAT, None, "m-top")             # the author
    assert p.model != "m-top" and "model hint ignored" in p.reason


def test_family_hint_honoured_only_if_present():
    cat = [m("a1", 1.0, family="aa"), m("b1", 0.5, family="bb")]
    assert pick("worker", SHORT, "aa", None, cat, None, None).model == "a1"
    p = pick("worker", SHORT, "zz", None, cat, None, None)
    assert p.model == "b1" and "family hint ignored" in p.reason
    assert pick("worker", SHORT, "aa", None, cat, ["b1"], None).model == "b1"        # allowed has no aa


def test_empty_or_bad_catalogue_is_blocked_never_a_default():
    for cat in ([], None, "junk", [{"nope": 1}, 3]):
        p = pick("worker", SHORT, None, None, cat, None, None)
        assert p.model is None and p.reason.startswith("blocked:") and "no catalogue" in p.reason


def test_allowed_that_matches_nothing_is_blocked():
    p = pick("worker", SHORT, None, None, CAT, ["not-there"], None)
    assert p.model is None and p.reason.startswith("blocked:")
    assert pick("worker", SHORT, None, None, CAT, [], None).model is None


def test_ties_break_by_id_and_order_does_not_matter():
    a = [m("m-b", 3.0), m("m-a", 3.0), m("m-c", 3.0)]
    for cat in (a, list(reversed(a))):
        assert pick("worker", SHORT, None, None, cat, None, None).model == "m-a"
        assert pick("debugger", "deadlock", None, None, cat, None, None).model == "m-a"


def test_unknown_price_is_the_median_and_local_ranks_by_context():
    cat = [m("m-lo", 1.0), m("m-unk", None), m("m-hi", 9.0)]
    assert pick("debugger", "deadlock", None, None, cat, None, None).model == "m-hi"
    assert pick("worker", SHORT, None, None, cat, None, None).model == "m-lo"
    loc = [m("l-small", None, local=True, context=8000), m("l-big", None, local=True, context=128000)]
    assert pick("debugger", "deadlock", None, None, loc, None, None).model == "l-big"


def test_reason_is_short_names_tier_and_has_no_task_text():
    secret = "zebra_quokka_secret deadlock"
    p = pick("debugger", secret, None, None, CAT, None, None)
    assert "hard" in p.reason and "zebra" not in p.reason and len(p.reason) < 200


@pytest.mark.parametrize("task", ["read this traceback", "be gracefully tolerant", "embrace the trace", "a grace period"])
def test_hard_words_are_whole_words(task):
    assert pick("debugger", task, None, None, CAT, None, None).tier == "standard"


@pytest.mark.parametrize("task", ["fix the Race", "a deadlock", "cryptography review", "memory  leak"])
def test_whole_hard_words_still_count(task):
    assert pick("debugger", task, None, None, CAT, None, None).tier == "hard"


@pytest.mark.parametrize("task", ["build the docker image", "write a disk image", "pull the base image", "image build"])
def test_container_images_are_not_vision(task):
    assert pick("worker", task, None, None, CAT, None, None).tier != "vision"


@pytest.mark.parametrize("task", ["describe this screenshot", "see logo.PNG", "this image is blurry", "a jpeg"])
def test_picture_words_are_vision(task):
    assert pick("worker", task, None, None, CAT, None, None).tier == "vision"


def test_invalid_numbers_in_a_catalogue_skip_the_entry():
    cat = [m("m-nan", float("nan")), m("m-neg", -1.0), m("m-inf", float("inf")), m("m-ok", 2.0)]
    assert pick("debugger", "deadlock", None, None, cat, None, None).model == "m-ok"


def test_allowed_weaker_model_hint_is_honoured_for_a_hard_tier():
    assert pick("debugger", "deadlock", None, "m-cheap", CAT, None, None).model == "m-cheap"
