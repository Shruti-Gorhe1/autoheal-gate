# AutoHeal Gate — Phase 1: GitHub Decoupling

## Scope completed

This phase completes the GitHub decoupling only. No Azure, Google Cloud, AWS, queue, worker, horizontal-scaling, or LangGraph rewrite work was added.

### Changes

- Kept `PipelineEvent`, `SourceProvider`, and `CICDProvider` as the provider-neutral contracts.
- Added `app/providers/resolver.py` to resolve source and CI/CD providers independently.
- Added `source_provider` and `cicd_provider` to `GateCheckRequest` with backward-compatible GitHub defaults.
- Webhook ingestion now carries the normalized event's independent provider identities into the gate request.
- Removed direct `GitHubClient` / `GitHubError` imports from `app/gate/service.py` and `app/gate/router.py`.
- Gate policy-file retrieval goes through `SourceProvider`.
- Failure-log retrieval goes through `CICDProvider`.
- Commit-diff retrieval goes through `SourceProvider`.
- Verdict publishing and PR comments go through `SourceProvider`.
- Approved fix PR creation goes through `SourceProvider`.
- Moved GitHub-specific patch-to-file reconstruction into the GitHub provider.
- Added focused provider/gate decoupling tests.
- Updated provider documentation.

## Verification

### Passed

- `pytest -q tests/test_providers.py tests/test_provider_gate_decoupling.py`
  - **25 passed**
- `pytest -q tests/test_providers.py tests/test_webhook.py tests/test_policy.py tests/test_auth.py`
  - **81 passed**
- `pytest -q -k 'not gate_api'`
  - **130 passed, 14 deselected**
- Python syntax compilation of all backend application modules passed.
- Static check confirms no `GitHubClient`, `GitHubError`, or `app.services.github_client` dependency remains under `backend/app/gate/`.

### Environment-limited verification

The full `tests/test_gate_api.py` suite could not be completed in this execution environment. The first four gate API tests passed after the provider refactor, but the fifth test exceeded the execution timeout. The environment is CI/Linux and does not provide the same local Windows/Ollama/GitHub setup as the development machine.

The source package therefore does **not** claim a full 138/138 test pass.

## Not implemented intentionally

- Azure Pipelines
- Google Cloud Build
- AWS CodePipeline / CodeBuild
- Queue / worker architecture
- Horizontal scaling
- Context Builder
- Idempotency / DLQ
- Additional provider adapters
- LangGraph workflow redesign
