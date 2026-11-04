import pytest

from herdr_engineering.live_service_publication import (
    LiveServicePolicy,
    LiveServicePublicationError,
)


def test_live_service_policy_requires_registered_service_id_and_exact_path():
    policy = LiveServicePolicy(service_id="service_1", path_prefix="/preview",
                               methods={"GET", "HEAD"})
    assert policy.allows(method="GET", path="/preview/index.html") is True
    assert policy.allows(method="POST", path="/preview/index.html") is False
    assert policy.allows(method="GET", path="/other") is False


def test_live_service_policy_rejects_arbitrary_origins_and_path_escape():
    with pytest.raises(LiveServicePublicationError, match="service_id"):
        LiveServicePolicy(service_id="", path_prefix="/", methods={"GET"})
    with pytest.raises(LiveServicePublicationError, match="private origin"):
        LiveServicePolicy(service_id="service_1", path_prefix="/", methods={"GET"},
                          origin_url="http://127.0.0.1:8080")
    policy = LiveServicePolicy(service_id="service_1", path_prefix="/preview",
                               methods={"GET"})
    with pytest.raises(LiveServicePublicationError, match="unsafe"):
        policy.allows(method="GET", path="/preview/../secret")

