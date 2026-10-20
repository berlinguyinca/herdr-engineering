# GitHub projects and issues

Provide a native Projects page covering repositories accessible to the configured personal GitHub token. Keep that token in a private server file and mount it only in the web service. Repository ordering uses exact open issue totals, excluding pull requests, highest first. Include private and archived repositories with explicit labels.

Fetch repository summaries first, then issue summaries progressively in a background singleflight refresh. Show actual repository and issue progress, partial coverage, last successful refresh and rate-limit/error states. Preserve last-known data. Paginate the browser response and load issue bodies only on inspection. Do not poll thousands of full bodies or expose credentials.

Issue tabs: All open issues, AutoSpec candidates, Needs preparation, In progress, Blocked. Classification follows recorded AutoSpec labels. A candidate is not a claim that execution is ready; dependencies, admission and capacity are not inferred. Dashboard reads must not invoke mutating AutoSpec queue reconciliation. Old open issues remain visible regardless of age.

Reuse canonical repository identity and explicit project mappings. Do not infer issue-to-commit or cross-repository links from titles or directory names. Preserve focus, filters, pagination and issue inspection during polling, and provide useful desktop and mobile layouts.

Verification: provider pagination and exact counts, HTTP/GraphQL errors, rate limits, token redaction, classification, API authorization/query validation, browser progress/filter/inspection/disposal/mobile checks, full regression suite, independent review, green CI and deployment preserving existing history and native processes.
