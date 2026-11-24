# CI does the merging: template

Repo-agnostic defaults from herdr-engineering (PRs #901, #905, #906, #910). Goal: nobody rebases, updates or re-reviews a PR just
because main moved; CI proves "PR + current main" and main proves itself. Reference implementation and rationale:
`docs/ci-and-reviews.md` ("Merge gating"), `scripts/ci/integrate-main.sh`, `.woodpecker/unit-gates-main.yml`.

## Files

| File | Copy to | Purpose |
|---|---|---|
| `integrate-main.sh` | `scripts/ci/integrate-main.sh` | On `pull_request`, merges `origin/<target>` into the checked-out head (throwaway identity, `.gitattributes` honoured). No-op for other events or if main is already contained. Exit 1 naming the files on conflict. Target branch comes from `CI_COMMIT_TARGET_BRANCH`, default `main`. Keep it byte-identical (a test enforces this against the reference). |
| `woodpecker/pr-gate.yml` | `.woodpecker/<name>.yml` | PR pipeline: checkout, `integrate-main`, your gates. Replace the `gates` command. |
| `woodpecker/main-gate.yml` | `.woodpecker/<name>-main.yml` | Same gates on every push to main, queued (limit 1), with a superseded-run guard. Replace the `gates` command. |

If the repo's existing pipeline already has a checkout step, just add the `integrate-main` step right after it (and after any
freshness guard) in every PR workflow, and add the main-push workflow.

## Adopting it in a repo

1. Copy the three files, replace the gate commands, add to `.gitattributes`: `CHANGELOG.md merge=union` (concurrent changelog
   bullets stop colliding; applies to the CI merge, not GitHub's merge button).
2. Open the PR. Open PRs forked before this lands have no `integrate-main` step (Woodpecker reads YAML from the PR head): rebase them
   before turning `strict` off.
3. After the first push-to-main run is green, change GitHub settings (all need repo admin; these are the settings, not done by the template):

```bash
R=OWNER/REPO
# allow auto-merge (once per repo)
gh api -X PATCH repos/$R -F allow_auto_merge=true -F delete_branch_on_merge=true
# branch protection: strict OFF (use -F so the value is a JSON boolean, not the string "false"), required contexts = the PR gates only.
# Do not put the push-to-main context in required_status_checks.
gh api repos/$R/branches/main/protection/required_status_checks   # read the current required_status_checks first
gh api -X PATCH repos/$R/branches/main/protection/required_status_checks \
  -F strict=false -f 'contexts[]=ci/woodpecker/pr/<workflow-name>'
```

   `required_status_checks.contexts` must name the exact PR-event context(s), for example `ci/woodpecker/pr/pr-gate`; a wrong name
   blocks every merge. Keep any existing contexts when editing; copy them from the read above.
4. Merge PRs with `gh pr merge --auto --squash <n>`: GitHub merges when the required checks are green, no up-to-date branch needed.

## Verifying

- A PR pipeline log shows `integrate-main: merged origin/main ...` or `nothing to merge`.
- A push to main starts the main-gate pipeline; a newer push makes an older queued run print `superseded by newer push` and end green.
- `gh api repos/$R/branches/main/protection --jq .required_status_checks.strict` prints `false`.

## Rollback

Set `strict=true` again (`-F strict=true`); optionally delete the main-gate workflow. The script is inert on every event but
`pull_request`.

## Notes

Home: the natural long-term home for a bootstrap template is the autospec repo (`bootstrap.sh`, `templates/`); this directory is the
source it can vendor. Never put review statuses (`Opus agent review`) in a template: they are posted by the review automation.
