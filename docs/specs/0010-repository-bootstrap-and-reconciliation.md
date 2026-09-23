# 0010 — Repository Bootstrap and Reconciliation

## Goal

Create the canonical repository structure and establish what is already implemented elsewhere before writing product code.

## Requirements

- Initialize or reconcile `berlinguyinca/herdr-engineering`; preserve existing Git history if present.
- Copy this complete spec bundle into the repository.
- Establish formatting/lint/test/build conventions appropriate to the chosen implementation languages after repository inspection; do not choose a language merely because the spec is Markdown.
- Create `IMPLEMENTATION_RECONCILIATION.md` mapping every requirement to `EXISTING`, `PARTIAL`, `MISSING`, `CONFLICTING`, `EXTERNAL`, or `SUPERSEDED` with source links.
- Inspect relevant local/remote repositories: Herdr integration points, Pi Engineering, AutoSpec, fleet Ansible, Pileated/Woodpecker integration and any pre-existing HerdR add-ons.
- Record which repository owns each required code change.
- Preserve archived predecessor specs and record conflicts against current normative specs.
- Create basic GitHub issue labels/milestones or equivalent phase mapping so the implementation program is visible in GitHub.

## Deliverables

- `IMPLEMENTATION_RECONCILIATION.md`
- initial repository CI for docs/schema/config validation
- phase/milestone issue structure
- updated `STATUS.md`

## Exit gate

A reviewer can trace every master requirement to a current owner/status, and no implementation phase relies on an unidentified repository or guessed pre-existing API.
