"""Metadata preview only: synthetic trusted bindings, no runtime enrollment or delivery."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from herdr_engineering.control_plane.activity_digest import (
    ActivityMetadata,
    DigestEnrollment,
    EventBinding,
    ProducerBinding,
    RevisionObservation,
    SessionBinding,
    preview_activity,
)


@pytest.fixture
def context():
    enrollment = DigestEnrollment(
        "canary",
        "repo:engineering",
        frozenset({"src/a.py", "tests/test_a.py"}),
        frozenset({"collector"}),
        enabled=True,
        configured_private_origin="http://100.104.39.6",
    )
    producer = ProducerBinding(
        "collector",
        "host:session-collector",
        "repo:engineering",
        frozenset({"file", "test", "revision"}),
        reports_test_completion=True,
    )
    session = SessionBinding(
        "herdr:bender:default:one%3Atwo", "epoch-one", "repo:engineering", "collector"
    )
    bindings = (
        EventBinding(
            1,
            "journal:file-a",
            "repo:engineering",
            session.canonical_session_id,
            "epoch-one",
            "collector",
        ),
        EventBinding(
            2,
            "journal:test-a",
            "repo:engineering",
            session.canonical_session_id,
            "epoch-one",
            "collector",
            completed_test_run_id="run-a",
        ),
    )
    return {
        "enrollment": enrollment,
        "session": session,
        "producers": (producer,),
        "event_bindings": bindings,
    }


def metadata():
    return (
        ActivityMetadata(1, 10, "file", path="src/a.py", operation="edit"),
        ActivityMetadata(
            2, 20, "test", run_id="run-a", status="passed", passed=4, failed=0, skipped=1
        ),
    )


def test_default_off_and_preview_only_even_when_payload_claims_engineering(context):
    assert preview_activity(metadata()).state == "not_enrolled"
    result = preview_activity(metadata(), **context)
    assert (
        result.state == "preview"
        and result.preview_only is True
        and result.delivery_enabled is False
    )
    assert "reported" in result.digest.basis and "independent" in result.digest.basis
    value = json.loads(result.canonical_text)
    assert value["files"][0]["path"] == "src/a.py"
    assert value["completed_tests"][0]["reported_counts"] == {
        "passed": 4,
        "failed": 0,
        "skipped": 1,
    }
    assert "run-a" not in result.canonical_text and "journal:file-a" not in result.canonical_text
    assert "#/herd/session/herdr%3Abender%3Adefault%3Aone%253Atwo" in value["private_session_link"]
    assert len(result.canonical_text.encode("utf-8")) <= 3072


def test_pure_preview_invokes_no_side_effect_dependency(context):
    with (
        patch("builtins.open", side_effect=AssertionError("filesystem")),
        patch("socket.socket", side_effect=AssertionError("network")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        assert preview_activity(metadata(), **context).state == "preview"


@pytest.mark.parametrize(
    "fault", ["no-producer", "wrong-repo", "wrong-context", "agent-label-only", "no-enrollment"]
)
def test_missing_external_binding_never_comes_from_payload(context, fault):
    if fault == "no-producer":
        context["producers"] = ()
    elif fault == "wrong-repo":
        context["session"] = replace(context["session"], canonical_repository_id="repo:other")
    elif fault == "wrong-context":
        context["event_bindings"] = tuple(
            replace(b, native_context_epoch="other") for b in context["event_bindings"]
        )
    elif fault == "agent-label-only":
        context["event_bindings"] = ()
    else:
        context["enrollment"] = replace(context["enrollment"], enabled=False)
    result = preview_activity(metadata(), **context)
    assert (
        result.digest is None and result.canonical_text is None and result.delivery_enabled is False
    )


@pytest.mark.parametrize(
    "path",
    [
        "../src/a.py",
        "src/../a.py",
        "/src/a.py",
        "//host/a.py",
        "C:\\src\\a.py",
        "https://host/a.py",
        "src//a.py",
        "src/./a.py",
        "src/a.py\x00",
        "src/%2e%2e/a.py",
        "src/unknown.py",
    ],
)
def test_only_exact_canonical_enrolled_paths_are_eligible(context, path):
    result = preview_activity((replace(metadata()[0], path=path),), **context)
    assert result.state == "empty" and result.digest is None


@pytest.mark.parametrize("path", [".env", "src/secrets.py", ".ssh/id_rsa", "keys/client.pem"])
def test_sensitive_paths_remain_excluded_even_if_manifest_lists_them(context, path):
    context["enrollment"] = replace(context["enrollment"], approved_code_paths=frozenset({path}))
    assert preview_activity((replace(metadata()[0], path=path),), **context).digest is None


@pytest.mark.parametrize("count", [True, False, 1.0, float("nan"), -1, 2**31])
def test_completed_counts_are_present_bounded_integers_never_inferred(context, count):
    result = preview_activity((replace(metadata()[1], failed=count),), **context)
    assert result.state == "empty" and result.digest is None


def test_running_and_unbound_tests_do_not_become_completed_receipts(context):
    assert preview_activity((replace(metadata()[1], status="running"),), **context).digest is None
    context["event_bindings"] = (
        context["event_bindings"][0],
        replace(context["event_bindings"][1], completed_test_run_id=None),
    )
    assert preview_activity((metadata()[1],), **context).digest is None


def test_raw_revision_order_and_prose_channels_do_not_change_semantic_digest(context):
    original = preview_activity(metadata(), **context)
    changed = (
        replace(metadata()[0], revision=999),
        replace(metadata()[1], revision=1000),
        ActivityMetadata(100, 1001, "message"),
        ActivityMetadata(101, 1002, "tool"),
        ActivityMetadata(102, 1003, "reasoning"),
        ActivityMetadata(103, 1004, "personal"),
    )
    updated = preview_activity(tuple(reversed(changed)) + changed, **context)
    assert updated.canonical_text == original.canonical_text
    assert updated.content_sha256 == original.content_sha256 and updated.omitted == original.omitted
    changed_count = preview_activity((metadata()[0], replace(metadata()[1], passed=5)), **context)
    assert changed_count.content_sha256 != original.content_sha256


@pytest.mark.parametrize("flag", ["sensitive", "withheld"])
def test_explicit_confidentiality_denials_are_respected(context, flag):
    assert preview_activity((replace(metadata()[0], **{flag: True}),), **context).digest is None


def test_observed_revision_requires_trusted_repo_observer_and_changes_fingerprint(context):
    session = context["session"]
    observation = RevisionObservation(
        "a" * 40,
        "repo:engineering",
        "scm:observation-a",
        session.canonical_session_id,
        "epoch-one",
        "collector",
        observed=True,
    )
    first = preview_activity((), revision_observations=(observation,), **context)
    assert first.digest.observed_revisions[0].commit_sha == "a" * 40
    second = preview_activity(
        (), revision_observations=(replace(observation, commit_sha="b" * 40),), **context
    )
    assert first.content_sha256 != second.content_sha256
    assert (
        preview_activity(
            (), revision_observations=(replace(observation, observed=False),), **context
        ).digest
        is None
    )
    assert (
        preview_activity(
            (),
            revision_observations=(replace(observation, canonical_repository_id="repo:other"),),
            **context,
        ).digest
        is None
    )


def test_no_eligible_activity_despite_sources_and_configured_link(context):
    result = preview_activity((), **context)
    assert result.state == "empty" and result.digest is None and result.content_sha256 is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://100.104.39.6",
        "https://control.dev.lan",
        "http://127.0.0.1:8080",
        "https://bender.tail0c50da.ts.net:9447",
        "https://private.example.test",
    ],
)
def test_private_configured_origins_no_login_or_network_needed(context, origin):
    context["enrollment"] = replace(context["enrollment"], configured_private_origin=origin)
    result = preview_activity(metadata(), **context)
    assert result.state == "preview"
    assert result.digest.private_session_link.startswith(origin + "/#/herd/session/")


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:secret@control.dev.lan",
        "http://control.dev.lan/path",
        "https://control.dev.lan?token=secret",
        "javascript:alert(1)",
    ],
)
def test_unapproved_or_non_origin_url_context_is_invalid(context, origin):
    context["enrollment"] = replace(context["enrollment"], configured_private_origin=origin)
    assert preview_activity(metadata(), **context).state == "invalid_context"


def test_caps_multibyte_budget_drop_whole_items_and_unreferenced_sources(context):
    paths = [f"src/{i:02}-" + ("界" * 150) + ".py" for i in range(12)]
    context["enrollment"] = replace(context["enrollment"], approved_code_paths=frozenset(paths))
    records = tuple(
        ActivityMetadata(i, 1, "file", path=path, operation="edit") for i, path in enumerate(paths)
    )
    session = context["session"]
    context["event_bindings"] = tuple(
        EventBinding(
            i,
            f"journal:{i}",
            "repo:engineering",
            session.canonical_session_id,
            "epoch-one",
            "collector",
        )
        for i in range(len(paths))
    )
    result = preview_activity(records, **context)
    value = json.loads(result.canonical_text)
    assert len(result.canonical_text.encode("utf-8")) <= 3072
    assert len(value["files"]) <= 8 and 0 < len(value["files"]) < 8
    assert all(item["path"] in paths for item in value["files"])
    assert len(value["source_hashes"]) == len(value["files"])
    assert result.omitted.cap > 0 and result.omitted.byte_budget > 0


def test_partial_reported_test_counts_remain_missing_never_zero(context):
    result = preview_activity((replace(metadata()[1], failed=None, skipped=None),), **context)
    assert result.state == "preview"
    assert json.loads(result.canonical_text)["completed_tests"][0]["reported_counts"] == {
        "passed": 4
    }
    result = preview_activity(
        (replace(metadata()[1], passed=None, failed=None, skipped=None),), **context
    )
    assert json.loads(result.canonical_text)["completed_tests"][0]["reported_counts"] == {}


def test_unused_kind_fields_do_not_affect_deduplication_or_omissions(context):
    original = preview_activity(metadata(), **context)
    file_variant = replace(
        metadata()[0],
        revision=900,
        run_id="PRIVATE_TEST_NAME",
        status="secret",
        passed=["PERSONAL"],
        failed=True,
    )
    test_variant = replace(metadata()[1], revision=901, path="/outside/private", operation="delete")
    changed = preview_activity((file_variant, test_variant, *metadata()), **context)
    assert changed == original


def test_conflicting_same_record_is_order_independent_without_revision_guess(context):
    one = metadata()[0]
    two = replace(one, operation="delete", revision=10000)
    first = preview_activity((one, two), **context)
    second = preview_activity((two, one), **context)
    assert first == second and first.state == "empty"
    assert first.omitted.invalid_metadata == 1


@pytest.mark.parametrize("project", [None, True, 42, {}, []])
def test_malformed_enrollment_project_is_invalid_not_typeerror(context, project):
    context["enrollment"] = replace(context["enrollment"], project_id=project)
    assert preview_activity(metadata(), **context).state == "invalid_context"


@pytest.mark.parametrize("flag", ["sensitive", "withheld"])
@pytest.mark.parametrize("reverse", [False, True])
def test_revision_confidentiality_conflict_same_provenance_fails_closed(context, flag, reverse):
    session = context["session"]
    public = RevisionObservation(
        "a" * 40,
        "repo:engineering",
        "scm:shared",
        session.canonical_session_id,
        "epoch-one",
        "collector",
        observed=True,
    )
    denied = replace(public, **{flag: True})
    observations = (denied, public) if reverse else (public, denied)
    result = preview_activity((), revision_observations=observations, **context)
    assert result.state == "empty" and result.digest is None
    assert result.omitted.confidential == 1


@pytest.mark.parametrize("run_id", ["\ud800", "x" * 1025])
def test_invalid_matched_run_id_is_safely_omitted_before_hashing(context, run_id):
    context["event_bindings"] = (
        replace(context["event_bindings"][1], completed_test_run_id=run_id),
    )
    result = preview_activity((replace(metadata()[1], run_id=run_id),), **context)
    assert result.state == "empty" and result.digest is None
    assert result.omitted.invalid_metadata == 1


@pytest.mark.parametrize("suffix", ["?", "#", "?#"])
def test_empty_origin_delimiters_are_invalid(context, suffix):
    context["enrollment"] = replace(
        context["enrollment"], configured_private_origin="https://private.example.test" + suffix
    )
    assert preview_activity(metadata(), **context).state == "invalid_context"
