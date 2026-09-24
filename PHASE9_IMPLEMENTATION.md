# Phase 9 — Persistent Job State, Retries & Dead-Letter Queue

## Scope

Phase 9 extends the Phase 8 local queue/worker boundary without replacing it. Job execution state is now durable in SQLAlchemy, transient failures can retry, and jobs that exhaust their retry budget move to a durable dead-letter state.

## Added

- `JobRecord` SQLAlchemy model with:
  - job/provider/event correlation
  - JSON payload
  - lifecycle status
  - attempt count and retry limit
  - timestamps
  - last error
- Durable queue persistence through `LocalJobQueue`
- Worker state transitions: `QUEUED → RUNNING → COMPLETED`
- Failure path: `RUNNING → RETRYING → QUEUED`
- Terminal failure: `DEAD_LETTER`
- Recovery of queued/retrying/running jobs when the API process starts again
- Configurable `QUEUE_MAX_ATTEMPTS` and `QUEUE_RETRY_DELAY_SECONDS`
- Job inspection endpoints:
  - `GET /api/system/jobs`
  - `GET /api/system/jobs/{job_id}`
- Webhook jobs retain the Phase 7 idempotency/event key
- Existing GitHub, Azure, GCP and AWS webhook contracts are preserved

## Failure/recovery flow

```text
Webhook
   ↓
Event Ingestion + Idempotency
   ↓
Durable JobRecord (QUEUED)
   ↓
Worker
   ↓
RUNNING
   │
   ├── success → COMPLETED
   │
   └── error
        ↓
     RETRYING
        │
        ├── attempts remain → QUEUED → worker
        │
        └── retry limit reached → DEAD_LETTER
```

A worker cancellation does not mark a job successful. Its durable `RUNNING` state is recovered as `QUEUED` on the next process start, provided the provider handler is registered.

## Configuration

```env
QUEUE_MAX_SIZE=1000
QUEUE_WORKER_COUNT=2
QUEUE_MAX_ATTEMPTS=3
QUEUE_RETRY_DELAY_SECONDS=0.5
```

SQLite remains the default zero-cost development database. PostgreSQL can be used later for shared production state.

## Tests

Phase 9 added four focused job-state tests. The source tree collected **206 tests** in the implementation environment. Targeted Phase 9/provider/webhook tests passed **53/53**. The full suite was attempted but exceeded the available execution time in this environment; the package therefore does not claim a full-suite green result here.

The expected local verification is:

```bash
python -m pytest -q
```

The Phase 8 verified baseline was 202 tests, so Phase 9 should run 206 tests.
