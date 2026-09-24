# Phase 6 — Separate Source/CI-CD Retrieval + Context Builder

## Scope

Phase 6 introduces a provider-neutral Context Builder between provider resolution and the agent pipeline.

It separates:

- **Source retrieval** — repository commit/diff evidence from `SourceProvider`.
- **CI/CD retrieval** — failed-run/build logs from `CICDProvider`.
- **Context assembly** — `ContextBuilder` combines the two into `FailureContext`.

The existing `collect_evidence()` function remains as a backward-compatible tuple wrapper.

## New components

- `backend/app/context/builder.py`
  - `FailureContext`
  - `ContextBuilder`
- `backend/app/context/__init__.py`

## Request compatibility

`GateCheckRequest` now has additive `pipeline_run_id: str | None` support. The existing integer `workflow_run_id` remains unchanged for GitHub callers.

This prevents GCP/AWS/Azure string build IDs from being lost before the Context Builder asks the CI/CD provider for logs.

All webhook integrations preserve `event.run_id` in `pipeline_run_id` while retaining the existing GitHub/Azure integer field where applicable.

## Gate flow

```text
GateCheckRequest
       |
       v
ProviderResolver
       |
       +--------------------+
       |                    |
       v                    v
SourceProvider         CICDProvider
       |                    |
       | commit diff        | failure logs
       +---------+----------+
                 |
                 v
          ContextBuilder
                 |
                 v
          FailureContext
                 |
                 v
          Existing Agents
                 |
                 v
          Policy / Verdict
```

## Deliberately not changed

- No queue
- No workers
- No Redis
- No persistent job-state redesign
- No horizontal scaling
- No sandbox redesign
- No frontend redesign
- No LangGraph redesign

## Validation

Phase 6 targeted validation in the implementation environment:

```text
63 passed
compileall: COMPILE_OK
```

The complete suite was collected/run attempt in the implementation environment, but the execution exceeded the available tool time limit before completion. The package therefore does **not** claim a full-suite green result here.

Run locally from `backend`:

```cmd
python -m pip install -r requirements.txt
python -m pytest -q
```

The Phase 5 verified baseline was 190 tests. Phase 6 adds the Context Builder tests, so the expected collected count is higher than 190.
