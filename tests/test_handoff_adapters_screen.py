"""Reading coloured pane rows: the faint suggestion after a prompt marker is not typed text."""
import pytest

from herdr_engineering.handoff_adapters.screen import screen_rows, strip_ansi, typed_text

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
