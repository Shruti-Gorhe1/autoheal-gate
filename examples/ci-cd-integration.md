# Integrating AutoHeal Gate into an existing pipeline

This file shows the three integration shapes the gate supports. Pick whichever
matches how your CI already runs; all three call the same `POST /api/gate/check`
endpoint underneath.

## 1. GitHub Actions, using the bundled composite action (recommended)

```yaml
name: CI
on: [push, pull_request]

jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }

      - name: Run tests
        id: tests
        continue-on-error: true
        run: |
          set -o pipefail
          pytest -q 2>&1 | tee build-output.log

      - id: verdict
        run: |
          if [ "${{ steps.tests.outcome }}" = "success" ]; then
            echo "ci_passed=true" >> "$GITHUB_OUTPUT"
          else
            echo "ci_passed=false" >> "$GITHUB_OUTPUT"
          fi

      - name: AutoHeal Gate
        uses: <owner>/autoheal-gate/action@main
        with:
          gate-url: ${{ secrets.AUTOHEAL_GATE_URL }}
          api-key: ${{ secrets.AUTOHEAL_GATE_API_KEY }}
          ci-passed: ${{ steps.verdict.outputs.ci_passed }}
          failure-log-path: build-output.log
```

The step fails (non-zero exit) on `BLOCK`, and on `HOLD` unless you set
`fail-on-hold: "false"`. Branch protection can require this check like any
other, which is what makes a `HOLD` actually stop a merge.

## 2. GitHub webhook (works with any CI, no action needed)

1. In the target repository: **Settings → Webhooks → Add webhook**
   - Payload URL: `https://your-gate/api/integrations/github/webhook`
   - Content type: `application/json`
   - Secret: the same value as `GITHUB_WEBHOOK_SECRET` in the gate's `.env`
   - Events: "Workflow runs"
2. Register the repository once (dashboard, or `POST /api/gate/repositories`),
   signed in as a user with access to it. The gate uses that user's stored
   GitHub token to read logs and publish the verdict.

Every finished Actions run now flows through the gate automatically, and the
verdict appears as a commit check and, on a pull request, as a comment.

## 3. Direct API call from any CI system (Jenkins, GitLab CI, CircleCI, ...)

```bash
curl -sS -X POST "$AUTOHEAL_GATE_URL/api/gate/check" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AUTOHEAL_GATE_API_KEY" \
  -d @- <<JSON
{
  "repository": "acme/payments-api",
  "commit_sha": "$CI_COMMIT_SHA",
  "branch": "$CI_COMMIT_BRANCH",
  "ci_passed": $([ "$BUILD_EXIT_CODE" = "0" ] && echo true || echo false),
  "failure_logs": $(jq -Rs . < build-output.log),
  "repo_path": "$CI_PROJECT_DIR",
  "test_command": "pytest -q"
}
JSON
```

A non-2xx/409 response, or a `"verdict": "BLOCK"` / `"HOLD"` in the body,
should fail your pipeline step. `"verdict": "PASS"` means continue normally.

## Choosing a policy

Start from the default policy (`GET /api/gate/policy/default`), then either:

- override per repository from the dashboard's **Repositories** page, or
- commit an `.autoheal/policy.yml` file to the repository itself (see
  `sample-repo/.autoheal/policy.yml` for a working example) so policy changes
  are reviewed alongside the code they govern.

Use `POST /api/gate/policy/simulate` to check what a policy decides for a
hypothetical result before wiring it into a real pipeline.
