"""Three-way text merge (plan 2026-10-30, Task 5; spec section B merge rules)."""
from __future__ import annotations

import random
import time

import pytest

from herdr_engineering.control_plane.artifact_merge import TextMerge, merge3_text

MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")


def _lines(*items: str) -> str:
    return "".join(f"{item}\n" for item in items)


def _strip_markers(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.startswith(MARKERS)]


# --- basic cases -----------------------------------------------------------


def test_non_overlapping_top_and_bottom_edits_merge():
    base = _lines("a", "b", "c", "d", "e", "f")
    ours = _lines("A", "b", "c", "d", "e", "f")
    theirs = _lines("a", "b", "c", "d", "e", "F")
    result = merge3_text(base, ours, theirs)
    assert result == TextMerge(text=_lines("A", "b", "c", "d", "e", "F"), conflicted=False, conflicts=0)


def test_identical_edits_on_both_sides_merge_once():
    base = _lines("a", "b", "c")
    both = _lines("a", "B", "new", "c")
    result = merge3_text(base, both, both)
    assert result.text == both
    assert not result.conflicted and result.conflicts == 0


def test_identical_edit_plus_separate_edit_merges():
    base = _lines("a", "b", "c", "d", "e")
    ours = _lines("a", "B", "c", "d", "E")
    theirs = _lines("a", "B", "c", "d", "e")
    result = merge3_text(base, ours, theirs)
    assert result.text == _lines("a", "B", "c", "d", "E")
    assert not result.conflicted


def test_same_point_insertion_with_different_text_conflicts():
    base = _lines("a", "b")
    ours = _lines("a", "x", "b")
    theirs = _lines("a", "y", "b")
    result = merge3_text(base, ours, theirs)
    assert result.conflicted and result.conflicts == 1
    assert result.text == "a\n<<<<<<< head\nx\n=======\ny\n>>>>>>> mine\nb\n"


def test_same_point_identical_insertion_merges_once():
    base = _lines("a", "b")
    ours = _lines("a", "x", "y", "b")
    result = merge3_text(base, ours, ours)
    assert result.text == ours and not result.conflicted


@pytest.mark.parametrize("changed", ["ours", "theirs"])
def test_one_side_unchanged_takes_other_side(changed: str):
    base = _lines("a", "b", "c")
    edit = _lines("z", "a", "c", "d")
    ours, theirs = (edit, base) if changed == "ours" else (base, edit)
    result = merge3_text(base, ours, theirs)
    assert result == TextMerge(text=edit, conflicted=False, conflicts=0)


def test_both_unchanged_returns_base():
    base = _lines("a", "b")
    assert merge3_text(base, base, base) == TextMerge(text=base, conflicted=False, conflicts=0)


def test_all_empty():
    assert merge3_text("", "", "") == TextMerge(text="", conflicted=False, conflicts=0)


def test_empty_base_different_additions_conflict():
    result = merge3_text("", _lines("x"), _lines("y"))
    assert result.conflicted and result.conflicts == 1
    assert result.text == "<<<<<<< head\nx\n=======\ny\n>>>>>>> mine\n"


def test_empty_base_identical_additions_merge():
    result = merge3_text("", _lines("x", "y"), _lines("x", "y"))
    assert result == TextMerge(text=_lines("x", "y"), conflicted=False, conflicts=0)


def test_empty_base_one_side_adds():
    assert merge3_text("", "", _lines("x")).text == _lines("x")
    assert merge3_text("", _lines("x"), "").text == _lines("x")


def test_delete_vs_edit_same_line_conflicts():
    base = _lines("a", "b", "c")
    ours = _lines("a", "c")
    theirs = _lines("a", "B", "c")
    result = merge3_text(base, ours, theirs)
    assert result.conflicted and result.conflicts == 1
    assert result.text == "a\n<<<<<<< head\n=======\nB\n>>>>>>> mine\nc\n"


def test_both_delete_same_line_merges():
    base = _lines("a", "b", "c")
    ours = _lines("a", "c")
    assert merge3_text(base, ours, ours).text == ours


def test_whole_file_deleted_vs_unchanged_takes_delete():
    base = _lines("a", "b")
    assert merge3_text(base, "", base) == TextMerge(text="", conflicted=False, conflicts=0)


def test_adjacent_but_not_overlapping_hunks_merge():
    base = _lines("a", "b", "c", "d")
    ours = _lines("a", "B", "c", "d")
    theirs = _lines("a", "b", "C", "d")
    result = merge3_text(base, ours, theirs)
    assert result == TextMerge(text=_lines("a", "B", "C", "d"), conflicted=False, conflicts=0)


def test_adjacent_insertion_and_edit_merge():
    base = _lines("a", "b", "c")
    ours = _lines("a", "new", "b", "c")  # insertion before b
    theirs = _lines("a", "B", "c")  # edit of b
    result = merge3_text(base, ours, theirs)
    assert result == TextMerge(text=_lines("a", "new", "B", "c"), conflicted=False, conflicts=0)


def test_overlapping_different_edits_conflict_and_rest_merges():
    base = _lines("1", "2", "3", "4", "5", "6", "7")
    ours = _lines("ONE", "2", "3x", "4x", "5", "6", "7")
    theirs = _lines("1", "2", "3y", "4", "5", "6", "SEVEN")
    result = merge3_text(base, ours, theirs)
    assert result.conflicted and result.conflicts == 1
    assert result.text == (
        "ONE\n2\n<<<<<<< head\n3x\n4x\n=======\n3y\n4\n>>>>>>> mine\n5\n6\nSEVEN\n"
    )


def test_two_separate_conflicts_counted():
    base = _lines("a", "b", "c", "d", "e")
    ours = _lines("A1", "b", "c", "d", "E1")
    theirs = _lines("A2", "b", "c", "d", "E2")
    result = merge3_text(base, ours, theirs)
    assert result.conflicts == 2 and result.conflicted


# --- line endings ----------------------------------------------------------


def test_crlf_files_keep_crlf():
    base = "a\r\nb\r\nc\r\nd\r\n"
    ours = "A\r\nb\r\nc\r\nd\r\n"
    theirs = "a\r\nb\r\nc\r\nD\r\n"
    result = merge3_text(base, ours, theirs)
    assert result.text == "A\r\nb\r\nc\r\nD\r\n"
    assert not result.conflicted


def test_crlf_conflict_markers_use_crlf():
    base = "a\r\nb\r\n"
    result = merge3_text(base, "a\r\nx\r\nb\r\n", "a\r\ny\r\nb\r\n")
    assert result.text == "a\r\n<<<<<<< head\r\nx\r\n=======\r\ny\r\n>>>>>>> mine\r\nb\r\n"


def test_missing_trailing_newline_on_one_side_preserved():
    base = _lines("a", "b", "c", "d")
    ours = "A\nb\nc\nd"  # edits top and drops the final newline
    theirs = _lines("a", "b", "C", "d")
    result = merge3_text(base, ours, theirs)
    assert result.text == "A\nb\nC\nd"
    assert not result.conflicted


def test_missing_trailing_newline_in_base_kept_when_untouched():
    base = "a\nb\nc"
    ours = "A\nb\nc"
    theirs = "a\nB\nc"
    assert merge3_text(base, ours, theirs).text == "A\nB\nc"


def test_conflict_side_without_trailing_newline_still_has_markers_on_own_lines():
    base = "a\nb"
    result = merge3_text(base, "a\nx", "a\ny")
    assert result.conflicted
    assert result.text == "a\n<<<<<<< head\nx\n=======\ny\n>>>>>>> mine\n"


# --- conflict output shape -------------------------------------------------


def test_conflict_output_contains_exactly_markers_and_both_sides():
    base = _lines("keep", "old1", "old2", "tail")
    ours = _lines("keep", "ours1", "ours2", "tail")
    theirs = _lines("keep", "theirs1", "tail")
    result = merge3_text(base, ours, theirs, ours_label="HEAD-v7", theirs_label="agent-x")
    lines = result.text.splitlines()
    assert lines == [
        "keep",
        "<<<<<<< HEAD-v7",
        "ours1",
        "ours2",
        "=======",
        "theirs1",
        ">>>>>>> agent-x",
        "tail",
    ]
    assert sum(ln.startswith("<<<<<<< ") for ln in lines) == 1
    assert sum(ln == "=======" for ln in lines) == 1
    assert sum(ln.startswith(">>>>>>> ") for ln in lines) == 1


def test_very_long_lines_do_not_crash():
    long_a = "x" * 200_000
    base = _lines("a", long_a, "c")
    ours = _lines("A", long_a, "c")
    theirs = _lines("a", long_a, "C")
    assert merge3_text(base, ours, theirs).text == _lines("A", long_a, "C")


def test_unicode_separators_are_not_line_breaks():
    # str.splitlines would split on U+2028 / \x0c; merge must split on \n only.
    base = "a b\nc\x0cd\n"
    ours = "a b\nc\x0cd\nend\n"
    assert merge3_text(base, ours, base).text == ours


# --- performance -----------------------------------------------------------


def _big_file(rng: random.Random, n: int) -> list[str]:
    lines = []
    for i in range(n):
        if i % 7 == 0:
            lines.append("\n")  # many repeated blank lines
        elif i % 11 == 0:
            lines.append("    }\n")  # repeated boilerplate
        else:
            lines.append(f"line {i} {rng.random():.6f}\n")
    return lines


def test_five_thousand_lines_fifty_scattered_edits_each_side_is_fast():
    rng = random.Random(5000)
    base = _big_file(rng, 5000)
    positions = rng.sample(range(0, 5000, 2), 100)  # even indices, never adjacent
    ours_pos, theirs_pos = set(positions[:50]), set(positions[50:])
    ours = [f"ours edit {i}\n" if i in ours_pos else ln for i, ln in enumerate(base)]
    theirs = [f"theirs edit {i}\n" if i in theirs_pos else ln for i, ln in enumerate(base)]
    expected = [
        ours[i] if i in ours_pos else theirs[i] if i in theirs_pos else base[i] for i in range(5000)
    ]
    start = time.perf_counter()
    result = merge3_text("".join(base), "".join(ours), "".join(theirs))
    elapsed = time.perf_counter() - start
    assert not result.conflicted
    assert result.text == "".join(expected)
    assert elapsed < 2.0, elapsed


# --- property / fuzz checks (seeded) ---------------------------------------


def _random_text(rng: random.Random, alphabet: str, max_lines: int) -> str:
    n = rng.randint(0, max_lines)
    text = "".join(f"{rng.choice(alphabet)}\n" for _ in range(n))
    if text and rng.random() < 0.2:
        text = text[:-1]  # sometimes no trailing newline
    return text


def _mutate(rng: random.Random, text: str, alphabet: str) -> str:
    lines = text.splitlines(keepends=True)
    for _ in range(rng.randint(0, 4)):
        op = rng.choice(("ins", "del", "rep"))
        pos = rng.randint(0, len(lines))
        if op == "ins":
            lines.insert(pos, f"{rng.choice(alphabet)}\n")
        elif lines and pos < len(lines):
            if op == "del":
                del lines[pos]
            else:
                lines[pos] = f"{rng.choice(alphabet)}\n"
    return "".join(lines)


def test_property_identities_seeded():
    rng = random.Random(20261030)
    for _ in range(600):
        alphabet = rng.choice(("ab", "abc", "abcdefgh"))
        base = _random_text(rng, alphabet, 12)
        x = _mutate(rng, base, alphabet + "XYZ") if rng.random() < 0.7 else _random_text(rng, alphabet, 12)
        for args in ((base, base, x), (base, x, base), (base, x, x)):
            result = merge3_text(*args)
            assert result == TextMerge(text=x, conflicted=False, conflicts=0), args


def test_fuzz_random_triples_sound_invariants():
    rng = random.Random(424242)
    for _ in range(1500):
        alphabet = rng.choice(("ab", "abc", "abcdef"))
        base = _random_text(rng, alphabet, 10)
        ours = _mutate(rng, base, alphabet + "OP")
        theirs = _mutate(rng, base, alphabet + "TU")
        result = merge3_text(base, ours, theirs)
        assert result.conflicted == (result.conflicts > 0)
        # deterministic and symmetric in conflict count
        assert merge3_text(base, ours, theirs) == result
        swapped = merge3_text(base, theirs, ours)
        assert swapped.conflicts == result.conflicts
        if not result.conflicted:
            # a clean merge does not depend on which side is "head"
            assert swapped.text == result.text, (base, ours, theirs)
        content = _strip_markers(result.text)
        ours_l, theirs_l = ours.splitlines(), theirs.splitlines()
        # no invented lines: every output line exists on one of the sides,
        # and never more often than both sides together
        for line in set(content):
            assert line in ours_l or line in theirs_l, (base, ours, theirs, result)
            assert content.count(line) <= ours_l.count(line) + theirs_l.count(line)
        if not result.conflicted:
            # a clean merge never drops content that only one side added:
            # lines introduced by exactly one side (absent from base and the
            # other side) must all appear.
            base_l = base.splitlines()
            for side, other in ((ours_l, theirs_l), (theirs_l, ours_l)):
                for line in set(side):
                    if line not in base_l and line not in other:
                        assert content.count(line) == side.count(line), (base, ours, theirs, result)
        else:
            assert result.text.count("<<<<<<< head") == result.conflicts
            assert result.text.count(">>>>>>> mine") == result.conflicts


def test_fuzz_disjoint_regions_merge_to_constructed_result():
    """Constructive oracle: edits confined to separate regions split by a stable,
    unique guard line always merge cleanly to the hand-assembled result."""
    rng = random.Random(7)
    for trial in range(400):
        left = [f"L{trial}-{i}\n" for i in range(rng.randint(0, 6))]
        right = [f"R{trial}-{i}\n" for i in range(rng.randint(0, 6))]
        guard = [f"GUARD-{trial}\n"]
        new_left = _mutate(rng, "".join(left), "opq").splitlines(keepends=True)
        new_right = _mutate(rng, "".join(right), "tuv").splitlines(keepends=True)
        base = "".join(left + guard + right)
        ours = "".join(new_left + guard + right)
        theirs = "".join(left + guard + new_right)
        result = merge3_text(base, ours, theirs)
        assert result == TextMerge(
            text="".join(new_left + guard + new_right), conflicted=False, conflicts=0
        ), (base, ours, theirs)


def test_dropped_final_newline_vs_append_conflicts_instead_of_gluing_lines():
    base = _lines("a", "b")
    ours = "a\nB"  # edits the last line and drops its newline
    theirs = _lines("a", "b", "c")  # appends a line
    result = merge3_text(base, ours, theirs)
    assert "Bc" not in result.text
    assert result.conflicted and result.conflicts == 1
    assert result.text == "a\n<<<<<<< head\nB\n=======\nb\nc\n>>>>>>> mine\n"


def test_large_markdown_with_repeated_blank_lines_is_fast_and_clean():
    # plain SequenceMatcher took ~36 s on this shape; patience anchoring fixes it
    rng = random.Random(40000)
    n = 40000
    base = ["\n" if i % 5 == 0 else f"text {i} {rng.random()}\n" for i in range(n)]
    ours, theirs = list(base), list(base)
    for i in rng.sample(range(1, n, 5), 200):
        ours[i] = f"ours {i}\n"
    for i in rng.sample(range(3, n, 5), 200):
        theirs[i] = f"theirs {i}\n"
    expected = [o if o != b else t for b, o, t in zip(base, ours, theirs, strict=True)]
    start = time.perf_counter()
    result = merge3_text("".join(base), "".join(ours), "".join(theirs))
    assert time.perf_counter() - start < 2.0
    assert result == TextMerge(text="".join(expected), conflicted=False, conflicts=0)


@pytest.mark.parametrize(
    "base_lines",
    [["\n"] * 500_000, ["a\n", "b\n"] * 250_000, [f"v{i % 3}\n" for i in range(200_000)]],
    ids=["all-blank", "alternating", "three-values"],
)
def test_pathological_repetition_is_bounded_and_loses_nothing(base_lines: list[str]):
    rng = random.Random(len(base_lines))
    ours, theirs = list(base_lines), list(base_lines)
    for i in rng.sample(range(len(base_lines)), 50):
        ours[i] = "OURS\n"
    for i in rng.sample(range(len(base_lines)), 50):
        theirs[i] = "THEIRS\n"
    start = time.perf_counter()
    result = merge3_text("".join(base_lines), "".join(ours), "".join(theirs))
    assert time.perf_counter() - start < 5.0
    # coarse diff is allowed to conflict, but every edit survives
    assert result.text.count("OURS\n") == 50 and result.text.count("THEIRS\n") == 50


def test_identities_and_invariants_hold_on_coarse_fallback(monkeypatch):
    from herdr_engineering.control_plane import artifact_merge

    monkeypatch.setattr(artifact_merge, "_MAX_GAP_COST", 0)  # every difflib gap -> one hunk
    rng = random.Random(99)
    for _ in range(400):
        base = _random_text(rng, "abc", 12)
        x = _mutate(rng, base, "abcXY")
        y = _mutate(rng, base, "abcZW")
        for args in ((base, base, x), (base, x, base), (base, x, x)):
            assert merge3_text(*args) == TextMerge(text=x, conflicted=False, conflicts=0)
        result = merge3_text(base, x, y)
        content = _strip_markers(result.text)
        for line in set(content):
            assert line in x.splitlines() or line in y.splitlines()


@pytest.mark.parametrize("labels", [{"ours_label": "he\nad"}, {"theirs_label": "mi\rne"},
                                    {"ours_label": "x\r\n"}])
def test_labels_with_line_breaks_are_refused(labels):
    with pytest.raises(ValueError, match="line breaks"):
        merge3_text("a\n", "b\n", "c\n", **labels)
