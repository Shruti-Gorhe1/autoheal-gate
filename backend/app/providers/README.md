# `app/providers/`

Two independent interfaces, because "where the code lives" and "where the
pipeline runs" are genuinely two different questions:

- **`SourceProvider`** -- reads/writes the repository: diffs, file content,
  a downloadable snapshot at a commit, commit statuses, PR comments, PR
  creation.
- **`CICDProvider`** -- reads the pipeline: right now, just the failed
  run's build/test logs (`get_failure_logs`), plus `normalize_webhook` for
  turning a native webhook payload into a `PipelineEvent`.

`PipelineEvent` carries `source_provider` and `cicd_provider` as separate
fields specifically so a GitHub repository built by Azure Pipelines (or any
other cross-provider combination) is representable -- see
`PipelineEvent.is_cross_provider`. `provider` remains as a single-value
shorthand for the common case where both are the same, and both fields
auto-derive from it when only `provider` is given.

## What's here

- **`base.py`** -- `PipelineEvent`, `ProviderCapabilities`, `SourceProvider`, `CICDProvider`.
- **`github.py`** -- `GitHubProvider(SourceProvider, CICDProvider)` with an explicit capability set.
- **`azure.py`** -- `AzureProvider(SourceProvider, CICDProvider)` for Azure Repos and Azure Pipelines.
- **`google_cloud_build.py`** -- `GoogleCloudBuildProvider(CICDProvider)` for Google Cloud Build. It intentionally does not claim source-control capabilities.
- **`aws.py`** -- `AWSProvider(CICDProvider)` for AWS CodeBuild and CodePipeline. It intentionally advertises only failure-log retrieval.
- **`../services/google_cloud_build_client.py`** -- small REST client for Cloud Build build lookup and Cloud Storage log retrieval.
- **`../services/aws_cicd_client.py`** -- boto3 client for CodeBuild/CloudWatch Logs and CodePipeline execution lookup.
- **`resolver.py`** -- `ProviderResolver`, which resolves source and CI/CD providers independently. GitHub and Azure DevOps are registered from Phases 2-3, and Google Cloud Build is registered as a CI/CD provider in Phase 4, and AWS CodeBuild/CodePipeline in Phase 5. GitHub
  is, today, the only provider that is both the source of the code and the
  CI/CD system that built it, so one class implements both interfaces by
  delegating to the existing, already-tested
  `app.services.github_client.GitHubClient`. This file adds no new GitHub
  API behavior, it only exposes the existing behavior through the
  provider-neutral shape.

## Why `CICDProvider` looks thin

It has exactly one method (`get_failure_logs`) plus `normalize_webhook`,
not the fuller "get_pipeline / get_jobs / rerun_pipeline / get_artifacts"
surface a CI/CD API could support. That's deliberate: nothing in this
codebase calls those yet. Adding an unused method to satisfy a checklist
produces an untested, unverifiable claim of a capability -- exactly what
this project's own rules prohibit. When a real caller needs one of those
(a "rerun the pipeline" button in the dashboard, say), it gets added to the
interface at that point, with a real implementation and a real test behind
it.

## What's NOT implemented yet

**No second provider exists.** Azure Pipelines, Google Cloud Build, and AWS
CodePipeline/CodeBuild are not implemented -- there is no `azure.py`,
`gcp.py`, or `aws.py` in this package. Some Azure-specific configuration
keys exist in `app/config.py` (`azure_devops_pat`, `azure_webhook_username`,
etc.) as forward-declared plumbing; they are not yet read by any working
code path. Do not infer Azure support from their presence.

Adding a real provider means:

1. A concrete `SourceProvider` and/or `CICDProvider` subclass (one class
   can implement both, as `GitHubProvider` does, when the provider is both
   the source and the CI/CD system -- or two separate classes, when it's
   only one).
2. `normalize_webhook()` (or an equivalent event-ingestion path) turning
   the provider's native payload into a `PipelineEvent`, setting
   `source_provider`/`cicd_provider` explicitly when they differ.
3. A new webhook/ingestion route in `app/integrations/`, verifying that
   provider's own signature/auth scheme.
4. Tests using mocked provider responses -- see `tests/test_providers.py`
   for the pattern: delegation is proven by monkeypatching the underlying
   client call and asserting the provider invoked it correctly, not by
   re-testing API behavior a client library already covers.

No agent, no policy rule, and no gate-orchestration logic should need to
change to add a provider. If it does, something provider-specific leaked
into a layer meant to be provider-neutral -- that's a bug in the new
provider's integration, not a gap in this interface.

## Where the rest of the pipeline still isn't provider-neutral

Two places still branch on "is there a token" rather than "is there a
provider," kept that way for backward compatibility with code and tests
written before this abstraction existed:

- `AgentContext.github_token` (in `app/agents/base.py`) is the original,
  GitHub-only field. `AgentContext.provider` (typed as `SourceProvider`) is
  the new one; the fix and validation agents prefer `provider` when it's
  set and only fall back to `github_token` when it isn't. Once every caller
  sets `provider`, `github_token` can be removed.
- `AgentContext.github_token` (in `app/agents/base.py`) is still retained
  for backward compatibility. The pipeline also receives the provider-neutral
  `SourceProvider`; later phases can remove the legacy field once all callers
  use the provider abstraction.
- `app/integrations/webhooks.py` is intentionally GitHub-specific at the
  ingestion boundary because it verifies GitHub's signature and parses the
  GitHub `workflow_run` payload. It normalizes that payload into
  `PipelineEvent` before calling the provider-neutral gate service.

## Phase 6 boundary

Provider adapters remain responsible only for provider-specific source or CI/CD operations. `app/context/builder.py` owns cross-provider evidence assembly; agents should consume the resulting provider-neutral context rather than making provider-specific API calls.

## Security and capabilities (Phase 12)

Provider capability flags are treated as an allow-list of implemented
operations. Reporting/PR actions are skipped when a concrete provider does not
advertise the operation. Secrets must remain server-side and are redacted from
observability/audit payloads.
