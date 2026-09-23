# 0180 — Fleet Validation, Release and Documentation

## Goal

Prove the whole product from fresh install through rollback and ship documentation that matches reality.

## Full acceptance run

Execute every row in `docs/acceptance-matrix.md` across representative Linux/macOS hosts and a phone/iPad tailnet client. Include a high-concurrency scenario with dozens of sessions and at least six parallel candidate worktrees.

## Fresh install

From documented prerequisites only:

1. enroll a clean supported host through Ansible;
2. install/pin the Powerpack;
3. connect it as a Herdr machine;
4. run Pi through `https://llm.metabolomics.us`;
5. open from web/mobile;
6. create session/worktree/artifact/dev service/test evidence;
7. observe CI when configured.

No undocumented manual fixes are allowed.

## Upgrade + rollback

Validate upgrade from the previous pinned release/profile and deliberate rollback. Record exact state that is preserved (sessions, user config, activity database, artifacts, leases/reconciliation behavior).

## Documentation

Finalize:

- README product story and screenshots/architecture diagram where useful;
- installation and fleet enrollment;
- plugin/dependency policy;
- web/mobile use;
- dev fabric use and troubleshooting;
- session timeline semantics/privacy;
- test/CI integration;
- operator/security/update/rollback guide;
- contributor/plugin-adapter guide;
- known limitations and supported versions.

## Release artifacts

- immutable dependency lock;
- tagged release/change log;
- validated Ansible/config examples;
- machine-readable compatibility matrix;
- completed acceptance evidence/report.

## Exit gate

Every non-external-blocked acceptance item is green, fresh install and upgrade/rollback drills pass, documentation commands are copy-tested, and the release can be recreated from Git + declared external dependencies without hidden workstation state.
