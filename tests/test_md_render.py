"""Markdown -> terminal text for the /summary side pane."""
import re

from herdr_engineering.md_render import render_markdown, visible_width

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text, width=80):
    return ANSI.sub("", render_markdown(text, width=width))


def test_markup_is_consumed_not_shown():
    out = plain("# Title\n\nSome **bold**, *italic* and `code` plus [a link](http://x.test/y).\n")
    assert "#" not in out and "**" not in out and "`" not in out and "[a link]" not in out
    assert "bold" in out and "italic" in out and "code" in out and "a link (http://x.test/y)" in out


def test_styles_are_applied_when_color_is_on_and_absent_when_off():
    md = "## Head\n\n**strong** `code`\n"
    on = render_markdown(md, width=60)
    assert "\x1b[" in on and "\x1b[1m" in on                       # bold
    assert "\x1b[" not in render_markdown(md, width=60, color=False)


def test_headings_get_a_rule_and_blank_line_separation():
    lines = plain("intro\n\n# One\n\ntext\n\n## Two\n").split("\n")
    assert lines[lines.index("One") + 1].startswith("═")
    assert lines[lines.index("Two") + 1].startswith("─")
    assert lines[lines.index("One") - 1] == ""


def test_lists_nest_and_wrap_with_a_hanging_indent():
    md = ("1. First item that is long enough to need wrapping across more than one line of output text\n"
          "   - nested bullet\n"
          "2. Second\n")
    lines = plain(md, width=40).split("\n")
    assert lines[0].startswith("1. ")
    cont = lines[1]
    assert cont.startswith("   ") and not cont.startswith("    ")      # hangs under the text, not the number
    assert any("◦ nested bullet" in line for line in lines) and any(line.startswith("2. Second") for line in lines)
    assert all(visible_width(line) <= 40 for line in lines)


def test_wrapping_respects_width_and_does_not_split_inline_code():
    out = render_markdown("word " * 30 + "`a code span` end", width=40)
    for line in out.split("\n"):
        assert visible_width(line) <= 40
    assert "a code span" in plain("word " * 30 + "`a code span` end", width=40).replace("\n", " ")


def test_fenced_code_is_kept_verbatim_and_not_inline_parsed():
    out = plain("```sh\n**not bold** $ x\n```\n", width=50)
    assert "**not bold** $ x" in out and "```" not in out


def test_tables_are_boxed_and_aligned():
    out = plain("| A | B |\n|---|---|\n| one | two |\n| three | 4 |\n", width=60).split("\n")
    assert out[0].startswith("┌") and out[-1].startswith("└")
    widths = {visible_width(line) for line in out}
    assert len(widths) == 1


def test_quote_and_rule():
    out = plain("> quoted words\n\n---\n\nafter\n", width=30)
    assert "▎ quoted words" in out and "─" * 30 in out


def test_header_lines_keep_their_own_lines():
    out = plain("**Session:** a\n**Status:** b\n", width=80).split("\n")
    assert out == ["Session: a", "Status: b"]


def test_a_real_report_renders_without_loss():
    md = open(__file__.replace("test_md_render.py", "fixtures/summary_report_sample.md")).read()
    out = plain(md, width=90)
    for needle in ("Original objective", "Evidence coverage", "release.sh"):
        assert needle in out
    assert all(visible_width(line) <= 90 for line in out.split("\n"))
    assert not re.search(r"\*\*\w", out)                              # no unconsumed bold markers
