"""Scoped offline research observations and local form proposals, never submission."""
from dataclasses import replace

import pytest

from herdr_engineering.event_contacts import (
    ContactRoute,
    ContactSnapshot,
    FormField,
    prepare_form,
    select_route,
)
from herdr_engineering.event_coordinator import Coordinator, Requirements, Venue


def coordinator():
    return Coordinator("user:one", "task:dinner", Requirements("2030-05-01 18:00", "America/Los_Angeles", 8, 0, 400, "USD"),
                       ("bistro",), ("resource:bistro",))


def venue():
    return Venue("bistro", "Example Bistro", "Fixture City", "verified")


def snapshot(routes=None):
    return ContactSnapshot("user:one", "task:dinner", "bistro", "resource:bistro",
                           "https://bistro.example.test", "https://bistro.example.test/contact",
                           100, 300, True, routes if routes is not None else (
                               ContactRoute("form", "https://bistro.example.test/contact"),))


def test_route_selection_prefers_email_and_deduplicates():
    email = ContactRoute("email", "events@bistro.example.test")
    s = snapshot((ContactRoute("form", "https://bistro.example.test/contact"), email, email))
    result = select_route(coordinator(), venue(), s, 200)
    assert result.route == email and result.blockers == ()
    assert not hasattr(result, "send")


def test_ambiguous_email_never_silently_falls_back_to_form():
    s = snapshot((ContactRoute("email", "one@example.test"), ContactRoute("email", "two@example.test"),
                  ContactRoute("form", "https://bistro.example.test/contact")))
    result = select_route(coordinator(), venue(), s, 200)
    assert result.route is None and "contact_route_ambiguous" in result.blockers


@pytest.mark.parametrize("changes", [
    {"user_id":"user:other"}, {"task_id":"task:other"}, {"venue_id":"other"},
    {"resource_ref":"resource:other"},
])
def test_observation_cannot_widen_delegation_scope(changes):
    with pytest.raises(ValueError):
        select_route(coordinator(), venue(), replace(snapshot(), **changes), 200)


@pytest.mark.parametrize("now", [99, 300])
def test_expired_or_future_observation_is_blocked(now):
    result = select_route(coordinator(), venue(), snapshot(), now)
    assert result.route is None and "contact_observation_stale" in result.blockers


def test_identity_and_cross_origin_observations_are_blocked():
    assert select_route(coordinator(), venue(), replace(snapshot(), identity_verified=False), 200).route is None
    assert select_route(coordinator(), replace(venue(), identity="closed"), snapshot(), 200).route is None
    assert select_route(coordinator(), venue(), replace(snapshot(), source_url="https://attacker.test/contact"), 200).route is None
    s = snapshot((ContactRoute("form", "https://attacker.test/submit"),))
    assert "contact_form_cross_origin" in select_route(coordinator(), venue(), s, 200).blockers


def test_official_page_email_may_use_a_different_domain():
    s = snapshot((ContactRoute("email", "bistro@example-mail.test"),))
    assert select_route(coordinator(), venue(), s, 200).route == s.routes[0]


def test_local_form_proposal_minimizes_fields_and_always_blocks_submission():
    fields = (FormField("your-email", "email", True), FormField("your-name", "name", True),
              FormField("body", "message", True), FormField("phone", "phone", False),
              FormField("honeypot", "message", False, False))
    plan = prepare_form(coordinator(), venue(), snapshot(), fields, 200,
                        requester_email="host@example.test", requester_name="Fixture Host")
    assert dict(plan.values)["your-email"] == "host@example.test"
    assert "8 adults and 0 children" in dict(plan.values)["body"]
    assert "phone" not in dict(plan.values) and "honeypot" not in dict(plan.values)
    assert plan.blockers == ("external_write_capability_unbound", "outreach_approval_required")
    assert (plan.user_id, plan.task_id, plan.venue_id, plan.resource_ref) == (
        "user:one", "task:dinner", "bistro", "resource:bistro")
    assert plan.source_url == snapshot().source_url and len(plan.digest) == 64
    assert not hasattr(plan, "submit")


def test_required_unsupported_or_hidden_fields_are_not_populated():
    fields = (FormField("phone", "phone", True), FormField("password", "password", True),
              FormField("hidden", "email", True, False))
    plan = prepare_form(coordinator(), venue(), snapshot(), fields, 200, requester_email="host@example.test")
    assert not plan.values
    assert {"unsupported_required_field:phone", "unsupported_required_field:password", "hidden_required_field:hidden"}.issubset(plan.blockers)


def test_unknown_requirements_never_become_form_placeholder_values():
    c = replace(coordinator(), requirements=replace(coordinator().requirements, date_time=None))
    fields = (FormField("date", "date_time", True), FormField("body", "message", True))
    plan = prepare_form(c, venue(), snapshot(), fields, 200, requester_email="host@example.test")
    assert plan.values == ()
    assert "missing_required_field:date" in plan.blockers and "missing_required_field:body" in plan.blockers


def test_failed_contact_verification_produces_no_form_values():
    plan = prepare_form(coordinator(), venue(), replace(snapshot(), identity_verified=False),
                        (FormField("email", "email", True),), 200, requester_email="host@example.test")
    assert plan.values == ()
    assert "contact_identity_unverified" in plan.blockers


def test_proposal_digest_binds_recipient_scope_and_explicit_values():
    fields = (FormField("name", "name", True),)
    a = prepare_form(coordinator(), venue(), snapshot(), fields, 200,
                     requester_email="host@example.test", requester_name="One")
    b = prepare_form(coordinator(), venue(), snapshot(), fields, 200,
                     requester_email="host@example.test", requester_name="Two")
    assert a.digest != b.digest


@pytest.mark.parametrize("target", ["http://bistro.example.test", "https://user:pass@bistro.example.test", "javascript:submit()"])
def test_unsafe_form_targets_are_rejected(target):
    with pytest.raises(ValueError):
        ContactRoute("form", target)


def test_mutable_routes_and_duplicate_form_keys_rejected():
    with pytest.raises(ValueError):
        snapshot([])
    with pytest.raises(ValueError):
        prepare_form(coordinator(), venue(), snapshot(), (FormField("same", "email", True), FormField("same", "name", True)),
                     200, requester_email="host@example.test")


def test_stopped_or_replied_venue_does_not_prepare_another_contact():
    for c in (replace(coordinator(), stopped_venues=("bistro",)), replace(coordinator(), replied_venues=("bistro",))):
        plan = prepare_form(c, venue(), snapshot(), (FormField("email", "email", True),),
                            200, requester_email="host@example.test")
        assert plan.values == () and plan.target is None


@pytest.mark.parametrize("status", ["sent", "uncertain", "bounced"])
def test_prior_email_delivery_does_not_turn_into_a_duplicate_form_inquiry(status):
    from herdr_engineering.event_coordinator import Receipt

    receipt = Receipt("key:previous-email", "bistro", "events@example.test", "thread", "message", 150, status, False)
    c = replace(coordinator(), receipts=(receipt,))
    plan = prepare_form(c, venue(), snapshot(), (FormField("email", "email", True),),
                        200, requester_email="host@example.test")
    assert plan.values == () and plan.target is None


def test_contact_source_query_cannot_leak_secrets_into_provenance():
    with pytest.raises(ValueError):
        replace(snapshot(), source_url="https://bistro.example.test/contact?access_token=source-secret")


def test_contact_form_query_cannot_leak_secrets_into_proposal():
    with pytest.raises(ValueError):
        ContactRoute("form", "https://bistro.example.test/contact?session=form-secret")
