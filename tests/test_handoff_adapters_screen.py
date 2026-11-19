"""Reading coloured pane rows: the faint suggestion after a prompt marker is not typed text."""
import pytest

from herdr_engineering.handoff_adapters.screen import (
    composer_is_empty,
    last_marker_row,
    row_text,
    screen_rows,
    strip_ansi,
    typed_text,
)

ESC = "\x1b"


def test_strip_ansi_removes_colour_codes_only():
    assert strip_ansi(f"{ESC}[0m{ESC}[38;2;1;2;3mtext{ESC}[0m") == "text"


def test_screen_rows_drops_trailing_rows_that_are_blank_once_uncoloured():
    rows = screen_rows(f"one\n{ESC}[48;2;1;2;3m   {ESC}[0m\n\n")
    assert [plain for _, plain in rows] == ["one"]


@pytest.mark.parametrize("row, expected", [
    ("❯", ""),
    ("❯\xa0", ""),
    (f"❯\xa0{ESC}[0m{ESC}[2mTry something{ESC}[0m", ""),  # faint suggestion
    (f"{ESC}[1m›{ESC}[0m{ESC}[38;2;1;2;3m⠁{ESC}[2mAsk anything{ESC}[0m", ""),  # braille animation cell is not text
    ("❯\xa0hello", "hello"),
    (f"❯ {ESC}[2mfaint{ESC}[22m typed", "typed"),  # 22 ends the faint run
    (f"❯ {ESC}[38;2;2;2;2mtyped{ESC}[0m", "typed"),  # the 2 inside a 38;2;r;g;b colour is not "faint"
    (f"❯ {ESC}[38;5;2mtyped{ESC}[0m", "typed"),
    ("❯ half typed", "half typed"),  # plain text cannot tell a suggestion from typing
])
def test_typed_text_after_the_marker(row, expected):
    assert typed_text(row, "❯" if "❯" in row else "›") == expected


def test_typed_text_is_none_without_the_marker():
    assert typed_text("output ❯ not a prompt", "❯") is None
    assert typed_text("", "❯") is None


@pytest.mark.parametrize("row, expected", [
    (f"❯ {ESC}[?25mtyped", "typed"),  # a private-parameter sequence ending in m is not SGR: ignored, never an int() error
    (f"❯ {ESC}[?1;2mtyped", "typed"),
    (f"❯ {ESC}[>4;2mtyped", "typed"),
    (f"❯ {ESC}[38:2::1:2:3mtyped{ESC}[0m", "typed"),  # colon form of a colour: its 2 is not "faint", and the sequence is not text
    (f"❯ {ESC}[38:5:2mtyped{ESC}[0m", "typed"),
    (f"❯ {ESC}[2m{ESC}[38:2::1:2:3mfaint{ESC}[0m", ""),  # a colour does not end the faint run
    (f"❯ {ESC}[4:3mtyped{ESC}[0m", "typed"),  # curly underline sub-parameter
    (f"❯ {ESC}[2:5mfaint{ESC}[0m", ""),
])
def test_typed_text_survives_sequences_that_are_not_plain_semicolon_sgr(row, expected):
    assert typed_text(row, "❯") == expected


def test_strip_ansi_removes_colon_and_private_parameter_sequences():
    assert strip_ansi(f"{ESC}[38:2::1:2:3ma{ESC}[?25mb{ESC}[>4;2mc") == "abc"


def test_typed_text_survives_a_numeric_parameter_longer_than_pythons_int_limit():
    row = "\x1b[" + "9" * 5000 + "m› hello\x1b[0m"
    assert typed_text(row, "›") == "hello"


def test_row_text_drops_faint_runs_and_braille_cells():
    assert row_text(f"  {ESC}[2mfaint{ESC}[0m ⠁⡀ typed ") == "typed"
    assert row_text(f"{ESC}[2mfaint{ESC}[0m") == ""


def test_last_marker_row_is_the_lowest_row_with_the_marker():
    rows = screen_rows(f"❯ old\nout\n❯{ESC}[2m suggestion{ESC}[0m\nfooter\n")
    assert last_marker_row(rows, "❯") == (2, "")
    assert last_marker_row(rows, "›") is None


@pytest.mark.parametrize("screen, expected", [
    ("❯ \n\n─\n", True),
    (f"❯ {ESC}[2msuggestion{ESC}[0m\n─\n", True),
    ("❯ \n  text\n─\n", False),  # the draft is on a row after a bare marker row
    ("❯ \n\n\n  text\n─\n", False),
    ("❯ text\n─\n", False),
])
def test_composer_is_empty_needs_every_row_up_to_the_closing_row_blank(screen, expected):
    rows = screen_rows(screen)
    index, typed = last_marker_row(rows, "❯")
    closing = next(i for i in range(index + 1, len(rows)) if rows[i][1] == "─")
    assert composer_is_empty(rows, index, typed, closing) is expected


def test_composer_is_not_empty_when_the_closing_row_is_not_below_the_marker():
    rows = screen_rows("─\n❯ \n")
    assert not composer_is_empty(rows, 1, "", 0)
