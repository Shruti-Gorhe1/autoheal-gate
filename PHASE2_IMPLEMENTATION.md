# Phase 2 — Provider Resolver + PipelineEvent Hardening

## Scope

Implemented only Phase 2. No Azure, GCP, AWS, queue, worker, context-builder, horizontal-scaling, frontend, sandbox, or LangGraph redesign work was added.

## Inspection findings

- `PipelineEvent` already represented `source_provider` and `cicd_provider` independently and preserved a backward-compatible `provider` shorthand.
- `GitHubProvider` already implemented both `SourceProvider` and `CICDProvider` by delegating to the existing `GitHubClient`.
- `Gate Service` was already free of direct `GitHubClient` imports and already resolved providers through the existing resolver function.
- The existing resolver function contained GitHub-only dispatch but did not expose an explicit resolver object or capability model.
- The GitHub webhook remains intentionally provider-specific at the ingestion boundary and normalizes into `PipelineEvent` before invoking the provider-neutral gate service.

## Changes

### 1. ProviderCapabilities

Added `ProviderCapabilities` to `app/providers/base.py`.

It describes only operations actually implemented by a provider:

- `get_commit_diff`
- `get_file`
- `download_snapshot`
- `publish_status`
- `publish_comment`
- `create_fix_pull_request`
- `get_failure_logs`

`GitHubProvider` advertises all seven because all seven are implemented and tested.

### 2. ProviderResolver

Added `ProviderResolver` to `app/providers/resolver.py` with independent registries:

- source provider registry
- CI/CD provider registry

Only `github` is registered in Phase 2.

The existing `resolve_providers()` function remains as a compatibility wrapper.

### 3. Gate Service

`app/gate/service.py` now owns a `ProviderResolver` instance and calls:

`provider_resolver.resolve(source_provider=..., cicd_provider=...)`

The Gate Service still does not import or construct `GitHubClient`.

### 4. Tests

Added tests for:

- explicit provider capability reporting
- cross-provider `PipelineEvent` combinations
- resolver object source resolution
- resolver object CI/CD resolution
- unsupported CI/CD provider handling
- Gate Service resolver integration

## Verification

### Phase 2 targeted tests

`tests/test_providers.py` + `tests/test_provider_gate_decoupling.py`:

**30 passed**

### Syntax compilation

`python -m compileall -q backend/app backend/tests`:

**passed**

### Full suite

The full suite was attempted in this execution environment but timed out before completion. Therefore no full-suite green result is claimed here.

The user's verified Phase 1 baseline immediately before Phase 2 was:

**144 passed**

After extracting this Phase 2 build, the user should run the full suite locally and confirm that the 144-test baseline remains green plus the new Phase 2 tests.
