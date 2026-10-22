# Publish CI test reports

The CI dashboard receives structured JUnit XML separately from Woodpecker run status. Missing reports mean **no report received**, not zero tests. The uploader publishes one report file for a run; use your test runner's combined JUnit output when it produces multiple files. Tests and their failure details are normalized, bounded and redacted by the control plane. Raw XML and test stdout are not retained.

Install HerdR Engineering in the CI environment, or run the module from its checked-out repository with Python 3.12+. No additional uploader dependencies are required.

```sh
# Supply this through the CI secret store, not a literal command or logged argument.
export HERDR_ENGINEERING_API_KEY
export HERDR_ENGINEERING_CONTROL_PLANE_URL=https://herdr.example

python3 -m herdr_engineering.ci_report_upload junit.xml \
  --repository org/app --run-number 123 --branch main --revision "$CI_COMMIT_SHA" \
  --provider-repo-id "$CI_REPO_ID"
```

`HERDR_ENGINEERING_API_KEY` must be a control-plane **operator** key. Viewer and ingest-only keys cannot upload reports. Use HTTPS for traffic outside a trusted local network. The command refuses redirects, embedded URL credentials, non-HTTP URLs, query strings and fragments. A failed upload exits nonzero and prints a bounded diagnostic without echoing credentials, XML, response bodies or destination URLs.

For the intentionally open deployment on the trusted Tailnet (configured with empty API keys as described in [the deployment guide](../deploy/control-plane/README.md)), supply the explicit `--allow-open` flag and leave `HERDR_ENGINEERING_API_KEY` unset:

```sh
# Woodpecker supplies CI_REPO, CI_PIPELINE_NUMBER, CI_COMMIT_BRANCH and CI_COMMIT_SHA.
unset HERDR_ENGINEERING_API_KEY
python3 -m herdr_engineering.ci_report_upload junit.xml --allow-open --control-plane http://dev.lan
```

This sends no authentication header. Without `--allow-open`, a missing key remains an error. The flag does not bypass server authentication: a keyed server rejects uploads without a key. If a key is present, it is validated and sent normally even with the flag. Use this mode only for an intentionally open server reachable through the trusted Tailnet; it does not change the server’s global authentication configuration.

The following Woodpecker variables supply defaults; explicit options override them:

| Option | Environment default |
| --- | --- |
| `--control-plane` | `HERDR_ENGINEERING_CONTROL_PLANE_URL` |
| `--repository` | `CI_REPO` |
| `--run-number` | `CI_PIPELINE_NUMBER` |
| `--branch` | `CI_COMMIT_BRANCH` |
| `--revision` | `CI_COMMIT_SHA` |
| `--provider-repo-id` | `CI_REPO_ID` |

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
