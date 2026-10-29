"""Spec 10.5 at the control-plane level: an emailed plot becomes a permission-scoped, versioned artifact linked to its
conversation, to a candidate issue only after a person confirmed it, and never into a wider audience. Fake Gmail data;
no network."""
import asyncio
import base64
import hashlib

import pytest

from herdr_engineering.control_plane import intel_gmail as gm
from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_scope import install_gate
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.intel_attachment_artifact import (
    attach_to_artifact,
    graph_ref,
    retract_attachment,
    thread_node_id,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_artifact_scope import ACL_OWNER, ACL_READER, OWNER, READER, STRANGER
from tests.control_plane.test_graph import OPERATOR, Feed, _AnyKey

PNG = b"\x89PNG\r\n\x1a\n" + b"plot-bytes" * 8
PNG2 = b"\x89PNG\r\n\x1a\n" + b"corrected!" * 8
ISSUE = "issue:github.com/o/r#9"
ON = pi.config({"enabled": True, "ingest": True, "derive": True, "suggest": True})


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mail(mid="m1", filename="Fig 1.png", body="see https://github.com/o/r/issues/9"):
    return {"id": mid, "threadId": "t-77", "internalDate": "1790000000000", "labelIds": ["INBOX"],
            "payload": {"mimeType": "multipart/mixed",
                        "headers": [{"name": "From", "value": "Ann <ann@lab.example>"},
                                    {"name": "Subject", "value": "binbase plot"},
                                    {"name": "Message-ID", "value": "<abc@lab.example>"}],
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64(body.encode())}},
                                  {"mimeType": "image/png", "filename": filename,
                                   "body": {"attachmentId": "att1", "size": len(PNG)}}]}}


class _RustFS:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self.objects[key] = data
        return key, digest


class _Store:
    """In-memory ArtifactStore surface used by the module: register_logical, commit_version, _rf."""

    def __init__(self):
        self._rf = _RustFS()
        self.artifacts, self.versions = {}, {}

    async def register_logical(self, **kw):
        aid = f"artifact_{len(self.artifacts) + 1}"
        self.artifacts[aid] = {**kw, "artifact_id": aid, "head": None}
        return {"artifact_id": aid, **kw}

    async def commit_version(self, artifact_id, **kw):
        art = self.artifacts[artifact_id]
        assert kw["base_version_id"] == art["head"], "compare-and-swap on the head"
        n = len([v for v in self.versions.values() if v["artifact_id"] == artifact_id]) + 1
        version = {"artifact_id": artifact_id, "version_id": f"{artifact_id}_v{n}", "version_number": n,
                   "parent_version_id": kw["base_version_id"], "output_manifest": kw["output_manifest"],
                   "provenance": kw["provenance"], "created_by": kw["created_by"]}
        self.versions[version["version_id"]] = version
        art["head"] = version["version_id"]
        return version


class World:
    def __init__(self):
        self.repo = ControlPlaneRepo()
        self.svc = IngestService(self.repo, key_scopes=_AnyKey())
        self.acl = ArtifactAcl()
        self.gate = install_gate(self.repo.graph, self.acl)
        self.store = _Store()
        self.intel = pi.IntelligenceStore()
        self.viewer = pi.Viewer("o", None, "restricted")
        self.feed = Feed(self.svc)

    def attach(self, msg=None, data=PNG, **kw):
        return asyncio.run(attach_to_artifact(
            store=self.store, acl=self.acl, ingest=self.svc, principal=OWNER, msg=msg or mail(), part_path="1.2",
            data=data, account="gw@lab.example", owner=ACL_OWNER, intelligence=self.intel, viewer=self.viewer, **kw))

    def sees(self, who, node):
        try:
            return bool(self.repo.graph.get_node(who, node))
        except KeyError:
            return False

    def propose_link(self, msg, accept=None):
        rel = self.intel.add_relationship(gm.explicit_links(msg, org_id="o", project_ids=("p",), account="gw@lab.example")[0],
                                          cfg=ON, actor="importer")
        if accept is not None:
            rel = self.intel.review_relationship(rel.id, accept=accept, actor="ann", expected_version=rel.version,
                                                 viewer=self.viewer)
        return rel


def test_accepted_attachment_becomes_a_versioned_artifact_with_original_provenance():
    w = World()
    out = w.attach()
    assert out["verdict"] == "accept" and out["version_id"].endswith("_v1")
    version = w.store.versions[out["version_id"]]
    prov = version["provenance"]
    assert (prov["message_id"], prov["thread_id"], prov["from"], prov["subject"]) == ("m1", "t-77", "ann@lab.example",
                                                                                    "binbase plot")
    assert prov["original_filename"] == "Fig 1.png" and prov["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert prov["sniffed_type"] == "image/png" and prov["message_id_header"] == "<abc@lab.example>"
    assert version["output_manifest"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert w.store.artifacts[out["artifact_id"]]["artifact_type"] == "email_attachment"


def test_permission_scope_is_the_mail_audience_and_nothing_wider():
    w = World()
    out = w.attach(audience=(ACL_READER,))
    aid = out["artifact_id"]
    assert out["audience"] == [ACL_OWNER, ACL_READER]
    assert w.acl.can(aid, ACL_READER, "read") and not w.acl.can(aid, ACL_READER, "edit")
    assert not w.acl.can(aid, ACL_READER, "share")
    assert not w.acl.can(aid, "api-key:cccccccccccc3333:agent", "read")
    node = f"artifact:{aid}"
    assert w.sees(OWNER, node) and w.sees(READER, node)
    # a bystander key, an operator and a room member who was not on the mail see nothing
    member = {"kind": "member", "id": "user:bob", "authenticated_by": "collab",
              "access": {"rooms": frozenset({"room-1"}), "room_ws": frozenset(), "room_missions": frozenset()}}
    assert not w.sees(STRANGER, node) and not w.sees(OPERATOR, node) and not w.sees(member, node)
    assert not w.sees(STRANGER, f"{node}@{out['version_id']}")


def test_graph_holds_ids_and_digests_not_mail_content():
    w = World()
    out = w.attach()
    import json
    blob = json.dumps(w.repo.graph.snapshot())
    for secret in ("ann@lab.example", "binbase plot", "Fig 1.png", "abc@lab.example", "gw@lab.example"):
        assert secret not in blob
    assert hashlib.sha256(PNG).hexdigest() in blob and out["artifact_id"] in blob


def test_artifact_is_linked_to_its_source_conversation_node():
    w = World()
    out = w.attach()
    thread = thread_node_id("gw@lab.example", "t-77")
    assert out["thread_node"] == thread and "gw@lab.example" not in thread
    edge = w.repo.graph.get_edge(OWNER, "produced", thread, f"artifact:{out['artifact_id']}")["edges"]
    assert len(edge) == 1
    # the conversation node is private to its writer: the audience gets the artifact, not the thread
    assert not w.sees(READER, thread)


def test_no_issue_link_for_an_inferred_provisional_or_rejected_relationship():
    w = World()
    msg = mail()
    rel = w.propose_link(msg)                             # inferred, provisional (an email can never self-confirm)
    assert rel.review_state == "provisional"
    assert w.attach(msg)["issue_links"] == []
    w2 = World()
    w2.propose_link(msg, accept=False)
    out = w2.attach(msg)
    assert out["issue_links"] == []
    with pytest.raises(KeyError):
        w2.repo.graph.get_edge(OWNER, "issue_link", f"artifact:{out['artifact_id']}", ISSUE)


def test_issue_link_appears_only_after_a_person_confirmed_it():
    w = World()
    msg = mail()
    w.propose_link(msg, accept=True)
    out = w.attach(msg)
    assert out["issue_links"] == [ISSUE] and graph_ref("issue:o/r#9") == ISSUE
    edges = w.repo.graph.get_edge(OWNER, "issue_link", f"artifact:{out['artifact_id']}", ISSUE)["edges"]
    assert len(edges) == 1
    assert not w.sees(STRANGER, f"artifact:{out['artifact_id']}")


def test_a_confirmation_for_another_message_does_not_link_this_one():
    w = World()
    w.propose_link(mail("m-other"), accept=True)
    assert w.attach(mail("m1"))["issue_links"] == []


def test_active_content_is_never_stored_or_linked():
    w = World()
    bad = mail(filename="x.html")
    out = w.attach(bad, data=b"<html><script>x()</script></html>")
    assert out["verdict"] != "accept" and out["artifact_id"] is None and w.store.artifacts == {}
    assert not w.repo.graph.snapshot()["nodes"]


def test_corrected_attachment_is_a_new_version_and_retraction_keeps_history():
    w = World()
    first = w.attach(audience=(ACL_READER,))
    aid, v1 = first["artifact_id"], first["version_id"]
    second = w.attach(mail("m2", filename="Fig 1 fixed.png"), data=PNG2, audience=(ACL_READER,),
                      previous={"artifact_id": aid, "version_id": v1})
    v2 = second["version_id"]
    assert second["artifact_id"] == aid and v2.endswith("_v2")
    assert w.store.versions[v1]["output_manifest"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()   # untouched
    graph = w.repo.graph
    assert graph.get_node(READER, f"artifact:{aid}")["node"]["attrs"]["version_id"] == v2
    assert graph.get_edge(READER, "supersession", f"artifact:{aid}@{v2}", f"artifact:{aid}@{v1}")["edges"]
    ret = retract_attachment(ingest=w.svc, principal=OWNER, artifact_id=aid, version_id=v1,
                             reason="wrong plot attached")
    assert ret["error"] is None
    old = graph.get_node(READER, f"artifact:{aid}@{v1}")["node"]       # authorized readers still inspect it
    assert old["state"] == "retracted" and old["attrs"]["content_digests"] == [hashlib.sha256(PNG).hexdigest()]
    assert graph.get_node(READER, f"artifact:{aid}@{v2}")["node"]["state"] == "ready"
    assert w.store.versions[v1]["provenance"]["message_id"] == "m1"     # original provenance survives
    assert not w.sees(STRANGER, f"artifact:{aid}@{v1}")


def test_reattaching_the_same_mail_is_a_new_version_never_an_overwrite():
    w = World()
    first = w.attach()
    before = w.repo.graph.snapshot()
    again = asyncio.run(attach_to_artifact(
        store=w.store, acl=w.acl, ingest=w.svc, principal=OWNER, msg=mail(), part_path="1.2", data=PNG,
        account="gw@lab.example", owner=ACL_OWNER, previous={"artifact_id": first["artifact_id"],
                                                             "version_id": first["version_id"]}))
    assert again["version_id"] != first["version_id"]               # a new version, not a silent overwrite
    assert len(w.store.versions) == 2
    assert w.repo.graph.snapshot() != before                         # the head moved to the new version


@pytest.mark.parametrize("ref,expected", [("issue:o/r#9", ISSUE), ("pr:o/r#3", "pr:github.com/o/r#3"),
                                          ("email:m1", None), ("issue:../x#1", None)])
def test_ref_mapping(ref, expected):
    assert graph_ref(ref) == expected
