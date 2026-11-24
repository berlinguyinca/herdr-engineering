"""Reading a pane screen as Herdr returns it (`pane read --format ansi`): colour codes in every row."""
import re

_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*[@-~])")  # ECMA-48: parameter bytes are 0x30-0x3F, so `:` and `?` too
_BRAILLE = range(0x2800, 0x2900)  # Codex animates the empty composer with braille cells outside the placeholder


def strip_ansi(text: str) -> str:
    return _CSI.sub("", text)


def screen_rows(screen: str) -> list[tuple[str, str]]:
    """(raw row, row without colour codes or edge spaces) for each row, trailing blank padding rows dropped."""
    rows = [(raw, strip_ansi(raw).strip()) for raw in screen.splitlines()]
    while rows and not rows[-1][1]:
        rows.pop()
    return rows


def _dim_after(dim: bool, params: str) -> bool:
    """The dim (faint) state after one SGR sequence; never raises.

    A sequence with private parameters (`?`, `>`, `<`, `=`) is not SGR and changes nothing. Colour codes 38/48/58 swallow
    their arguments: `38;5;n` and `38;2;r;g;b` as separate parameters, `38:2::r:g:b` as one colon-joined parameter.
    """
    if params[:1] in ("?", ">", "<", "="):
        return dim
    parts = params.split(";")
    index = 0
    while index < len(parts):
        head = parts[index].split(":")[0]  # a colon-joined parameter is one code with sub-parameters
        code = int(head) if head.isascii() and head.isdigit() and len(head) <= 9 else 0 if head == "" else -1  # longer is no SGR code, and would pass Python's int-string limit
        if code in (38, 48, 58):
            if ":" not in parts[index]:
                following = parts[index + 1] if index + 1 < len(parts) else ""
                index += 1 + (4 if following == "2" else 2)
                continue
        elif code in (0, 22):
            dim = False
        elif code == 2:
            dim = True
        index += 1
    return dim


def _visible(row: str) -> list[tuple[str, bool]]:
    """(char, faint) for each char of a raw (coloured) row."""
    dim = False
    text: list[tuple[str, bool]] = []
    position = 0
    for match in _CSI.finditer(row):
        text.extend((char, dim) for char in row[position:match.start()])
        if match.group(2) == "m":
            dim = _dim_after(dim, match.group(1))
        position = match.end()
    text.extend((char, dim) for char in row[position:])
    return text


def _unfaint(chars: list[tuple[str, bool]]) -> str:
    return "".join(char for char, faint in chars if not faint and ord(char) not in _BRAILLE).strip()


def row_text(row: str) -> str:
    """The text of a raw (coloured) row without faint runs and braille animation cells, edge spaces dropped."""
    return _unfaint(_visible(row))


def typed_text(row: str, marker: str) -> str | None:
    """The text typed after ``marker`` in a raw (coloured) row, or None when the row has no such marker.

    The empty prompt of Claude Code and Codex shows a faint suggestion after the marker; that is not
    typed text, and neither are Codex's braille animation cells. A row without colour codes
    (plain text) cannot tell the two apart, so any text after the marker counts as typed.
    """
    text = _visible(row)
    joined = "".join(char for char, _ in text)
    if not joined.strip().startswith(marker):
        return None
    return _unfaint(text[joined.index(marker) + len(marker):])


def last_marker_row(rows: list[tuple[str, str]], marker: str) -> tuple[int, str] | None:
    """(index, text typed after the marker) of the last row of ``screen_rows`` that starts with ``marker``, else None."""
    for index in range(len(rows) - 1, -1, -1):
        typed = typed_text(rows[index][0], marker)
        if typed is not None:
            return index, typed
    return None


def composer_is_empty(rows: list[tuple[str, str]], marker_index: int, typed: str, closing_index: int) -> bool:
    """True when nothing is typed in the composer that opens at ``marker_index`` and closes at ``closing_index``.

    A multi-line draft can leave the marker row bare with its text on a following row, so every row from the marker
    row to the closing row (the rule or status row under the composer) must be blank too; faint placeholder text and
    braille animation cells do not count as text.
    """
    return (not typed and closing_index > marker_index
            and all(not row_text(raw) for raw, _ in rows[marker_index + 1:closing_index]))
