# Assistant package registration v1 plan

Goal: define and register immutable, content-addressed assistant package manifests without
launching runtimes or granting capabilities.

1. Write failing manifest tests for one valid Pi package and one valid Docker/Apptainer
   package, canonical digest stability, strict unknown-field rejection, forbidden
   authority/credential/host access, bounded nested values, immutable artifacts, model
   policy consistency, and the fixed `task-restricted-v1` sandbox request.
2. Implement a dependency-free manifest normalizer that returns canonical JSON, SHA-256
   digest, requested capabilities, and `runtime_launch_supported: false`. Keep user identity,
   grants, credentials, live knowledge, snapshots, and task state outside the manifest.
3. Add failing CLI tests for local `packages validate` without credentials and authenticated
   room-owner `register`, `list`, and `inspect`, including same-content replay, changed
   same-version conflict, cross-user/agent refusal, and versioned JSON errors.
4. Bind the client to the additive owner-only API at
   `/api/v1/collab/rooms/{room}/assistant-packages`; the server remains the authority for
   ownership and immutable registration.
5. Document the exact v1 manifest and sandbox request, runtime-adapter gaps, and verification.
   Run focused tests, CLI regressions, Ruff, compilation, and diff checks.

No runtime launch, container engine access, arbitrary host shell, credentials, grants,
snapshots, cloning, knowledge promotion, browser/event integration, or dependency addition
is part of this slice.
