# GitHub projects

The native Projects page lists repositories accessible to the configured personal GitHub token. Repository ranking uses open issues excluding pull requests. Private and archived repositories remain inspectable. The UI paginates summaries, retrieves issue detail on demand and shows the actual background sync phase and coverage. A partial or rate-limited sync retains its last-known records.

Place the personal token in `~/.config/herdr-engineering/github-token` with mode `0600`. Set `HERDR_ENGINEERING_GITHUB_TOKEN_FILE` when using another private path. Never put the token in a browser request, URL, repository file or command argument. Repository access depends on the token's granted repository/organization permissions; a fine-grained token must permit reading the selected repositories and their issues.

For Docker, add `deploy/control-plane/compose.github.yaml` to the existing Compose invocation. It mounts the private file read-only in the web service at `/run/secrets/github-token`. This override is independent of Woodpecker configuration. Replace only the web service with `up -d --no-deps control-plane-web`; preserve the existing deployment environment, mounts and persistence settings.

AutoSpec tabs reflect issue labels, not a guarantee that a worker can run an issue. Candidates can still require safety admission, dependencies, an available worker or PR reconciliation. Dashboard reads never run queue reconciliation or modify GitHub issues. Old open issues remain open work, regardless of their last update time.
