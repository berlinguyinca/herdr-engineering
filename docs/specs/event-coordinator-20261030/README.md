# Event coordinator development slice

This branch contains an offline reusable domain core, not a deployed personal assistant.
No email/form/venue contact, browser, booking, payment or LLM request is performed.

Files:
- `herdr_engineering/event_coordinator.py`: immutable domain, drafts, policy proposals,
  receipts, source evidence, unanswered/conflict comparison and checkpoint round trips.
- `herdr_engineering/event_fixture.py`: local fixture runner using existing artifact publisher.
- `tests/test_event_coordinator.py`: behavioral and adversarial offline scenarios.
- `tests/fixtures/event_coordinator/birthday.json`: 16 venue leads from supplied summary, with personal sheet metadata redacted.
- `SPEC.md`: code reuse map, capability/task contracts, plan and integration blockers.

Run from this worktree with existing Python/pytest, no install:

```sh
PYTHONPATH=. pytest -q tests/test_event_coordinator.py
PYTHONPATH=. python3 -m herdr_engineering.event_fixture \
  --fixture tests/fixtures/event_coordinator/birthday.json \
  --output /tmp/herdr-event-fixture-artifacts
```

The printed ArtifactRef identifies a hash-checked JSON report under the output root.
It includes 16 personalized local drafts, missing inputs, all unanswered questions,
comparison evidence slots, a restart checkpoint and the unbound browser envelopes.
Historical contact leads stay separate from draft recipients. All recipients are null.
The sender is `fixture-host@example.test`; it is never the user's identity.
Unknown event details appear as explicit `[... required]` markers. Closed/ambiguous
venues are retained for comparison, blocked from outreach. No approval is created.

Production integration must supply authenticated user/task storage, grant issuance and
revocation, verified official recipient identity, a durable transactional outbox with
provider delivery reconciliation, authorized inbox/Sheets capability handles and PA/Pi
runtime/model/budget/notification interfaces. `Grant` is a testable local policy value;
a caller constructing one gains no authority. `blockers() == ()` is advisory only.
Restore snapshots only alongside current central journal/revocations; never import them
from a venue, webpage or email. Opt-outs/bounces/any correlated reply stop follow-ups.
Conflicting answers remain unresolved rather than silently choosing a venue.

The initial implementation allows one deterministic draft per exact content. To propose
another follow-up, supply a newly reviewed Draft and exact new approval hash; it must
still satisfy receipt history, cadence, max attempts and the approval window. Automatic
reply extraction, live source research, final venue ranking and production dispatch are
not implemented here. A missing budget cannot be replaced by an invented estimate.

## Main-lane dinner workflow handoff

Checkpoint `6ec0733` introduced the domain. The subsequent dinner handoff commit adds
`tests/fixtures/event_coordinator/dinner-eight-under-400.json` and
`docs/contracts/event-fixture-report-v1.schema.json` (ID `urn:herdr:event-fixture-report:v1`).
The latter references the browser owner's schema ID `urn:herdr:browser-capability:v1`;
register that canonical schema to use a general validator. It does not copy browser code.

The dinner fixture explicitly models eight synthetic adults, zero children and a USD400
ceiling. That split is fixture data, not an inferred real-world preference. Date/time
and timezone stay unknown. No cost quote is supplied, so comparisons correctly show
unknown costs; the report does not assert that any venue meets the ceiling.

```sh
PYTHONPATH=. python3 -m herdr_engineering.event_fixture \
  --fixture tests/fixtures/event_coordinator/dinner-eight-under-400.json \
  --output /tmp/herdr-dinner-fixture-artifacts
```

Public fixture API: `prepare_fixture(data: dict, workspace: ArtifactWorkspaceProvider)
-> PublishResult` in `herdr_engineering/event_fixture.py`. `data` has `source`,
`requirements` and `venues`, as illustrated by either JSON fixture. Venue keys are
`id/name/city/identity/priority`, optional historical `contact/source`. Requirements
keys are `date_time/timezone/adults/children/budget/currency`; unknown values are null.
Output is an immutable artifact reference; `workspace.read(result.artifact)` returns
report JSON. Fixture IDs/sender are deliberately synthetic and fixed in this wrapper.

General domain API for main orchestration: construct
`Coordinator(user_id, task_id, Requirements(...), venue_ids=(...), resource_refs=(...))`.
Then use `draft(venue, sender)`, `observe(Evidence(...))`, `comparison(venue_id)`,
`unanswered(venue_id)`, `dumps()/loads()` and `browser_proposal(...)`. Issue selected
scope through your authenticated orchestration; never infer it from model/page content.
Main can supply its own input dictionaries or use the domain directly without editing
these files. Review UI/delegation/task-scope orchestration remain main-lane ownership.
