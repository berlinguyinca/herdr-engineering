import pytest

from herdr_engineering.urlrewrite import (
    AmbiguousTarget,
    resolve_loopback,
    rewrite_text,
    rewrite_url,
)

L = [{"target_machine_id": "bender", "target_port": 3000, "external_port": 18010, "label": "web"}]
BASE = "dev.lan"


def test_remote_loopback_is_rewritten_to_owner():
    assert rewrite_url("http://localhost:3000/a?b=1", L, from_machine="mac", url_base=BASE) == \
        "http://dev.lan:18010/a?b=1"


def test_same_machine_reference_is_left_alone():
    assert rewrite_url("http://127.0.0.1:3000/", L, from_machine="bender", url_base=BASE) == "http://127.0.0.1:3000/"


def test_unknown_port_and_non_loopback_untouched():
    assert rewrite_url("http://localhost:9/", L, from_machine="mac", url_base=BASE) == "http://localhost:9/"
    assert rewrite_url("http://example.com:3000/", L, from_machine="mac", url_base=BASE) == "http://example.com:3000/"


def test_ambiguous_port_refuses_unless_service_given():
    two = L + [{"target_machine_id": "beast", "target_port": 3000, "external_port": 18011, "label": "api"}]
    with pytest.raises(AmbiguousTarget):
        resolve_loopback(3000, two, from_machine="mac", url_base=BASE)
    r = resolve_loopback(3000, two, from_machine="mac", url_base=BASE, service="api")
    assert r.external == "dev.lan:18011"


def test_text_rewrite_reports_ambiguity_and_keeps_text():
    two = L + [{"target_machine_id": "beast", "target_port": 3000, "external_port": 18011}]
    out, notes = rewrite_text("open http://localhost:3000/x now", two, from_machine="mac", url_base=BASE)
    assert out == "open http://localhost:3000/x now" and notes
    out, notes = rewrite_text("open http://localhost:3000/x now", L, from_machine="mac", url_base=BASE)
    assert out == "open http://dev.lan:18010/x now" and not notes
