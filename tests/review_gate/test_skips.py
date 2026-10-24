import pytest

from herdr_engineering import cli
from herdr_engineering.review_gate.skips import SkipsError, check_junit, load_disabled


def junit(tmp_path, cases, name="r.xml"):
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{"<skipped/>" if s else ""}</testcase>'
        for c, n, s in cases
    )
    path = tmp_path / name
    path.write_text(f'<testsuites><testsuite name="s">{body}</testsuite></testsuites>')
    return str(path)


def disabled_file(tmp_path, text):
    path = tmp_path / "disabled.yaml"
    path.write_text(text)
    return str(path)


def test_unlisted_skip_fails(tmp_path):
    report = check_junit(
        [junit(tmp_path, [("a.T", "t1", True), ("a.T", "t2", False)])], frozenset()
    )
    assert report.skipped_unlisted == ("a.T::t1",)
    assert report.total == 2


def test_listed_skip_passes_and_unused_entry_is_reported(tmp_path):
    path = junit(tmp_path, [("a.T", "t1", True), ("a.T", "t2", False)])
    report = check_junit([path], frozenset({"a.T::t1", "a.T::t2"}))
    assert report.skipped_unlisted == ()
    assert report.unused_disabled == ("a.T::t2",)


def test_xfail_counts_as_skip(tmp_path):
    path = tmp_path / "x.xml"
    path.write_text(
        '<testsuite><testcase classname="a" name="b"><skipped type="pytest.xfail"/></testcase>'
        "</testsuite>"
    )
    assert check_junit([str(path)], frozenset()).skipped_unlisted == ("a::b",)


def test_zero_testcases_is_bad_input(tmp_path):
    path = tmp_path / "e.xml"
    path.write_text("<testsuites/>")
    with pytest.raises(SkipsError):
        check_junit([str(path)], frozenset())


def test_missing_path_is_bad_input(tmp_path):
    with pytest.raises(SkipsError):
        check_junit([str(tmp_path / "nope.xml")], frozenset())
    with pytest.raises(SkipsError):
        load_disabled(str(tmp_path / "nope.yaml"))


def test_dtd_is_refused(tmp_path):
    path = tmp_path / "d.xml"
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>'
        '<testsuite><testcase classname="a" name="b"/></testsuite>'
    )
    with pytest.raises(SkipsError):
        check_junit([str(path)], frozenset())


def test_malformed_xml_is_bad_input(tmp_path):
    path = tmp_path / "m.xml"
    path.write_text("<testsuite><testcase")
    with pytest.raises(SkipsError):
        check_junit([str(path)], frozenset())


def test_disabled_file_requires_reason_and_rejects_duplicates(tmp_path):
    ok = disabled_file(tmp_path, 'disabled:\n  - {test: "a::b", reason: "flaky"}\n')
    assert load_disabled(ok) == frozenset({"a::b"})
    for text in (
        'disabled:\n  - {test: "a::b", reason: ""}\n',
        'disabled:\n  - {test: "a::b"}\n',
        'disabled:\n  - {test: "a::b", reason: "x"}\n  - {test: "a::b", reason: "y"}\n',
        'disabled:\n  - {test: "nocolons", reason: "x"}\n',
        "disabled: [",
        "- not a mapping\n",
    ):
        with pytest.raises(SkipsError):
            load_disabled(disabled_file(tmp_path, text))


def test_directory_of_junit_files(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    junit(d, [("a", "x", False)], name="1.xml")
    junit(d, [("b", "y", True)], name="2.xml")
    (d / "note.txt").write_text("ignored")
    report = check_junit([str(d)], frozenset())
    assert report.total == 2
    assert report.skipped_unlisted == ("b::y",)
    with pytest.raises(SkipsError):
        check_junit([str(tmp_path / "empty-none")], frozenset())


def test_cli_exit_codes(tmp_path, capsys):
    clean = junit(tmp_path, [("a", "x", False)], name="c.xml")
    dirty = junit(tmp_path, [("a", "x", True)], name="d.xml")
    dis = disabled_file(tmp_path, "disabled: []\n")

    def run(*junits, disabled=dis):
        argv = ["review-gate", "check-skips", "--disabled", disabled]
        for j in junits:
            argv += ["--junit", j]
        return cli.main(argv)

    assert run(clean) == 0
    assert run(dirty) == 1
    assert "a::x" in capsys.readouterr().out
    assert run(str(tmp_path / "missing.xml")) == 2
    assert run(clean, disabled=str(tmp_path / "missing.yaml")) == 2


@pytest.mark.parametrize(
    "raw",
    [
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "b">]><testsuite><testcase classname="a" name="b"/></testsuite>'.encode(
            "utf-16"
        ),
        '<testsuite><testcase classname="a" name="b"/></testsuite>'.encode("utf-16-le"),
        b'<?xml version="1.0" encoding="UTF-16"?><testsuite><testcase classname="a" name="b"/></testsuite>',
        b'<?xml version="1.0" encoding="latin-1"?><testsuite><testcase classname="a" name="b"/></testsuite>',
    ],
)
def test_non_utf8_input_is_bad_input(tmp_path, raw):
    path = tmp_path / "u.xml"
    path.write_bytes(raw)
    with pytest.raises(SkipsError):
        check_junit([str(path)], frozenset())


def test_utf8_declaration_and_bom_are_fine(tmp_path):
    path = tmp_path / "ok.xml"
    path.write_bytes(
        b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>'
        b'<testsuite><testcase classname="a" name="b"/></testsuite>'
    )
    assert check_junit([str(path)], frozenset()).total == 1
