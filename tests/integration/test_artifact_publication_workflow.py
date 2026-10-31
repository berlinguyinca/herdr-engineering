import threading
import urllib.error
import urllib.request

from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_renderers import RendererRegistry
from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager
from herdr_engineering.control_plane.publication import FakeExposureProvider, PublicationController
from herdr_engineering.public_gateway import PublicArtifactGateway
from herdr_engineering.public_gateway_server import PublicGatewayServer


def test_create_render_review_publish_deliver_revoke_workflow():
    versions = {}
    jobs = ArtifactJobManager()
    renderer = RendererRegistry()

    def commit(**kwargs):
        version_id = f"version_{len(versions) + 1}"
        versions[version_id] = {"artifact_id": kwargs["artifact_id"],
                                "source": kwargs["source"],
                                "outputs": kwargs["outputs"],
                                "host_id": kwargs["host_id"],
                                "agent_id": kwargs["requested_by"]}
        return version_id

    job = jobs.submit(profile="web", artifact_id="artifact_1", base_version_id=None,
                      request={"source": "<html><body>release</body></html>"},
                      requested_by="agent:host-a")
    version = jobs.execute(job["job_id"], host_id="host-a", renderer=renderer,
                           commit=commit)
    version_id = version["result_version_id"]

    reviews = ArtifactReviewManager()
    review = reviews.request(artifact_id="artifact_1", version_id=version_id,
                             requested_by="agent:host-a")
    reviewed = reviews.complete(review["review_id"], reviewer="human:1",
                                decision="approved", comments=["validated on host-c"])
    assert reviewed["state"] == "approved"

    controller = PublicationController(provider=FakeExposureProvider())
    requested = controller.request(resource_id="artifact_1", version_id=version_id,
                                   requested_by="agent:host-a", audience="unlisted_link",
                                   expires_at=9999999999)
    publication = controller.approve(requested["publication_id"], approver="human:1")
    gateway = PublicArtifactGateway(
        controller,
        resolver=lambda resource_id, pinned_version: (
            versions[pinned_version]["outputs"][0]["content"].encode(), "text/html")
        if resource_id == "artifact_1" and pinned_version == version_id else None,
    )
    delivered = gateway.request(publication["token"], method="GET", path="/")
    assert delivered.status == 200
    assert b"release" in delivered.body
    server = PublicGatewayServer(gateway, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/s/{publication['token']}/index.html"
    try:
        with urllib.request.urlopen(url) as response:
            assert response.read() == delivered.body
        controller.revoke(publication["publication_id"], actor="human:1")
        try:
            urllib.request.urlopen(url)
            raise AssertionError("revoked public URL unexpectedly delivered")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()

