"""Lab annotations: edge checks (origin, CSRF, content type, framing, write cookie), writes to the database, caps."""

from __future__ import annotations

import json
import re

from herdr_artifact_host.share_session import csrf_token, write_cookie_name, write_cookie_value
from tests.artifact_host.seed import PUBLIC_URL, SECRET, Host, Seeded, access, ident
from tests.artifact_host.test_public_lab import lab, landing_session, sign_in


class Session:
    def __init__(
        self, host: Host, share: Seeded, user: str = "alice", email: str = "Alice@Lab.example"
    ) -> None:
        self.host, self.share = host, share
        self.sess = landing_session(sign_in(host, share, access(user), ident(user, email=email)))
        self.base = f"/s/{share.token}/k/{self.sess}/annotations"
        self.cookie = f"{write_cookie_name(share.share_id, self.sess)}={write_cookie_value(SECRET, share.share_id, self.sess)}"

    def headers(self, **over: str | None) -> dict[str, str]:
        h: dict[str, str] = {
            "Origin": PUBLIC_URL,
            "X-Herdr-Annotate": csrf_token(SECRET, self.share.share_id, self.sess),
            "Content-Type": "application/json",
            "Cookie": self.cookie,
        }
        for k, v in over.items():
            k = k.replace("_", "-")
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
        return h

    def post(self, path: str, doc: object, **over: str | None):
        body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        return self.host.request("POST", path, self.headers(**over), body)

    def create(self, comment: str = "Looks wrong", file: str = "index.html"):
        return self.post(
            self.base, {"anchor": {"kind": "document", "file": file}, "comment": comment}
        )


def rows(host: Host, share: Seeded) -> list[dict]:
    return host.rows(
        "SELECT annotation_id, file_name, body, status, author_identity, author_email, author_role, edited_at, "
        "withdrawn_at FROM annotations WHERE share_id=$1 ORDER BY created_at",
        share.share_id,
    )


def test_create_list_edit_withdraw(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    r = a.create()
    assert r.status == 201
    ann = r.json()["annotation"]
    assert (
        re.fullmatch(r"ann_[A-Za-z0-9_-]{22}", ann["annotation_id"])
        and ann["body"] == "Looks wrong"
    )
    [row] = rows(host, s)
    assert row["author_identity"] == "alice" and row["author_email"] == "alice@lab.example"
    assert (
        row["author_role"] == "member"
        and row["status"] == "open"
        and row["file_name"] == "index.html"
    )
    listed = host.get(a.base, {"Cookie": a.cookie})
    assert listed.status == 200 and [x["annotation_id"] for x in listed.json()["annotations"]] == [
        ann["annotation_id"]
    ]
    e = a.post(f"{a.base}/{ann['annotation_id']}/edit", {"comment": "Changed"})
    assert e.status == 202
    assert rows(host, s)[0]["body"] == "Changed" and rows(host, s)[0]["edited_at"] is not None
    w = a.post(f"{a.base}/{ann['annotation_id']}/delete", {})
    assert w.status == 202 and rows(host, s)[0]["status"] == "withdrawn"
    assert a.post(f"{a.base}/{ann['annotation_id']}/edit", {"comment": "again"}).status == 409
    kinds = [e["kind"] for e in host.events(s.share_id)]
    assert {"annotated", "annotation_edited", "annotation_withdrawn"} <= set(kinds)


def test_owner_email_gets_owner_role(migrated_dsn: str, tmp_path) -> None:
    from tests.artifact_host.seed import running_host

    with running_host(
        migrated_dsn, tmp_path, {"owner_emails": frozenset({"alice@lab.example"})}
    ) as h:
        s = lab(h)
        assert Session(h, s).create().status == 201
        assert rows(h, s)[0]["author_role"] == "owner"


def test_get_needs_write_cookie(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    assert host.get(a.base).status == 404
    assert host.get(a.base, {"Cookie": "hw_0000000000000000=bad"}).status == 404


def test_only_own_annotations_listed(host: Host) -> None:
    s = lab(host, emails=("alice@lab.example", "bob@lab.example"))
    a, b = Session(host, s), Session(host, s, "bob", "bob@lab.example")
    a.create("alice's")
    assert host.get(b.base, {"Cookie": b.cookie}).json() == {"annotations": []}
    aid = rows(host, s)[0]["annotation_id"]
    ghost = "ann_" + "A" * 22
    hijack = b.post(f"{b.base}/{aid}/edit", {"comment": "hijack"})
    assert (
        hijack.status == 409
        and hijack.shape()[2] == b.post(f"{b.base}/{ghost}/edit", {"comment": "x"}).shape()[2]
    )
    assert rows(host, s)[0]["body"] == "alice's"


def test_edge_refusals_are_uniform_404_and_write_nothing(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    ref = host.get(f"/s/{s.token}/nothing").shape()
    bad = [
        a.create.__func__
        and a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            Origin=None,
        ),
        a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            Origin="https://evil.example",
        ),
        a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            X_Herdr_Annotate=None,
        ),
        a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            X_Herdr_Annotate="a" * 43,
        ),
        a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            Content_Type="text/plain",
        ),
        a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            Content_Type="application/json; charset=utf-8",
        ),
        a.post(
            a.base,
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"},
            Cookie=None,
        ),
    ]
    for r in bad:
        assert r.shape()[0] == 404 and r.body == b"Not found\n"
    assert r.shape()[2] == ref[2]
    assert rows(host, s) == []


def test_oversize_and_chunked_refused(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    assert a.post(a.base, b"x" * 40000).status == 404
    h = a.headers()
    h["Transfer-Encoding"] = "chunked"
    assert host.request("POST", a.base, h, b"0\r\n\r\n").status == 404
    assert rows(host, s) == []


def test_input_errors_are_json_400(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    r = a.post(
        a.base, {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x", "extra": 1}
    )
    assert r.status == 400 and r.json() == {"error": "bad_anchor"}
    r = a.create(file="ghost.html")
    assert r.status == 400 and r.json() == {"error": "bad_file"}
    assert a.post(a.base, b"{not json").status == 400
    assert rows(host, s) == []
    assert any(
        e["detail"].find("annotate_bad_anchor") != -1 for e in host.events(s.share_id, "denied")
    )


def test_removed_from_allow_list_loses_write_access(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    assert a.create().status == 201
    host.execute("UPDATE shares SET allow_emails='{}' WHERE share_id=$1", s.share_id)
    assert a.create().status == 404
    assert len(rows(host, s)) == 1


def test_revoked_share_takes_no_writes(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    host.execute("UPDATE shares SET state='revoked' WHERE share_id=$1", s.share_id)
    assert a.create().status == 404
    assert rows(host, s) == []


def test_caps_return_quota(migrated_dsn: str, tmp_path) -> None:
    from herdr_artifact_host.annotation_anchor import annotation_settings
    from tests.artifact_host.seed import running_host

    limits = annotation_settings({"max_per_share": 2})
    with running_host(migrated_dsn, tmp_path, {"annotations": limits}) as h:
        s = lab(h)
        a = Session(h, s)
        assert a.create("1").status == 201 and a.create("2").status == 201
        r = a.create("3")
        assert r.status == 429 and r.json() == {"error": "quota"}
        assert len(rows(h, s)) == 2


def test_rate_limit(migrated_dsn: str, tmp_path) -> None:
    from herdr_artifact_host.annotation_anchor import annotation_settings
    from tests.artifact_host.seed import running_host

    limits = annotation_settings({"per_user_per_minute": 1})
    with running_host(migrated_dsn, tmp_path, {"annotations": limits}) as h:
        s = lab(h)
        a = Session(h, s)
        assert a.create("1").status == 201
        assert a.create("2").status == 429


def test_disabled_annotations(migrated_dsn: str, tmp_path) -> None:
    from herdr_artifact_host.annotation_anchor import annotation_settings
    from tests.artifact_host.seed import running_host

    with running_host(
        migrated_dsn, tmp_path, {"annotations": annotation_settings({"enabled": False})}
    ) as h:
        s = lab(h)
        a = Session(h, s)
        assert a.create().status == 404
        assert h.get(f"/s/{s.token}/k/{a.sess}/a/notes.txt").status == 404
        assert h.get(f"/s/{s.token}/k/{a.sess}/f/notes.txt").status == 200
