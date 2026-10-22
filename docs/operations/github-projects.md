# GitHub projects

The native Projects page lists repositories accessible to the configured personal GitHub token. Repository ranking uses open issues excluding pull requests. Private and archived repositories remain inspectable. The UI paginates summaries, retrieves issue detail on demand and shows the actual background sync phase and coverage. A partial or rate-limited sync retains its last-known records.

Keep the personal token in the `github` section of the central fleet config (mode `0600`). Never put the token in a browser request, URL, repository file or command argument. Repository access depends on the token's granted repository/organization permissions; a fine-grained token must permit reading the selected repositories and their issues.

For Docker, `deploy/control-plane/deploy.sh` mounts the central file read-only into the web service and replaces only that service (`up -d --no-deps control-plane-web`); persistence settings are untouched.

AutoSpec tabs reflect issue labels, not a guarantee that a worker can run an issue. Candidates can still require safety admission, dependencies, an available worker or PR reconciliation. Dashboard reads never run queue reconciliation or modify GitHub issues. Old open issues remain open work, regardless of their last update time.
