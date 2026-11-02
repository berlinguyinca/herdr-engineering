"""Pane mirror and input translation for the Shepherd terminal host relay (no network).

A fake client records calls and returns scripted visible screens; the clock is injected.
"""
import base64

import pytest

from herdr_engineering.herdr_pane_client import HerdrError, HerdrPaneClient
from herdr_engineering.terminal_mirror import InputTranslator, PaneMirror, apply, translate_input

PANE = "w1:p1"
PREFIX = "\x1b[0m\x1b[H\x1b[2J"


class FakeClient:
    def __init__(self, screens=(), layout=None, viewport_rows=24):
        self.screens = list(screens)
        self._layout = layout
        self.viewport_rows = viewport_rows
        self.calls = []
        self.reads = 0

    def read_visible(self, pane_id):
        self.reads += 1
        return self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)

    def layout(self, pane_id):
        if self._layout is None:
            raise HerdrError("pane_not_in_layout", "x")
        return {"width": self._layout[1], "height": self._layout[0]}

    def list_panes(self):
        return [{"pane_id": PANE, "scroll": {"viewport_rows": self.viewport_rows}}]

    def send_text(self, pane_id, text):
        self.calls.append(("text", pane_id, text))

    def send_keys(self, pane_id, keys):
        self.calls.append(("keys", pane_id, list(keys)))

    def send_input(self, pane_id, data):
        self.calls.append(("input", pane_id, bytes(data)))


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def mirror(client, clock=None, **kw):
    clock = clock or Clock()
    return PaneMirror(client, PANE, now=clock.now, sleep=clock.sleep, **kw), clock


def decode(frame):
    return base64.b64decode(frame["data"]).decode()


# -- mirror ------------------------------------------------------------------------------------
def test_first_call_returns_frame_with_clear_prefix_and_crlf():
    m, _ = mirror(FakeClient(["hello\nworld"], layout=(10, 40)))
    f = m.frame()
    assert decode(f) == PREFIX + "hello\r\nworld"
    assert (f["rows"], f["cols"]) == (10, 40)


def test_unchanged_screen_yields_no_frame():
    m, clock = mirror(FakeClient(["same"], layout=(5, 5)))
    assert m.frame() is not None
    clock.t += 1
    assert m.frame() is None


def test_changed_screen_yields_one_frame():
    m, clock = mirror(FakeClient(["a", "b"], layout=(5, 5)))
    assert decode(m.frame()) == PREFIX + "a"
    clock.t += 1
    assert decode(m.frame()) == PREFIX + "b"


def test_frames_coalesce_at_min_interval():
    c = FakeClient(["a", "b", "b"], layout=(5, 5))
    m, clock = mirror(c, min_interval=0.1)
    assert m.frame() is not None
    clock.t += 0.05
    assert m.frame() is None  # too soon: no read, the change is kept for later
    assert c.reads == 1
    clock.t += 0.06
    assert decode(m.frame()) == PREFIX + "b"


def test_existing_crlf_not_doubled_and_one_trailing_newline_dropped():
    m, _ = mirror(FakeClient(["a\r\nb\n"], layout=(2, 2)))
    assert decode(m.frame()) == PREFIX + "a\r\nb"


def test_rows_cols_fall_back_to_viewport_and_longest_visible_line():
    c = FakeClient(["\x1b[31mred\x1b[0m\nlonger line"], viewport_rows=33)
    m, _ = mirror(c)
    f = m.frame()
    assert (f["rows"], f["cols"]) == (33, 11)  # escape codes do not count towards the width


def test_rows_fall_back_to_line_count_when_pane_unlisted():
    c = FakeClient(["a\nb\nc"])
    c.list_panes = lambda: []
    m, _ = mirror(c)
    assert m.frame()["rows"] == 3


def test_wait_for_change_polls_every_100ms_until_changed():
    c = FakeClient(["a", "a", "a", "b"], layout=(5, 5))
    m, clock = mirror(c)
    m.frame()
    assert m.wait_for_change(5) is True
    assert clock.sleeps == pytest.approx([0.1, 0.1])


def test_wait_for_change_times_out_when_screen_static():
    c = FakeClient(["a"], layout=(5, 5))
    m, clock = mirror(c)
    m.frame()
    assert m.wait_for_change(0.35) is False
    assert 0.3 <= sum(clock.sleeps) <= 0.4


def test_wait_for_change_true_at_once_before_any_frame():
    m, clock = mirror(FakeClient(["a"], layout=(5, 5)))
    assert m.wait_for_change(1) is True
    assert clock.sleeps == []


def test_wait_for_change_respects_min_interval_before_returning():
    c = FakeClient(["a", "b"], layout=(5, 5))
    m, clock = mirror(c, min_interval=0.1)
    m.frame()
    assert m.wait_for_change(5) is True
    assert sum(clock.sleeps) >= 0.1 - 1e-9
    assert m.frame() is not None  # no spin: the frame is ready right after


def test_read_errors_propagate():
    c = FakeClient()

    def boom(_):
        raise HerdrError("unavailable", "gone")

    c.read_visible = boom
    m, _ = mirror(c)
    with pytest.raises(HerdrError):
        m.frame()


# -- translation table -------------------------------------------------------------------------
@pytest.mark.parametrize("data,expected", [
    (b"\r", [("keys", ["enter"])]),
    (b"\n", [("keys", ["enter"])]),
    (b"\x7f", [("keys", ["backspace"])]),
    (b"\t", [("keys", ["tab"])]),
    (b"\x1b[A", [("keys", ["up"])]),
    (b"\x1b[B", [("keys", ["down"])]),
    (b"\x1b[C", [("keys", ["right"])]),
    (b"\x1b[D", [("keys", ["left"])]),
    (b"\x1bOA", [("keys", ["up"])]),
    (b"\x1bOB", [("keys", ["down"])]),
    (b"\x1bOC", [("keys", ["right"])]),
    (b"\x1bOD", [("keys", ["left"])]),
    (b"\x1b[Z", [("keys", ["shift+tab"])]),
    (b"\x1b[1;3D", [("keys", ["alt+left"])]),
    (b"\x1b[1;5D", [("keys", ["ctrl+left"])]),
    (b"\x1bOP", [("keys", ["f1"])]),
    (b"\x03", [("keys", ["ctrl+c"])]),
    (b"\x01", [("keys", ["ctrl+a"])]),
    (b"\x1a", [("keys", ["ctrl+z"])]),
    (b"\x1bb", [("keys", ["alt+b"])]),
    (b"\x1bz", [("keys", ["alt+z"])]),
    (b"\x1b[H", [("raw", b"\x1b[H")]),
    (b"\x1b[1~", [("raw", b"\x1b[1~")]),
    (b"\x1b[F", [("raw", b"\x1b[F")]),
    (b"\x1b[4~", [("raw", b"\x1b[4~")]),
    (b"\x1b[5~", [("raw", b"\x1b[5~")]),
    (b"\x1b[6~", [("raw", b"\x1b[6~")]),
    (b"\x1b[3~", [("raw", b"\x1b[3~")]),
    (b"\x1b[2~", [("raw", b"\x1b[2~")]),
    (b"\x1b[15~", [("raw", b"\x1b[15~")]),
    (b"\x1bOQ", [("raw", b"\x1bOQ")]),
    (b"\x1b[<0;10;5M", [("raw", b"\x1b[<0;10;5M")]),
    (b"\x1b[M !!", [("raw", b"\x1b[M !!")]),
    (b"\x1b[1;2A", [("raw", b"\x1b[1;2A")]),
    (b"\x00", [("raw", b"\x00")]),
    (b"\x1c", [("raw", b"\x1c")]),
    (b"\x1bB", [("raw", b"\x1bB")]),
    (b"\x1b1", [("raw", b"\x1b1")]),
    (b"hello", [("text", "hello")]),
    (b"a b", [("text", "a b")]),
    (b"", []),
])
def test_translation_table(data, expected):
    assert translate_input(data) == expected


def test_lone_escape_alone_is_held_until_flush():
    t = InputTranslator()
    assert t.feed(b"\x1b") == []
    assert t.flush() == [("keys", ["esc"])]
    assert t.flush() == []


def test_translate_input_wrapper_flushes_lone_escape():
    assert translate_input(b"\x1b") == [("keys", ["esc"])]


def test_double_escape_is_esc_then_held_esc():
    t = InputTranslator()
    assert t.feed(b"\x1b\x1b") == [("keys", ["esc"])]
    assert t.flush() == [("keys", ["esc"])]


def test_mixed_run_text_then_enter():
    assert translate_input(b"ls\r") == [("text", "ls"), ("keys", ["enter"])]


def test_mixed_run_keys_between_text():
    assert translate_input(b"ab\x03cd\x1b[Aef") == [
        ("text", "ab"), ("keys", ["ctrl+c"]), ("text", "cd"), ("keys", ["up"]), ("text", "ef")]


def test_utf8_text():
    assert translate_input("héllo 世界 😀".encode()) == [("text", "héllo 世界 😀")]


def test_chunk_boundary_utf8_char_split():
    t = InputTranslator()
    b = "a世".encode()
    assert t.feed(b[:2]) == [("text", "a")]
    assert t.feed(b[2:]) == [("text", "世")]


def test_chunk_boundary_csi_split_everywhere():
    seq = b"\x1b[1;5D"
    for cut in range(1, len(seq)):
        t = InputTranslator()
        assert t.feed(b"x" + seq[:cut]) == [("text", "x")]
        assert t.feed(seq[cut:]) == [("keys", ["ctrl+left"])]


def test_chunk_boundary_ss3_and_unknown_csi():
    t = InputTranslator()
    assert t.feed(b"\x1bO") == []
    assert t.feed(b"A") == [("keys", ["up"])]
    assert t.feed(b"\x1b[5") == []
    assert t.feed(b"~z") == [("raw", b"\x1b[5~"), ("text", "z")]


def test_chunk_boundary_x10_mouse_report():
    t = InputTranslator()
    assert t.feed(b"\x1b[M ") == []
    assert t.feed(b"!!") == [("raw", b"\x1b[M !!")]


def test_flush_of_partial_csi_goes_raw():
    t = InputTranslator()
    assert t.feed(b"\x1b[1;") == []
    assert t.flush() == [("raw", b"\x1b[1;")]


def test_runaway_escape_sequence_is_bounded():
    t = InputTranslator()
    out = t.feed(b"\x1b[" + b"1;" * 100)
    assert out and all(a[0] == "raw" for a in out)
    assert t.flush() == []  # nothing left pending


def test_invalid_utf8_goes_raw():
    assert translate_input(b"ab\xffcd") == [("text", "ab"), ("raw", b"\xff"), ("text", "cd")]
    assert translate_input(b"\xc3(") == [("raw", b"\xc3"), ("text", "(")]


def test_truncated_utf8_flushed_raw():
    t = InputTranslator()
    assert t.feed(b"a\xe4\xb8") == [("text", "a")]
    assert t.flush() == [("raw", b"\xe4\xb8")]


def test_paste_inside_is_text_markers_raw():
    out = translate_input(b"\x1b[200~line1\nline2\r\x1b[A\x1b[201~")
    assert out == [("raw", b"\x1b[200~"), ("text", "line1\nline2\r\x1b[A"), ("raw", b"\x1b[201~")]


def test_paste_across_chunks_and_split_end_marker():
    t = InputTranslator()
    assert t.feed(b"\x1b[200~ab") == [("raw", b"\x1b[200~"), ("text", "ab")]
    assert t.feed(b"cd\x1b[20") == [("text", "cd")]
    assert t.feed(b"1~\r") == [("raw", b"\x1b[201~"), ("keys", ["enter"])]


def test_paste_end_marker_false_alarm_stays_text():
    t = InputTranslator()
    t.feed(b"\x1b[200~")
    assert t.feed(b"\x1b[2") == []
    assert t.feed(b"Xq") == [("text", "\x1b[2Xq")]


def test_paste_flush_emits_held_text():
    t = InputTranslator()
    t.feed(b"\x1b[200~")
    assert t.feed(b"\x1b") == []
    assert t.flush() == [("text", "\x1b")]


def test_100kb_paste_splits_into_4kb_text_actions():
    data = (b"abc\xe4\xb8\x96" * 20000)[:100 * 1024]
    data = data[:data.rfind(b"c") + 1]
    out = translate_input(b"\x1b[200~" + data + b"\x1b[201~")
    texts = [a[1] for a in out if a[0] == "text"]
    assert len(texts) >= 25
    assert all(len(s.encode()) <= 4096 for s in texts)
    assert "".join(texts).encode() == data
    assert out[0] == ("raw", b"\x1b[200~") and out[-1] == ("raw", b"\x1b[201~")


def test_100kb_plain_text_splits_on_char_boundaries():
    data = "é世😀" * 9000
    out = translate_input(data.encode())
    assert all(a[0] == "text" and len(a[1].encode()) <= 4096 for a in out)
    assert "".join(a[1] for a in out) == data


def test_oversized_invalid_run_raw_split_to_4kb():
    out = translate_input(b"\xff" * 10000)
    assert all(a[0] == "raw" and len(a[1]) <= 4096 for a in out)
    assert b"".join(a[1] for a in out) == b"\xff" * 10000


def test_emitted_keys_are_all_valid_for_the_client():
    from herdr_engineering.herdr_pane_client import _KEY
    raw = bytes(range(256)) + b"\x1b[A\x1b[Z\x1b[1;3D\x1b[1;5D\x1bOP\x1bq"
    for a in translate_input(raw):
        if a[0] == "keys":
            assert all(_KEY.fullmatch(k) for k in a[1])
        if a[0] == "text":
            assert len(a[1].encode()) <= 4096


# -- apply -------------------------------------------------------------------------------------
def test_apply_performs_actions_in_order_batching_keys():
    c = FakeClient()
    apply(c, PANE, translate_input(b"ls\r\x03\x1b[H\x1b[Aok"))
    assert c.calls == [
        ("text", PANE, "ls"),
        ("keys", PANE, ["enter", "ctrl+c"]),
        ("input", PANE, b"\x1b[H"),
        ("keys", PANE, ["up"]),
        ("text", PANE, "ok"),
    ]


def test_apply_splits_more_than_32_keys():
    c = FakeClient()
    apply(c, PANE, [("keys", ["enter"])] * 70)
    assert [len(x[2]) for x in c.calls] == [32, 32, 6]


def test_apply_drops_raw_that_is_not_utf8():
    c = FakeClient()
    apply(c, PANE, [("raw", b"\xff"), ("text", "a")])
    assert c.calls == [("text", PANE, "a")]


def test_apply_with_real_client_validators_accepts_every_translation():
    class Spy(HerdrPaneClient):
        def __init__(self):
            super().__init__("/nonexistent")
            self.sent = []

        def _call(self, method, params):
            self.sent.append((method, params))
            return {}

    c = Spy()
    apply(c, PANE, translate_input(b"ls\r\x03\x1b[1;5D\x1bb\x1b[5~\x7f\t\x1b[Z" + "é".encode()))
    assert [m for m, _ in c.sent] == ["pane.send_text", "pane.send_keys", "pane.send_input",
                                      "pane.send_keys", "pane.send_text"]
