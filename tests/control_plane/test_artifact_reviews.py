import pytest

from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager, ReviewError


def test_review_is_pinned_to_immutable_version_and_requires_reviewer():
    reviews = ArtifactReviewManager()
    review = reviews.request(artifact_id="artifact_1", version_id="version_2",
                             requested_by="agent:1")
    assert review["state"] == "requested"
    completed = reviews.complete(review["review_id"], reviewer="user:1",
                                 decision="approved", comments=["looks good"])
    assert completed["state"] == "approved"
    assert completed["version_id"] == "version_2"


def test_rejected_review_is_terminal_and_cannot_be_reopened():
    reviews = ArtifactReviewManager()
    review = reviews.request(artifact_id="artifact_1", version_id="version_2",
                             requested_by="agent:1")
    rejected = reviews.complete(review["review_id"], reviewer="user:1",
                                decision="rejected", comments=["unsafe"])
    assert rejected["state"] == "rejected"
    with pytest.raises(ReviewError, match="terminal"):
        reviews.complete(review["review_id"], reviewer="user:2",
                         decision="approved", comments=[])
