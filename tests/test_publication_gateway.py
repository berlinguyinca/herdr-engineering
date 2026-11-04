import time

from herdr_engineering.control_plane.publication import (
    FakeExposureProvider,
    PrivateOnlyExposureProvider,
    PublicationController,
    PublicationError,
)
from herdr_engineering.public_gateway import PublicArtifactGateway


def _controller(now=None):
    provider = FakeExposureProvider()
    controller = PublicationController(provider=provider, now=now or time.time)
    return controller, provider


def test_agent_request_stays_pending_until_human_approval():
    controller, provider = _controller()
    request = controller.request(
        resource_id="artifact_1", version_id="version_1", requested_by="agent:1",
        audience="unlisted_link", expires_at=time.time() + 3600)
    assert request["state"] == "requested"
    assert provider.calls == []
    approved = controller.approve(request["publication_id"], approver="user:1")
    assert approved["state"] == "active"
    assert approved["approved_by"] == "user:1"
    assert provider.calls[-1][0] == "expose"


def test_revoke_prevents_future_delivery_even_if_provider_delete_fails():
    controller, provider = _controller()
    request = controller.request(
        resource_id="artifact_1", version_id="version_1", requested_by="user:1",
        audience="unlisted_link", expires_at=time.time() + 3600)
    active = controller.approve(request["publication_id"], approver="user:1")
    provider.fail_revoke = True
    revoked = controller.revoke(active["publication_id"], actor="user:1")
    assert revoked["state"] == "revoked"
    assert controller.is_deliverable(active["publication_id"]) is False


def test_gateway_requires_opaque_token_and_only_serves_pinned_version():
    controller, _ = _controller()
    request = controller.request(
        resource_id="artifact_1", version_id="version_7", requested_by="user:1",
        audience="unlisted_link", expires_at=time.time() + 3600)
    active = controller.approve(request["publication_id"], approver="user:1")
    token = active["token"]
    gateway = PublicArtifactGateway(controller, resolver=lambda resource, version: (
        b"hello", "text/plain") if (resource, version) == ("artifact_1", "version_7") else None)
    response = gateway.request(token, method="GET", path="/")
    assert response.status == 200
    assert response.body == b"hello"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert gateway.request(token, method="POST", path="/").status == 405
    assert gateway.request("wrong-token", method="GET", path="/").status == 404


def test_expired_publication_is_denied_without_calling_resolver():
    clock = [100.0]
    controller, _ = _controller(now=lambda: clock[0])
    request = controller.request(
        resource_id="artifact_1", version_id="version_1", requested_by="user:1",
        audience="unlisted_link", expires_at=101.0)
    active = controller.approve(request["publication_id"], approver="user:1")
    called = []
    gateway = PublicArtifactGateway(controller,
                                    resolver=lambda *_: called.append(True))
    clock[0] = 102.0
    assert gateway.request(active["token"], method="GET", path="/").status == 410
    assert called == []


def test_authenticated_guest_publication_requires_allowlisted_guest_identity():
    controller, _ = _controller()
    request = controller.request(
        resource_id="artifact_1", version_id="version_1", requested_by="user:1",
        audience="authenticated_guest", guest_principals=["guest:1"],
        expires_at=time.time() + 3600)
    active = controller.approve(request["publication_id"], approver="user:1")
    gateway = PublicArtifactGateway(controller,
                                    resolver=lambda *_: (b"guest", "text/plain"))
    assert gateway.request(active["token"], method="GET", path="/").status == 401
    assert gateway.request(active["token"], method="GET", path="/", guest_id="guest:2").status == 403
    assert gateway.request(active["token"], method="GET", path="/", guest_id="guest:1").status == 200


def test_private_only_provider_keeps_approval_disabled_and_records_failure():
    events = []
    controller = PublicationController(provider=PrivateOnlyExposureProvider(),
                                       on_change=lambda action, record: events.append(action))
    request = controller.request(resource_id="artifact_1", version_id="version_1",
                                 requested_by="agent:1", audience="unlisted_link",
                                 expires_at=time.time() + 3600)
    try:
        controller.approve(request["publication_id"], approver="human:1")
        raise AssertionError("private-only provider unexpectedly exposed")
    except PublicationError:
        pass
    assert controller.get(request["publication_id"])["state"] == "failed"
    assert events == ["requested", "failed"]


def test_publication_records_operator_selected_local_gateway_target():
    controller, _ = _controller()
    request = controller.request(resource_id="artifact_1", version_id="version_1",
                                 requested_by="agent:1", audience="unlisted_link",
                                 expires_at=time.time() + 3600)
    assert request["local_target"] is None
    controller = PublicationController(provider=FakeExposureProvider(),
                                       default_local_target="http://127.0.0.1:8085")
    request = controller.request(resource_id="artifact_1", version_id="version_1",
                                 requested_by="agent:1", audience="unlisted_link",
                                 expires_at=time.time() + 3600)
    assert request["local_target"] == "http://127.0.0.1:8085"

