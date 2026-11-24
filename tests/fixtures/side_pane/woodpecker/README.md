# Recorded Woodpecker and `gh` output for the CI tab

Captured 2026-11-18 from the tokenless public API (GET only, no credential sent or read):

- `lookup.json`: https://ci.metabolomics.us/api/repos/lookup/berlinguyinca/herdr-engineering
- `pipelines.json`: https://ci.metabolomics.us/api/repos/120/pipelines (the 50 most recent)
- `pipeline_<n>.json`: https://ci.metabolomics.us/api/repos/120/pipelines/<n> for 1751 (running), 1746 and 1745 (failure),
  1750 (push), 1728 (success), 1722 (killed), 1741 (cron, running)
- `../gh_pr_list.json`: `gh pr list --state all --limit 40 --json number,title,headRefName,state,mergedAt,url` in this repository,
  plus the one CLOSED row (#834) of `--state closed`

Personal fields (author, e-mail, avatar, sender, commit message) were removed; nothing else was edited.
`pipeline_1751_no_workflows.json` is DERIVED, not captured: pipeline 1751 with `workflows` emptied, because no recorded
pipeline has zero workflows. Pull-request pipelines carry the target branch (`main`) and `ref` `refs/pull/<n>/merge`.
