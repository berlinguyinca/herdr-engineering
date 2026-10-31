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
