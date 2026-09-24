# Phase 11 — Observability with Phoenix / OpenTelemetry

## Scope

Phase 11 adds end-to-end tracing around the existing AutoHeal execution flow
without changing provider, queue, worker, LangGraph, or frontend contracts.

## Trace flow

```text
Webhook / Event Ingestion
        ↓
    Queue Submit
        ↓
     Worker
        ↓
 Context Builder
   ↙           ↘
Source        CI/CD retrieval
        ↓
 Existing Agent spans
        ↓
 Sandbox / Validation
        ↓
 Gate result
```

## Correlation

The implementation records `job.id`, `event.id`, provider, pipeline/run IDs,
and carries a W3C `traceparent` through the durable queue payload. A worker
continues the originating trace when OpenTelemetry is enabled.

## Phoenix

Tracing remains disabled by default. For local development:

```cmd
python -m pip install -r requirements-observability.txt
python -m phoenix.server.main serve
```

Then set:

```env
PHOENIX_ENABLED=true
PHOENIX_PROJECT_NAME=autoheal-gate
PHOENIX_ENDPOINT=http://localhost:6006/v1/traces
```

Restart the backend and inspect traces at `http://localhost:6006`.

## Instrumented spans

- `event.ingestion`
- `queue.submit`
- `worker.process`
- `context.build`
- `context.source`
- `context.cicd`
- existing `triage`, `retrieval`, `root_cause`, `fix`, `validation`
- existing `llm-call`

## Failure behavior

Observability is non-blocking. If OpenTelemetry/Phoenix is unavailable,
AutoHeal continues using no-op spans. Trace export failures never change gate
or job behavior.

## Validation

Phase 11 adds dedicated observability tests while preserving the complete
Phase 10 regression suite. The expected full-suite count is 215 tests.
