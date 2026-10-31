# Event contact preparation v1

Pure Python interface in `herdr_engineering/event_contacts.py`; no runtime registration,
browser binding, send or submit operation. Additive to the fixture CLI at 784e69c;
main can keep its current pin until it chooses to integrate this extension.

## Research input boundary

`ContactSnapshot(user_id, task_id, venue_id, resource_ref, official_origin,
source_url, observed_at, valid_until, identity_verified, routes)` holds an immutable
structured observation from an authorized research adapter. `routes` is a tuple of
`ContactRoute("email", address)` or `ContactRoute("form", https_url)` values.
Times are finite nonnegative epoch seconds. Identity verification is an explicit
trusted attestation, never a website/model claim. Source and official origin must
be HTTPS without userinfo, query or fragment components; forms must share the verified
origin. Query-bearing source/target URLs are rejected rather than persisted or
redacted into a potentially different endpoint. Operational token/query data belongs
behind a future opaque authorized adapter binding, never in domain proposals.

`select_route(coordinator, venue, snapshot, now) -> RouteSelection` checks user/task,
selected venue/resource refs, venue identity, source origin and freshness. It dedupes
identical routes and prefers a unique email. Multiple emails are ambiguous even if
one form exists. Without email, exactly one same-origin form may be selected. An
empty/ambiguous/stale/unverified observation returns blockers and no selected route.
An out-of-task observation raises ValueError rather than expanding scope. Existing
opt-out/reply/delivery history blocks a new initial route, preventing an email inquiry
from becoming a duplicate form inquiry. This interface does not prepare follow-ups.

These checks do not prove a source is official. The adapter must independently
verify ownership using broker-resolved source/evidence and preserve provenance.
No HTTP request, mailbox access or HTML/browser parsing occurs in this module.

## Local form proposal

`prepare_form(coordinator, venue, snapshot, fields, now, *, requester_email,
requester_name=None) -> FormProposal` prepares only local text values. `fields` must
be a tuple of `FormField(name, semantic, required, visible=True)` with unique names.
An authorized adapter must derive semantic roles and visibility; arbitrary page text
is not a field instruction. It must not reinterpret these local names as executable
selectors without a separately reviewed browser operation.

Supported semantic roles: `name`, `email`, `subject`, `message`, `date_time`,
`timezone`, `adults`, `children`, `budget`, `currency`. Name/email come only from
explicit task inputs. Date/count/cost information comes from Requirements. Message
uses the personalized event draft only when its requirements are complete. Missing
required values become blockers; zero children remains the value "0".

Hidden controls and unsupported fields are omitted. Required hidden/unsupported
controls block preparation readiness; optional phone, payment, password, consent,
CAPTCHA and unrelated fields are omitted. No values are produced when contact
verification fails or email is the selected route. No placeholder is inserted for
unknown event values.

The frozen FormProposal contains user/task/venue/resource scope, source URL/time,
selected target, immutable `(field_name, text_value)` pairs and blockers. `digest`
is a stable SHA256 of those records, binding values to scope and provenance.
Every proposal includes `external_write_capability_unbound` and
`outreach_approval_required`, even when all fields can be prepared. It is not an
approval request accepted by the platform and cannot authorize submission.

Serialize with `dataclasses.asdict(proposal)` and the existing scoped artifact
publisher after user/task storage authorization. Snapshots/proposals are local
projections: live adapters must refresh origin/target/field evidence and authenticate
current authority before eventual external use. Contact research or a venue reply
never widens selected task scope or creates a grant.

## Validation

```sh
PYTHONPATH=. pytest -q tests/test_event_contacts.py tests/test_event_coordinator.py \
  tests/test_artifacts.py tests/test_contracts.py
```

Synthetic observations use example.test domains. No venue research or correspondence
is executed by these tests. Browser v1 remains intended observe/prepare only, unbound.
