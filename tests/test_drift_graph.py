"""The drift-graph block: extracted from the report, validated against the real prompts."""
import json

import pytest

from herdr_engineering import drift_graph as dg


def prompts(n, ts=True):
    return [{"n": i, "ts": f"2026-01-01T{9 + i // 60:02d}:{i % 60:02d}:00Z" if ts else None, "text": f"p{i}"}
            for i in range(1, n + 1)]


def block(segments, **extra):
    return json.dumps({"segments": segments, **extra})


GOOD = [{"label": "USB stick", "from_prompt": 1, "to_prompt": 5},
        {"label": "web page", "from_prompt": 6, "to_prompt": 12}]
REPORT = "# Session summary\n\nbody\n\n```drift-graph\n{\"segments\": []}\n```\n\nEvidence coverage: complete\n"


def test_extract_block_strips_all_blocks_and_gives_no_graph_when_there_are_two():
    text = "# S\n\n```drift-graph\n{\"a\": 1}\n```\n\nmiddle\n\n```drift-graph\n{\"b\": 2}\n```\n\nend\n"
    clean, raw = dg.extract_block(text)
    assert "drift-graph" not in clean and "{" not in clean and "middle" in clean and clean.rstrip().endswith("end")
    assert raw is None                                       # two blocks: which one the model meant is a guess
    assert "\n\n\n" not in clean


def test_report_without_block_is_unchanged():
    text = "# S\n\n```python\nprint(1)\n```\n\nEvidence coverage: complete\n"
    assert dg.extract_block(text) == (text, None)


def test_extract_block_leaves_other_fences_alone():
    clean, raw = dg.extract_block(REPORT.replace("```drift-graph", "```json"))
    assert raw is None and "```json" in clean


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"segments": "x"}', '{"segments": [1, 2]}'])
def test_unusable_input_gives_none(raw):
    assert dg.build_graph(raw, prompts(12), 12) is None


@pytest.mark.parametrize("segments", [
    [GOOD[0]],                                                                         # one segment
    [{"label": f"s{i}", "from_prompt": i, "to_prompt": i} for i in range(1, 10)],      # nine segments
    [GOOD[0], {"label": "b", "from_prompt": 7, "to_prompt": 12}],                      # gap
    [{"label": "a", "from_prompt": 2, "to_prompt": 5}, {"label": "b", "from_prompt": 6, "to_prompt": 12}],  # not from 1
    [GOOD[0], {"label": "b", "from_prompt": 6, "to_prompt": 13}],                      # past the total
    [GOOD[0], {"label": "b", "from_prompt": 6, "to_prompt": 5}],                       # backwards
    [{"label": "", "from_prompt": 1, "to_prompt": 5}, GOOD[1]],                        # empty label
])
def test_invalid_segments_give_none(segments):
    assert dg.build_graph(block(segments), prompts(12), 12) is None


def test_fewer_than_five_prompts_gives_none():
    assert dg.build_graph(block([{"label": "a", "from_prompt": 1, "to_prompt": 2},
                                 {"label": "b", "from_prompt": 3, "to_prompt": 4}]), prompts(4), 4) is None


def test_valid_graph_computes_counts_and_start_times_from_prompts():
    ps = prompts(12)
    g = dg.build_graph(block(GOOD), ps, 12)
    assert g["total_prompts"] == 12
    assert [(s["from_prompt"], s["to_prompt"], s["count"]) for s in g["segments"]] == [(1, 5, 5), (6, 12, 7)]
    assert [s["start"] for s in g["segments"]] == [ps[0]["ts"], ps[5]["ts"]]
    assert g["drift"] is None


def test_model_supplied_times_and_counts_are_ignored():
    segs = [dict(GOOD[0], count=99, start="1999-01-01T00:00:00Z"), GOOD[1]]
    g = dg.build_graph(block(segs), prompts(12), 12)
    assert g["segments"][0]["count"] == 5 and g["segments"][0]["start"].startswith("2026")


def test_start_is_none_when_the_prompt_has_no_time():
    g = dg.build_graph(block(GOOD), prompts(12, ts=False), 12)
    assert [s["start"] for s in g["segments"]] == [None, None]


def test_drift_is_kept_on_a_later_segment_start():
    g = dg.build_graph(block(GOOD, drift_at=6, drift_note="this is a web page task"), prompts(12), 12)
    assert g["drift"] == {"at": 6, "note": "this is a web page task", "worked_s": None, "span_s": None, "fresh": None, "cached": None}


@pytest.mark.parametrize("at", [1, 3, 99, "x", None])
def test_bad_drift_is_dropped_but_segments_are_kept(at):
    g = dg.build_graph(block(GOOD, drift_at=at, drift_note="n"), prompts(12), 12)
    assert g is not None and g["drift"] is None and len(g["segments"]) == 2


def test_labels_and_note_are_redacted_and_clipped():
    secret = "sk-ant-api03-SECRETSECRETSECRETSECRET1234"
    segs = [{"label": f"{secret} " + "x" * 80, "from_prompt": 1, "to_prompt": 5}, GOOD[1]]
    g = dg.build_graph(block(segs, drift_at=6, drift_note="n" * 300), prompts(12), 12)
    assert "SECRETSECRET" not in g["segments"][0]["label"]
    assert len(g["segments"][0]["label"]) <= dg.MAX_LABEL and g["segments"][0]["label"].endswith("…")
    assert len(g["drift"]["note"]) <= dg.MAX_NOTE


def test_sampled_sessions_use_the_true_total():
    ps = prompts(12)
    g = dg.build_graph(block([{"label": "a", "from_prompt": 1, "to_prompt": 300},
                              {"label": "b", "from_prompt": 301, "to_prompt": 500}]), ps + [{"n": 13, "ts": None}], 500)
    assert g is None                      # n above the prompts we hold has no time source: refuse rather than guess
    full = [{"n": i, "ts": f"2026-01-01T00:00:{i % 60:02d}Z"} for i in range(1, 501)]
    g = dg.build_graph(block([{"label": "a", "from_prompt": 1, "to_prompt": 300},
                              {"label": "b", "from_prompt": 301, "to_prompt": 500}]), full, 500)
    assert [s["count"] for s in g["segments"]] == [300, 200]


def test_agents_counts_dispatches_inside_each_segment_and_is_none_without_dispatches():
    ps = prompts(12)
    during_second = [ps[6]["ts"], ps[7]["ts"], None]
    g = dg.build_graph(block(GOOD), ps, 12, dispatch_ts=during_second)
    assert [s["agents"] for s in g["segments"]] == [0, 2]
    assert [s["agents"] for s in dg.build_graph(block(GOOD), ps, 12, dispatch_ts=[])["segments"]] == [None, None]
    assert [s["agents"] for s in dg.build_graph(block(GOOD), ps, 12)["segments"]] == [None, None]


def _raw(**over):
    d = {"segments": [{"label": "A", "from_prompt": 1, "to_prompt": 3}, {"label": "B", "from_prompt": 4, "to_prompt": 6}]}
    d.update(over)
    return json.dumps(d)


PROMPTS = [{"n": i, "ts": f"2026-01-01T0{i}:00:00Z"} for i in range(1, 7)]


def test_control_characters_never_reach_a_label_or_note():
    raw = _raw(segments=[{"label": "a\x1b[2Jb\x07c", "from_prompt": 1, "to_prompt": 3},
                         {"label": "B", "from_prompt": 4, "to_prompt": 6}], drift_at=4, drift_note="n\x1b]0;x\x07\u200bm")
    g = dg.build_graph(raw, PROMPTS, 6)
    text = g["segments"][0]["label"] + g["drift"]["note"]
    assert not any(ord(c) < 32 or ord(c) == 127 or c == "\u200b" for c in text)
    assert g["segments"][0]["label"].startswith("a") and "c" in g["segments"][0]["label"]


def test_a_label_or_note_that_is_not_a_string_drops_the_graph():
    bad = [{"label": ["x"], "from_prompt": 1, "to_prompt": 3}, {"label": "B", "from_prompt": 4, "to_prompt": 6}]
    assert dg.build_graph(_raw(segments=bad), PROMPTS, 6) is None
    assert dg.build_graph(_raw(drift_at=4, drift_note=["x"]), PROMPTS, 6)["drift"]["note"] == ""


def test_agent_counts_need_both_segment_boundaries_known():
    prompts = [dict(p) for p in PROMPTS]
    prompts[3]["ts"] = None                                   # segment 2 starts at an unknown time
    g = dg.build_graph(_raw(), prompts, 6, ["2026-01-01T06:30:00Z"])
    assert [s["agents"] for s in g["segments"]] == [None, None]  # segment 1 has no end time, segment 2 no start time


def test_two_blocks_drop_the_graph_but_both_are_stripped():
    block = "```drift-graph\n" + _raw() + "\n```\n"
    clean, raw = dg.extract_block("# R\n\n" + block + "\n" + block)
    assert "drift-graph" not in clean and raw is None


def test_crlf_blocks_are_found_and_stripped():
    text = "# R\r\n\r\n```drift-graph\r\n" + _raw() + "\r\n```\r\n"
    clean, raw = dg.extract_block(text)
    assert "drift-graph" not in clean and "\r" not in clean and dg.build_graph(raw, PROMPTS, 6) is not None


def test_an_unterminated_block_is_stripped_and_gives_no_graph():
    clean, raw = dg.extract_block("# R\n\nbody\n\n```drift-graph\n" + _raw()[:40])
    assert "drift-graph" not in clean and "body" in clean and raw is None


# --- segment metrics: time worked and tokens -------------------------------------------------------------

def timed(starts):
    """12 prompts where prompt 1 and prompt 6 start at the given times; the rest are placeholders in between."""
    ps = prompts(12)
    ps[0]["ts"], ps[5]["ts"] = starts
    return ps


def test_worked_seconds_drops_idle_gaps_but_keeps_short_ones():
    ts = ["2026-01-01T09:00:00Z", "2026-01-01T09:10:00Z", "2026-01-01T09:40:00Z",     # 10 + 30 min, both within the limit
          "2026-01-01T20:00:00Z", "2026-01-01T20:10:00Z"]                              # 11 h gap dropped, then 10 min
    assert dg.worked_seconds(ts, "2026-01-01T09:00:00Z", None) == 50 * 60
    assert dg.worked_seconds(ts, "2026-01-01T09:00:00Z", None, idle=20 * 60) == 20 * 60   # the 30 min gap is idle now


def test_worked_seconds_ignores_untimed_and_garbage_entries_and_order():
    ts = ["2026-01-01T09:10:00Z", None, "garbage", "2026-01-01T09:00:00Z"]
    assert dg.worked_seconds(ts, "2026-01-01T09:00:00Z", None) == 10 * 60


def test_segments_report_worked_time_and_wall_clock_span():
    ps = timed(("2026-01-01T09:00:00Z", "2026-01-01T19:40:00Z"))
    activity = ["2026-01-01T09:00:00Z", "2026-01-01T09:10:00Z", "2026-01-01T09:20:00Z", "2026-01-01T09:40:00Z",
                "2026-01-01T19:40:00Z", "2026-01-01T19:50:00Z", "2026-01-01T20:00:00Z"]
    g = dg.build_graph(block(GOOD), ps, 12, activity_ts=activity)
    first, second = g["segments"]
    assert (first["worked_s"], first["span_s"]) == (40 * 60, 10 * 3600 + 40 * 60)     # the overnight-style gap is not work
    assert (second["worked_s"], second["span_s"]) == (20 * 60, 20 * 60)
    assert g["totals"]["worked_s"] == 60 * 60 and g["totals"]["span_s"] == 11 * 3600


def test_time_is_none_without_activity_or_without_a_known_boundary():
    ps = timed(("2026-01-01T09:00:00Z", "2026-01-01T09:30:00Z"))
    plain = dg.build_graph(block(GOOD), ps, 12)
    assert [(s["worked_s"], s["span_s"]) for s in plain["segments"]] == [(None, None), (None, None)]
    assert plain["totals"]["worked_s"] is None
    ps[5]["ts"] = None
    g = dg.build_graph(block(GOOD), ps, 12, activity_ts=["2026-01-01T09:00:00Z", "2026-01-01T09:05:00Z"])
    assert [s["worked_s"] for s in g["segments"]] == [None, None]     # segment 1 has no end, segment 2 no start


def test_tokens_are_split_at_the_segment_boundary_and_never_guessed():
    ps = timed(("2026-01-01T09:00:00Z", "2026-01-01T09:30:00Z"))
    usage = [{"ts": "2026-01-01T09:05:00Z", "fresh": 100, "cached": 1000},
             {"ts": "2026-01-01T09:29:59Z", "fresh": 50, "cached": 500},
             {"ts": "2026-01-01T09:30:00Z", "fresh": 7, "cached": 70},
             {"ts": None, "fresh": 3, "cached": 30}]                     # cannot be placed, but is in the total
    g = dg.build_graph(block(GOOD), ps, 12, usage=usage)
    assert [(s["fresh"], s["cached"]) for s in g["segments"]] == [(150, 1500), (7, 70)]
    assert (g["totals"]["fresh"], g["totals"]["cached"]) == (160, 1600)


def test_no_usage_means_none_never_zero():
    ps = timed(("2026-01-01T09:00:00Z", "2026-01-01T09:30:00Z"))
    for usage in (None, []):
        g = dg.build_graph(block(GOOD), ps, 12, usage=usage)
        assert [s["fresh"] for s in g["segments"]] == [None, None] and g["totals"]["fresh"] is None
    only_first = [{"ts": "2026-01-01T09:05:00Z", "fresh": 5, "cached": 0}]
    assert dg.build_graph(block(GOOD), ps, 12, usage=only_first)["segments"][1]["fresh"] is None


def test_the_drift_carries_the_cost_of_everything_after_it():
    ps = timed(("2026-01-01T09:00:00Z", "2026-01-01T09:30:00Z"))
    activity = ["2026-01-01T09:00:00Z", "2026-01-01T09:30:00Z", "2026-01-01T09:55:00Z"]
    usage = [{"ts": "2026-01-01T09:05:00Z", "fresh": 100, "cached": 1}, {"ts": "2026-01-01T09:40:00Z", "fresh": 40, "cached": 4}]
    g = dg.build_graph(block(GOOD, drift_at=6, drift_note="n"), ps, 12, activity_ts=activity, usage=usage)
    assert g["drift"]["worked_s"] == 25 * 60 and g["drift"]["span_s"] == 25 * 60 and g["drift"]["fresh"] == 40 and g["drift"]["cached"] == 4
    assert dg.build_graph(block(GOOD), ps, 12, activity_ts=activity, usage=usage)["drift"] is None


def test_main_session_only_is_carried_when_children_exist():
    ps = timed(("2026-01-01T09:00:00Z", "2026-01-01T09:30:00Z"))
    usage = [{"ts": "2026-01-01T09:05:00Z", "fresh": 1, "cached": 0}]
    assert dg.build_graph(block(GOOD), ps, 12, usage=usage, main_only=True)["totals"]["main_only"] is True
    assert dg.build_graph(block(GOOD), ps, 12, usage=usage)["totals"]["main_only"] is False
