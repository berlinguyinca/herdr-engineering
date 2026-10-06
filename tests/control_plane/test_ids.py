import pytest

from herdr_engineering.control_plane import ids


def test_new_id_prefix_and_shape():
    v = ids.new_id("mission")
    assert v.startswith("mission_")
    assert len(v) == len("mission_") + 26


def test_ids_are_time_ordered():
    a = ids.new_id("session")
    b = ids.new_id("session")
    assert a < b  # ULID suffix is time-sortable


def test_is_valid_id():
    assert ids.is_valid_id(ids.new_id("service"), "service")
    assert not ids.is_valid_id("mission_XXXX", "mission")
    assert not ids.is_valid_id(ids.new_id("mission"), "service")


def test_invalid_kind_rejected():
    with pytest.raises(ValueError):
        ids.new_id("bogus")
