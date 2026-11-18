"""The shared record tail scanner and the session id check, against real files."""
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.herdr_session import is_safe_session_id
from herdr_engineering.handoff_adapters.records import lines_newest_first, records_newest_first


def test_lines_come_newest_first_without_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text('{"n":1}\n\n{"n":2}\n{"n":3}\n')
    assert list(lines_newest_first(path)) == ['{"n":3}', '{"n":2}', '{"n":1}']


def test_a_final_line_without_newline_is_yielded(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text('{"n":1}\n{"n":2')
    assert list(lines_newest_first(path)) == ['{"n":2', '{"n":1}']


def test_empty_file_yields_nothing(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text("")
    assert list(lines_newest_first(path)) == []


@pytest.mark.parametrize("block", [1, 2, 3, 7, 16, 1000])
def test_every_block_size_yields_the_same_whole_lines(tmp_path: Path, block: int) -> None:
    path = tmp_path / "r.jsonl"
    lines = [f'{{"n":{i},"t":"é{"x" * i}"}}' for i in range(12)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert list(lines_newest_first(path, block)) == lines[::-1]


def test_unicode_line_separators_inside_a_line_do_not_split_it(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    odd = '{"text":"a\u2028b\u2029c\u0085d"}'
    path.write_text(f'{{"n":1}}\n{odd}\n{{"n":3}}\n', encoding="utf-8")
    assert list(lines_newest_first(path)) == ['{"n":3}', odd, '{"n":1}']
    assert list(lines_newest_first(path, 5)) == ['{"n":3}', odd, '{"n":1}']
    assert len(odd.splitlines()) == 4  # what str.splitlines() would have done


@pytest.mark.parametrize("good", ["11111111-1111-4111-8111-111111111111", "ses_abc123", "a.b"])
def test_safe_session_ids(good: str) -> None:
    assert is_safe_session_id(good)


@pytest.mark.parametrize("bad", [None, "", "..", "../x", "a/b", "a\\b", "*", "a?b", "[a]"])
def test_unsafe_session_ids(bad: str | None) -> None:
    assert not is_safe_session_id(bad)


def test_records_are_parsed_objects_skipping_half_written_and_non_object_lines(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text('{"n":1}\n[1,2]\n"text"\n{"n":2}\n{"n":3,"cut')
    assert list(records_newest_first(path)) == [{"n": 2}, {"n": 1}]
