"""Three-way manifest merge (plan 2026-10-30, Task 6; spec section B merge rules)."""
from __future__ import annotations

import hashlib

import pytest

from herdr_engineering.control_plane.artifact_merge import (
    FileConflict,
    ManifestMerge,
    merge_manifests,
)


def entry(name: str, text: str | bytes, **extra) -> dict:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "blob": data,
        **extra,
    }


def read_text(e: dict) -> str:
    return e["blob"].decode("utf-8")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, template: dict) -> dict:
        self.calls.append((name, template))
        return entry(name, template["text"], media_type=template.get("media_type"), stored=True)


def merge(base, head, mine, **kw) -> tuple[ManifestMerge, Recorder]:
    rec = Recorder()
    return merge_manifests(base, head, mine, read_text=read_text, store_text=rec, **kw), rec


def names(m: ManifestMerge) -> list[str]:
    return [f["name"] for f in m.files]


def test_added_on_one_side_only() -> None:
    a = entry("a.md", "A\n")
    b = entry("b.md", "B\n")
    m, rec = merge([], [a], [b], )
    assert names(m) == ["a.md", "b.md"]
    assert m.conflicts == [] and m.merged_names == []
    assert m.clean_names == ["a.md", "b.md"]
    assert rec.calls == []


def test_delete_one_side_other_unchanged() -> None:
    f = entry("a.md", "x\n")
    m, _ = merge([f], [], [f])
    assert m.files == [] and m.conflicts == []
    m, _ = merge([f], [f], [])
    assert m.files == [] and m.conflicts == []


def test_both_delete_and_untouched() -> None:
    f = entry("a.md", "x\n")
    g = entry("g.md", "g\n")
    m, _ = merge([f, g], [g], [g])
    assert names(m) == ["g.md"] and m.conflicts == []


def test_one_side_modified_takes_it() -> None:
    f = entry("a.md", "x\n")
    f2 = entry("a.md", "y\n")
    m, rec = merge([f], [f], [f2])
    assert m.files == [f2] and m.clean_names == ["a.md"] and rec.calls == []
    m, _ = merge([f], [f2], [f])
    assert m.files == [f2]


def test_identical_modification_and_identical_add() -> None:
    f = entry("a.md", "x\n")
    f2 = entry("a.md", "y\n")
    m, rec = merge([f], [f2], [dict(f2)])
    assert m.files == [f2] and m.clean_names == ["a.md"] and rec.calls == []
    m, _ = merge([], [f2], [dict(f2)])
    assert m.files == [f2] and m.conflicts == []


def test_modify_modify_nonoverlapping_merges_and_stores_once() -> None:
    base = entry("a.md", "1\n2\n3\n4\n5\n6\n7\n8\n")
    head = entry("a.md", "ONE\n2\n3\n4\n5\n6\n7\n8\n")
    mine = entry("a.md", "1\n2\n3\n4\n5\n6\n7\nEIGHT\n")
    m, rec = merge([base], [head], [mine])
    assert m.conflicts == []
    assert m.merged_names == ["a.md"] and m.clean_names == []
    assert len(rec.calls) == 1
    assert rec.calls[0][0] == "a.md"
    assert rec.calls[0][1]["text"] == "ONE\n2\n3\n4\n5\n6\n7\nEIGHT\n"
    assert m.files[0]["stored"] is True


def test_add_add_different_conflicts() -> None:
    h = entry("a.md", "h\n")
    mi = entry("a.md", "m\n")
    m, rec = merge([], [h], [mi])
    assert m.files == [] and rec.calls == []
    assert m.conflicts == [FileConflict("a.md", "add_add", None, h["sha256"], mi["sha256"], None)]


def test_delete_modify_conflicts_both_directions() -> None:
    b = entry("a.md", "x\n")
    c = entry("a.md", "y\n")
    m, _ = merge([b], [], [c])
    assert m.conflicts == [FileConflict("a.md", "delete_modify", b["sha256"], None, c["sha256"], None)]
    m, _ = merge([b], [c], [])
    assert m.conflicts == [FileConflict("a.md", "delete_modify", b["sha256"], c["sha256"], None, None)]
    assert m.files == []


def test_modify_modify_overlapping_gives_marker_text() -> None:
    b = entry("a.md", "x\n")
    h = entry("a.md", "head\n")
    mi = entry("a.md", "mine\n")
    m, rec = merge([b], [h], [mi])
    (c,) = m.conflicts
    assert c.kind == "modify_modify_text" and c.name == "a.md"
    assert c.merged_text == "<<<<<<< head\nhead\n=======\nmine\n>>>>>>> mine\n"
    assert (c.base_sha256, c.theirs_sha256, c.mine_sha256) == (b["sha256"], h["sha256"], mi["sha256"])
    assert m.files == [] and rec.calls == []


def test_case_collision() -> None:
    h = entry("Readme.md", "h\n")
    mi = entry("README.md", "m\n")
    m, rec = merge([], [h], [mi])
    assert sorted(c.name for c in m.conflicts) == ["README.md", "Readme.md"]
    assert {c.kind for c in m.conflicts} == {"case_collision"}
    assert m.files == [] and rec.calls == []


def test_oversize_text() -> None:
    b = entry("a.txt", "a\n")
    h = entry("a.txt", "h" * 50 + "\n")
    mi = entry("a.txt", "m" * 50 + "\n")
    m, rec = merge([b], [h], [mi], max_text_bytes=40)
    assert [c.kind for c in m.conflicts] == ["oversize"]
    assert m.conflicts[0].merged_text is None and rec.calls == []


def test_oversize_by_actual_length_when_size_bytes_lies() -> None:
    b = entry("a.txt", "x\n" * 30)
    h = entry("a.txt", "h\n" + "x\n" * 29, size_bytes=1)  # declared tiny, actually 60 bytes
    mi = entry("a.txt", "x\n" * 29 + "m\n", size_bytes=1)
    b["size_bytes"] = 1
    m, rec = merge([b], [h], [mi], max_text_bytes=40)
    assert [(c.name, c.kind) for c in m.conflicts] == [("a.txt", "oversize")]
    assert m.files == [] and rec.calls == []


@pytest.mark.parametrize("side", [0, 1, 2])
def test_duplicate_names_within_a_manifest_are_refused(side) -> None:
    a1, a2 = entry("a.md", "1\n"), entry("a.md", "2\n")
    sides = [[a1], [a1], [a1]]
    sides[side] = [a1, a2]
    with pytest.raises(ValueError, match="duplicate file name"):
        merge(*sides)


def test_binary_by_media_type_and_name() -> None:
    b = entry("img.png", b"\x89a", media_type="image/png")
    h = entry("img.png", b"\x89b", media_type="image/png")
    mi = entry("img.png", b"\x89c", media_type="image/png")
    m, _ = merge([b], [h], [mi])
    assert [c.kind for c in m.conflicts] == ["binary"]
    # unknown extension and no media type: binary
    b, h, mi = (entry("blob.bin", x) for x in ("a", "b", "c"))
    m, _ = merge([b], [h], [mi])
    assert [c.kind for c in m.conflicts] == ["binary"]


def test_text_like_media_types_and_extensions_are_merged() -> None:
    for name, mt in [("x.dat", "text/plain"), ("x.dat", "application/json; charset=utf-8"),
                     ("x.dat", "application/yaml"), ("x.dat", "application/xml"),
                     ("x.dat", "application/javascript"), ("x.svg", None), ("x.ts", None), ("x.csv", None)]:
        extra = {"media_type": mt} if mt else {}
        b = entry(name, "1\n2\n3\n4\n5\n6\n7\n", **extra)
        h = entry(name, "H\n2\n3\n4\n5\n6\n7\n", **extra)
        mi = entry(name, "1\n2\n3\n4\n5\n6\nM\n", **extra)
        m, rec = merge([b], [h], [mi])
        assert m.conflicts == [], (name, mt)
        assert len(rec.calls) == 1


def test_not_utf8() -> None:
    b = entry("a.txt", "a\n")
    h = entry("a.txt", b"\xff\xfe h\n")
    mi = entry("a.txt", "m\n")
    m, rec = merge([b], [h], [mi])
    assert [c.kind for c in m.conflicts] == ["not_utf8"]
    assert rec.calls == []


def test_ordering_is_sorted_by_name() -> None:
    fs = [entry(n, "x\n") for n in ("z.md", "a.md", "m.md")]
    m, _ = merge([], fs, [])
    assert names(m) == ["a.md", "m.md", "z.md"]
    assert m.clean_names == ["a.md", "m.md", "z.md"]


def test_store_never_called_when_any_conflict() -> None:
    base = entry("ok.md", "1\n2\n3\n4\n5\n6\n7\n8\n")
    head = entry("ok.md", "H\n2\n3\n4\n5\n6\n7\n8\n")
    mine = entry("ok.md", "1\n2\n3\n4\n5\n6\n7\nM\n")
    m, rec = merge([base], [head, entry("z.md", "h\n")], [mine, entry("z.md", "m\n")])
    assert [c.kind for c in m.conflicts] == ["add_add"]
    assert m.files == [] and m.merged_names == [] and rec.calls == []
