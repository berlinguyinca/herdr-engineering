# Publish CI test reports

The CI dashboard receives structured JUnit XML separately from Woodpecker run status. Missing reports mean **no report received**, not zero tests. The uploader publishes one report file for a run; use your test runner's combined JUnit output when it produces multiple files. Tests and their failure details are normalized, bounded and redacted by the control plane. Raw XML and test stdout are not retained.

Install HerdR Engineering in the CI environment, or run the module from its checked-out repository with Python 3.12+. No additional uploader dependencies are required.

```sh
# Supply this through the CI secret store, not a literal command or logged argument.
export HERDR_ENGINEERING_API_KEY
export HERDR_ENGINEERING_CONTROL_PLANE_URL=https://herdr.example

python3 -m herdr_engineering.ci_report_upload junit.xml \
  --repository org/app --run-number 123 --branch main --revision "$CI_COMMIT_SHA"
```

`HERDR_ENGINEERING_API_KEY` must be a control-plane **operator** key. Viewer and ingest-only keys cannot upload reports. Use HTTPS for traffic outside a trusted local network. The command refuses redirects, embedded URL credentials, non-HTTP URLs, query strings and fragments. A failed upload exits nonzero and prints a bounded diagnostic without echoing credentials, XML, response bodies or destination URLs.

For the intentionally open deployment on the trusted Tailnet (configured with empty API keys as described in [the deployment guide](../deploy/control-plane/README.md)), supply the explicit `--allow-open` flag and leave `HERDR_ENGINEERING_API_KEY` unset:

```sh
# Woodpecker supplies CI_REPO, CI_PIPELINE_NUMBER, CI_COMMIT_BRANCH and CI_COMMIT_SHA.
unset HERDR_ENGINEERING_API_KEY
python3 -m herdr_engineering.ci_report_upload junit.xml --allow-open --control-plane http://dev.lan
```

This sends no authentication header. Without `--allow-open`, a missing key remains an error. The flag does not bypass server authentication: a keyed server rejects uploads without a key. If a key is present, it is validated and sent normally even with the flag. Use this mode only for an intentionally open server reachable through the trusted Tailnet; it does not change the server’s global authentication configuration.

The uploader reads the following environment defaults; explicit options override them. Woodpecker supplies repository, run number, branch and revision. The control-plane URL and optional internal repository ID are operator configuration.

| Option | Environment default |
| --- | --- |
| `--control-plane` | `HERDR_ENGINEERING_CONTROL_PLANE_URL` |
| `--repository` | `CI_REPO` |
| `--run-number` | `CI_PIPELINE_NUMBER` |
| `--branch` | `CI_COMMIT_BRANCH` |
| `--revision` | `CI_COMMIT_SHA` |
| `--provider-repo-id` | `CI_REPO_ID` (optional, user-configured internal Woodpecker repository ID) |

`CI_REPO_ID` is a compatibility default for an explicitly configured internal Woodpecker repository ID; Woodpecker 3.18 does not supply it automatically. Do not substitute `CI_REPO_REMOTE_ID`, which identifies the repository in its forge and can differ from the internal Woodpecker ID. The InferWeave relay uses its allowlisted internal ID `116`.

Repository, positive run number, branch and revision are required. The provider repository ID is recommended to distinguish repositories with equal names on different forges. It must match the identity reported by Woodpecker. If omitted, HerdR links the report only when the retained observations uniquely match its exact repository, run number, branch and revision. Reports uploaded before a run is observed can link on a later refresh. Ambiguous or unmatched reports remain explicitly unlinked; HerdR never chooses a provider by name alone. The report must be UTF-8 XML of at most 1 MiB, with at most 5,000 cases and no DTD/entity declarations. The response is bounded to 2 MiB; network operations use a 15-second timeout.

## Woodpecker: publish even when tests fail

Keep test execution and upload in separate steps. The test command keeps its real exit status, and the report step runs for either success or failure using Woodpecker’s [status condition](https://woodpecker-ci.org/docs/usage/workflow-syntax#status). This example assumes the HerdR package is already available to the Python image through your approved installation/build process; replace the test command and report path for the project.

```yaml
steps:
  - name: tests
    image: your-project-python-ci-image
    commands:
      - python3 -m pytest --junitxml=junit.xml

  - name: publish-test-report
    image: your-project-python-ci-image
    environment:
      HERDR_ENGINEERING_CONTROL_PLANE_URL: https://herdr.example
      HERDR_ENGINEERING_API_KEY:
        from_secret: herdr_operator_key
    commands:
      - python3 -m herdr_engineering.ci_report_upload junit.xml
    when:
      - status: [success, failure]
```

The shared workspace must preserve `junit.xml` between steps. A test-runner crash may leave no valid report; the upload then fails visibly rather than submitting an empty inventory. No report upload changes deployment permissions or triggers a deployment.

Identical repeated reports are deduplicated. Different reports for the same run become separate attempts. Do not upload different shards as separate attempts: combine their XML into one report first. Retained history has explicit limits, and dashboard counts describe received reports rather than an unbounded complete archive.

## InferWeave: relay native Hive reports from the host

Hive cannot reach the private HerdR control plane. Its test runner therefore writes an atomic JSON bundle into `/quobyte/metabolomicsgrp/it/server/woodpecker/artifacts/herdr-inferweave-v1` after native pytest execution. The relay runs on Bender, where the existing `hive` SSH alias reaches `login1` and the control plane is reachable. Run it on the host; the dashboard container does not provide SSH credentials or an SSH client.

The producer names each file `<run_number>-<40 lowercase hex revision>-hive-slurm.json`. Schema version 1 requires exactly these fields:

| Field | Required value |
| --- | --- |
| `schema_version` | Integer `1` |
| `repository` | `InferWeave/inferweave` |
| `provider_repo_id` | Integer `116` |
| `run_number` | Positive integer matching the filename |
| `branch` | Actual nonempty provider branch |
| `revision` | Full 40-character revision matching the filename |
| `suite` | `hive-slurm` |
| `runner_exit_code` | Native test runner exit code as an integer |
| `reported_at` | ISO timestamp including its timezone |
| `xml` | Original native JUnit XML, at most 1 MiB |

The whole UTF-8 bundle is limited to 2 MiB. XML must satisfy the uploader's parser limits. Test counts come from native cases; the relay never synthesizes reports from logs or runner exit codes. Missing or malformed reports remain missing in HerdR.

Before publishing, the relay reads `GET /api/v1/ci/pipeline?repository=116&number=<run>` and requires an available provider run with exactly matching run number, full revision and branch. A matching filename alone is insufficient. Unknown or mismatched runs and unsuccessful uploads remain retryable. An operator key is read from `HERDR_ENGINEERING_API_KEY`, or explicitly use `--allow-open` for the intentionally open trusted-network deployment:

```sh
python3 -m herdr_engineering.ci_report_relay \
  --source-host hive \
  --source-dir /quobyte/metabolomicsgrp/it/server/woodpecker/artifacts/herdr-inferweave-v1 \
  --control-plane http://dev.lan --allow-open \
  --state-dir "$HOME/.local/state/herdr-ci-report-relay" --once
```

Without `--once`, the process repeats every two minutes. Diagnostics print aggregate published/failed counts without credentials, URLs, filenames, XML or server bodies. A failed one-shot exits nonzero. Neither command runs tests, changes source artifacts, nor triggers deployments.

The source reader uses a static Python program over SSH. Configuration travels on stdin rather than through shell-interpolated filenames. It ignores symlinks and nonregular files, uses directory-relative no-follow reads, caps each read, and returns at most 10 reports per batch. It examines at most 20,000 directory entries and selects the newest 5,000 matching bounded files. It assumes the producer's atomic, immutable-per-run publication contract; acknowledged files with unchanged size and nanosecond modification time skip rereading, and reread content is deduplicated by SHA-256. Changed content in a previously published filename becomes another real report attempt.

Unreadable artifacts contribute to the failed count; permission errors cannot appear as an empty successful batch. A separate private, atomically saved scan cursor rotates through bounded source entries across restarts, so ten newest failed reports cannot indefinitely block older valid reports. The cursor records scan progress only; it does not mark a report delivered, and failed reports remain retryable.

The private state directory contains an atomically replaced, fsynced checkpoint with up to 5,000 acknowledged content hashes and file metadata. Checkpoints advance only after the control plane acknowledges the exact report identity; network failures, refused uploads, and metadata mismatches do not advance them. Existing checkpoint files must be owned by the current user and private. Keep the state directory across upgrades. The source bounds and checkpoint limits describe an operational relay window, not an unlimited historical artifact archive.

For periodic operation, maintain an isolated released checkout at `$HOME/IdeaProjects/herdr-ci-report-relay-runtime` and install the user unit templates from `deploy/control-plane/herdr-ci-report-relay.service` and `.timer` into `$HOME/.config/systemd/user/`. The service explicitly uses that host checkout as its working directory and `PYTHONPATH`, keeps state under the systemd user state directory, and uses the existing host SSH configuration. An optional private `$HOME/.config/herdr-engineering/ci-report-relay.env` can supply `HERDR_ENGINEERING_API_KEY`. Adjust the unit's endpoint/open-mode arguments when deploying to a keyed or different server.

```sh
systemctl --user daemon-reload
systemctl --user enable --now herdr-ci-report-relay.timer
systemctl --user start herdr-ci-report-relay.service
systemctl --user status herdr-ci-report-relay.timer
```

Unit installation and a genuine provider run are required for operational acceptance. Local transport/parser tests do not prove that a production Hive run has published a report.
