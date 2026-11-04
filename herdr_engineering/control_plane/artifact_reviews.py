"""Small immutable-version review state machine."""
from __future__ import annotations

import secrets
import time


class ReviewError(ValueError):
    pass


class ArtifactReviewManager:
    def __init__(self, *, now=time.time, on_change=None):
        self.now = now
        self.on_change = on_change
        self._reviews = {}

    def request(self, *, artifact_id: str, version_id: str, requested_by: str) -> dict:
        if not artifact_id or not version_id or not requested_by:
            raise ReviewError("artifact, version, and requester are required")
        review_id = "review_" + secrets.token_hex(12)
        review = {"review_id": review_id, "artifact_id": artifact_id,
                  "version_id": version_id, "requested_by": requested_by,
                  "reviewer": None, "state": "requested", "comments": [],
                  "created_at": self.now(), "completed_at": None}
        self._reviews[review_id] = review
        if self.on_change:
            self.on_change("request", dict(review))
        return dict(review)

    def complete(self, review_id: str, *, reviewer: str, decision: str,
                 comments: list[str]) -> dict:
        review = self._reviews.get(review_id)
        if review is None:
            raise ReviewError("review not found")
        if review["state"] != "requested":
            raise ReviewError("review is terminal")
        if decision not in {"approved", "rejected"} or not reviewer:
            raise ReviewError("reviewer and valid decision are required")
        if not isinstance(comments, list) or not all(isinstance(item, str) for item in comments):
            raise ReviewError("comments must be a list of strings")
        review.update(reviewer=reviewer, state=decision, comments=list(comments),
                      completed_at=self.now())
        if self.on_change:
            self.on_change("complete", dict(review))
        return dict(review)

    def get(self, review_id: str) -> dict | None:
        review = self._reviews.get(review_id)
        return dict(review) if review else None

    def list_for_artifact(self, artifact_id: str) -> list[dict]:
        return [dict(review) for review in self._reviews.values()
                if review["artifact_id"] == artifact_id]

    def load(self, reviews: list[dict]) -> None:
        for review in reviews:
            self._reviews[review["review_id"]] = dict(review)

