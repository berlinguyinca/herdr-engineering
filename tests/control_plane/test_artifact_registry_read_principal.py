"""Sibling read routes need a principal check when a registry store exists but no in-memory ACL (real Postgres + HTTP).

An unauthorised caller gets the same plain 404 as for an id that does not exist; the creator still reads."""
import json
import time
import urllib.request

from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager
from herdr_engineering.control_plane.publication import PrivateOnlyExposureProvider, PublicationController
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.test_artifact_delete_api import CREATOR, api
from tests.control_plane.test_artifact_delete_round2 import status_of

registry_pg = _registry_pg_module.registry_pg
ROOT = "/api/v1/artifacts"
HTML = b"<!doctype html><html><body><p>private words</p></body></html>"


def upload(env):
    req = urllib.request.Request(env.base + f"{ROOT}?filename=p.html&mime=text/html", data=HTML, method="POST",
                                 headers={"X-API-Key": "creator-key", "Content-Type": "text/html"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["artifact_id"]


def wired(pg):
    jobs, reviews = ArtifactJobManager(), ArtifactReviewManager()
    publications = PublicationController(provider=PrivateOnlyExposureProvider())
    return api(pg, extra={"artifact_job_manager": jobs, "artifact_review_manager": reviews,
                          "publication_controller": publications}), jobs, reviews, publications


def test_sibling_reads_without_an_acl_answer_the_same_404_as_a_missing_id(registry_pg):
    ctx, jobs, reviews, publications = wired(registry_pg)
    with ctx as env:
        aid = upload(env)
        job = jobs.submit(profile="web", artifact_id=aid, base_version_id=None, request={"x": 1}, requested_by=CREATOR)
        env.adapter("record_job_event", "created", job)
        review = reviews.request(artifact_id=aid, version_id="v1", requested_by=CREATOR)
        pub = publications.request(resource_id=aid, version_id="v1", requested_by=CREATOR, audience="unlisted_link",
                                   expires_at=time.time() + 3600)
        stranger = "member-key"
        routes = [f"{ROOT}/{aid}/download", f"{ROOT}/{aid}/render", f"{ROOT}/{aid}/live", f"{ROOT}/{aid}/reviews",
                  f"/api/v1/artifact-jobs/{job['job_id']}", f"/api/v1/artifact-reviews/{review['review_id']}",
                  f"/api/v1/publications/{pub['publication_id']}"]
        missing = [f"{ROOT}/art-nope/download", f"{ROOT}/art-nope/render", f"{ROOT}/art-nope/live",
                   f"{ROOT}/art-nope/reviews", "/api/v1/artifact-jobs/job_nope", "/api/v1/artifact-reviews/review_nope",
                   "/api/v1/publications/publication_nope"]
        for path, absent in zip(routes, missing):
            assert status_of(env, path)[0] == 200, path                          # the creator still reads
            got = status_of(env, path, key=stranger)
            assert got[0] == 404 and b"private words" not in got[1], path
            assert got[1] == status_of(env, absent, key=stranger)[1], path       # same body as a missing id
        listed = status_of(env, "/api/v1/publications", key=stranger)
        assert listed[0] == 200 and pub["publication_id"] not in listed[1].decode()
        assert pub["publication_id"] in status_of(env, "/api/v1/publications")[1].decode()


def test_reviews_list_of_an_event_projection_only_upload_works_when_the_acl_authorises_it(registry_pg):
    """B: the registry gate must not apply when an ACL decided. A legacy upload has no registry row, so
    ``report_permission`` would refuse the creator; with the ACL present the list must still answer."""
    reviews = ArtifactReviewManager()
    ctx = api(registry_pg, extra={"artifact_acl": ArtifactAcl(), "artifact_review_manager": reviews})
    with ctx as env:
        aid = "legacy-projection-only"
        env.server.repo.register_artifact(session_id=None, artifact_id=aid, created_by=CREATOR, mission_id=None,
                                          original_filename="old.txt", mime_type="text/plain", size_bytes=3,
                                          sha256="", storage_key="", data=b"old")
        env.server.artifact_acl.grant(aid, CREATOR, {"read", "admin"})
        assert env.adapter("report_permission", aid, CREATOR, "read") is False      # no registry row, as claimed
        reviews.request(artifact_id=aid, version_id="v1", requested_by=CREATOR)
        got, body = status_of(env, f"{ROOT}/{aid}/reviews")
        assert got == 200 and len(json.loads(body)["reviews"]) == 1
        assert status_of(env, f"{ROOT}/{aid}/reviews", key="member-key")[0] == 404


def test_the_artifact_event_stream_needs_the_same_principal_check(registry_pg):
    ctx, *_ = wired(registry_pg)
    with ctx as env:
        aid = upload(env)
        mine = status_of(env, f"/api/v1/events?entity_type=artifact&entity_id={aid}")
        assert mine[0] == 200 and "ArtifactRegistered" in mine[1].decode()
        for suffix in ("", "&limit=10"):
            theirs = status_of(env, f"/api/v1/events?entity_type=artifact&entity_id={aid}{suffix}", key="member-key")
            absent = status_of(env, f"/api/v1/events?entity_type=artifact&entity_id=art-nope{suffix}", key="member-key")
            assert theirs[0] == absent[0] == 200 and "ArtifactRegistered" not in theirs[1].decode()
            assert json.loads(theirs[1])["events"] == json.loads(absent[1])["events"] == []
