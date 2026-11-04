import threading
import urllib.error
import urllib.request

from herdr_engineering.control_plane.publication import FakeExposureProvider, PublicationController
from herdr_engineering.public_gateway import PublicArtifactGateway
from herdr_engineering.public_gateway_server import PublicGatewayServer


def test_gateway_server_delivers_approved_pinned_artifact_and_revoke_denies():
    controller = PublicationController(provider=FakeExposureProvider())
    pending = controller.request(resource_id="artifact_1", version_id="version_3",
                                 requested_by="user:1", audience="unlisted_link",
                                 expires_at=9999999999)
    active = controller.approve(pending["publication_id"], approver="user:1")
    gateway = PublicArtifactGateway(
        controller,
        resolver=lambda resource, version: (b"published", "text/plain")
        if (resource, version) == ("artifact_1", "version_3") else None,
    )
    server = PublicGatewayServer(gateway, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/s/{active['token']}/index.txt"
        with urllib.request.urlopen(url) as response:
            assert response.read() == b"published"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        controller.revoke(active["publication_id"], actor="user:1")
        try:
            urllib.request.urlopen(url)
            raise AssertionError("revoked publication unexpectedly delivered")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_server_does_not_expose_control_plane_paths():
    controller = PublicationController(provider=FakeExposureProvider())
    gateway = PublicArtifactGateway(controller, resolver=lambda *_: None)
    server = PublicGatewayServer(gateway, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/v1/health")
            raise AssertionError("control-plane path unexpectedly exposed")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_health_is_liveness_only():
    controller = PublicationController(provider=FakeExposureProvider())
    server = PublicGatewayServer(PublicArtifactGateway(controller, resolver=lambda *_: None),
                                 host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz") as response:
            assert response.status == 200
            assert response.read() == b'{"status":"ok"}'
    finally:
        server.shutdown()
        server.server_close()

