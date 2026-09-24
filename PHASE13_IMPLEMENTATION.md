# Phase 13 — Cross-Provider Integration & Concurrency

Phase 13 validates the architecture introduced in Phases 1–12 without adding a new external broker or cloud dependency.

## Coverage

- Cross-provider resolver matrix:
  - GitHub source + GitHub Actions
  - GitHub source + Azure Pipelines
  - GitHub source + Google Cloud Build
  - GitHub source + AWS CodeBuild/CodePipeline
  - Azure Repos + Azure Pipelines
  - Azure Repos + Google Cloud Build
  - Azure Repos + AWS CodeBuild/CodePipeline
- Provider credential isolation: source and CI/CD credentials are resolved independently.
- `PipelineEvent` preserves `source_provider` and `cicd_provider` for cross-provider events.
- `ContextBuilder` consumes separate source and CI/CD evidence streams.
- Ten concurrent jobs are exercised through the durable queue with multiple workers and unique job workspaces.
- Network/cloud calls remain mocked at the test boundary, so the suite is deterministic and free to run locally.

## New runtime endpoint

`GET /api/system/providers`

Returns the source/CI/CD provider matrix and boolean capabilities only. No tokens, PATs, API keys, or provider client configuration are returned.

## Validation

The Phase 13 test module is `backend/tests/test_phase13_cross_provider.py`.

Run:

```bash
cd backend
python -m pytest -q tests/test_phase13_cross_provider.py
```
