# Phase 7 — Event Ingestion + Idempotency

## Scope

Phase 7 adds a durable ingestion boundary so repeated CI/CD webhook deliveries do not trigger the same AutoHeal gate job more than once.

### Implemented

- Persistent `IngestedEvent` database table with a unique idempotency key.
- Provider-scoped stable event-key generation.
- Explicit delivery/message/event IDs are preferred when a provider supplies one.
- Fallback identity uses provider + repository + run/build ID + commit + event type.
- Atomic database claim using the unique constraint, so duplicate concurrent deliveries have one winner.
- GitHub, Azure DevOps, Google Cloud Build, and AWS EventBridge webhook endpoints claim events before scheduling background work.
- Duplicate deliveries return `accepted: false`, `duplicate: true` and do not schedule another gate run.
- Existing provider-specific normalization and Gate Service behavior remain unchanged.
- Test coverage for duplicate claims, provider isolation, and concurrent claims.

## Event identity

```text
Webhook
   |
   v
Authenticate / validate
   |
   v
Normalize enough identity
   |
   v
Idempotency claim (DB unique key)
   |
   +---- duplicate ---> stop
   |
   +---- new ----------> schedule gate work
```

The idempotency record is deliberately persisted rather than kept in process memory. This is required so multiple API processes can share the same deduplication boundary once the application is horizontally scaled.

## Provider behavior

| Provider | Preferred event identity | Fallback |
|---|---|---|
| GitHub | `X-GitHub-Delivery` | workflow run + repository/commit |
| Azure | payload event ID when present | build ID + repository/commit |
| GCP | Pub/Sub `messageId` | Cloud Build ID + repository/commit |
| AWS | EventBridge event `id` | build/execution ID + event type |

## Intentionally deferred

Phase 7 does **not** add a queue, worker pool, persistent job state machine, retries/DLQ, or sandbox isolation. Those remain later phases.

## Verification

The Phase 7 package was compiled successfully and the provider/webhook regression subset plus new idempotency tests pass. The complete suite should be run from `backend` in the user's Phase 6 environment before Phase 7 is marked fully verified.
