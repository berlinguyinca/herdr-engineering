# GitHub Repository and Project Setup

Phase 0010 should create/reconcile the GitHub repository `berlinguyinca/herdr-engineering` and make the implementation program visible without duplicating the specs into prose-only tickets.

## Recommended labels

- `phase:0010` … `phase:0180`
- `area:fleet`
- `area:web-mobile`
- `area:browser`
- `area:artifacts`
- `area:sessions`
- `area:pi-autospec`
- `area:dev-fabric`
- `area:tests`
- `area:ci`
- `area:ux`
- `area:security`
- `upstream`
- `cross-repo`
- `blocked-external`

## Recommended milestones

1. **A — Trusted Foundation** (0010–0050)
2. **B — Usable Cockpit** (0060–0100)
3. **C — Distributed IDE** (0110–0150)
4. **D — Production Release** (0160–0180)

## Issue policy

Create one parent issue per numbered phase using `.github/ISSUE_TEMPLATE/implementation-phase.yml`. Create child issues only when they represent independently testable work; the numbered spec remains the requirement authority.

Each issue/PR should reference exact acceptance rows and preserve links to cross-repository PRs.

## Branch protection target

Once CI exists, protect the default branch with required review and passing repository checks. Do not require checks that depend on unavailable private infrastructure for ordinary documentation-only contributions; separate public/static checks from fleet integration gates where necessary.
