from herdr_engineering.review_gate.fingerprint import finding_fingerprint
from herdr_engineering.review_gate.models import Finding


def _f(path="a.py", message="Unused var x1", line=10, category="style"):
    return Finding(
        path=path,
        category=category,
        message=message,
        severity="low",
        required_change="fix",
        line=line,
    )


def test_line_number_is_ignored():
    assert finding_fingerprint(_f(line=10)) == finding_fingerprint(_f(line=99))


def test_digits_and_whitespace_normalised():
    a = _f(message="Unused  var x1")
    b = _f(message="unused var x2")
    assert finding_fingerprint(a) == finding_fingerprint(b)


def test_different_path_differs():
    assert finding_fingerprint(_f(path="a.py")) != finding_fingerprint(_f(path="b.py"))
