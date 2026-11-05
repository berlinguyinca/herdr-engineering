# Scheduler dashboard implementation plan

**Goal:** Complete the approved dashboard slice on existing scheduler APIs:
operator policy editing, recent placement requests, queue state and explanations,
with refresh-safe controls and accessible feedback. Native placement remains deferred.

**Architecture:** Extend `mountScheduling`, the existing keyed morph/polling view,
and the current web client. Keep preview and policy forms outside refreshed
metrics/history regions. Use one policy editor with an explicit host selection,
captured revision, confirmation, and reload action. Polls may report a conflict
but never replace an edited draft. API revision conflicts require review of the
latest policy before resubmission. Preserve omitted tag restrictions on mode edits.

**Tech stack:** Existing JavaScript modules/components, Python control-plane API,
Node tests and Playwright. No new packages or alternate dashboard service.

- [x] Lock current behavior with scoped baseline tests.
- [x] Add failing service/API tests for chronological, sanitized history detail
  and policy edits preserving omitted restrictions; extend existing allowlists.
- [x] Add client policy mutation and rendered policy/history/queue sections.
- [x] Add browser regressions for edits surviving refresh/blur, revision conflict,
  success/failure/retry, keyboard controls, mobile layout and disposal.
- [x] Implement dirty-draft protection, request cancellation and live region updates.
- [x] Validate against real HTTP/backend policy handling, including a controlled
  isolated Linux/Mac placement-policy rollback canary. Do not change live fleet flags.
- [x] Run focused regressions and record evidence. Publication is tracked by the
  scoped PR linked to #290; full scheduler acceptance remains open.

Recent history represents current states of the latest 200 requests, not a full
transition audit trail or proof that claimed agents are running. Explain decisions
using a server-side allowlist; never send lease tokens or raw request snapshots.
The existing mounted view/design is retained. Missing telemetry remains unknown.
