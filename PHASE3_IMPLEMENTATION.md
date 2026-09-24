# Phase 3 — Azure Repos + Azure Pipelines

## Scope

Phase 3 adds Azure DevOps as a real provider while preserving the provider-neutral
Gate Service and existing GitHub behavior.

### Added

- `AzureProvider` implementing `SourceProvider` and `CICDProvider`.
- `AzureDevOpsClient` using Azure DevOps REST API 7.1.
- Azure Repos source operations:
  - commit diff reconstruction
  - file retrieval
  - synchronous snapshot materialization
  - commit/status/PR operations
- Azure Pipelines failure-log retrieval.
- Azure `build.complete` webhook normalization.
- `/api/integrations/azure/webhook`.
- Optional Basic Auth verification for the Azure webhook.
- Independent Azure registration in `ProviderResolver`.
- Azure capability reporting.
- Phase 3 provider and webhook tests.

## Cross-provider event model

The normalized event can represent:

- Azure Repos + Azure Pipelines
- GitHub + Azure Pipelines
- Azure Repos + GitHub Actions
- GitHub + GitHub Actions

Only the first two require Azure implementation in this phase.

For a GitHub-backed Azure Pipeline webhook, the Azure adapter marks:

```text
source_provider = github
cicd_provider   = azure
```

and keeps the Azure organization/project/build identifiers in the normalized
event metadata.

## Authentication

Azure DevOps API calls use a server-side Personal Access Token:

```text
AZURE_DEVOPS_PAT
```

The token is never sent to the frontend or placed in the LLM context.

Azure service-hook requests can optionally use Basic Auth:

```text
AZURE_WEBHOOK_USERNAME
AZURE_WEBHOOK_PASSWORD
```

For local development, both may be left blank.

## Intentionally not changed

- LangGraph
- RAG
- agents
- queue/workers
- Context Builder
- GCP
- AWS
- frontend
- sandbox architecture
- persistent job-state redesign
- horizontal scaling

## Verification

Collected suite size in this build:

**162 tests collected**

Targeted provider/regression verification:

**53 passed**

The complete `pytest -q` suite was attempted in the available execution
environment but exceeded the execution time limit before completion. Therefore
this package does **not** claim a full-suite green result here.

Run from `backend` on the local Phase 2 environment:

```bash
pytest -q
```

The full suite must be green before treating Phase 3 as fully verified.
