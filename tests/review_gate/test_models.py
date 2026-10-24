import pytest

from herdr_engineering.review_gate.models import PrKey


def test_prkey_round_trip():
    key = PrKey.parse("github.com/acme/widgets#42")
    assert key == PrKey("github.com/acme/widgets", 42)
    assert str(key) == "github.com/acme/widgets#42"


@pytest.mark.parametrize(
    "text",
    [
        "acme/widgets#1",
        "github.com/acme#1",
        "github.com/acme/widgets#0",
        "github.com/acme/widgets#-1",
        "github.com/acme/widgets#x",
        "github.com/acme/widgets#١٢",
        "github.com/acme/widgets",
        "#5",
    ],
)
def test_prkey_invalid(text):
    with pytest.raises(ValueError):
        PrKey.parse(text)
