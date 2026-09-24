# Phase 8 — Queue + Workers

Phase 8 inserts a provider-neutral queue boundary between webhook ingestion/idempotency and AutoHeal execution.

## Scope

- Local/free `asyncio.Queue` implementation (`app/services/job_queue.py`).
- Configurable worker pool (`QUEUE_WORKER_COUNT`, `QUEUE_MAX_SIZE`).
- GitHub, Azure, GCP and AWS webhooks enqueue accepted events instead of using FastAPI `BackgroundTasks`.
- Workers execute the existing provider-specific processing handlers; the Gate Service and LangGraph pipeline are unchanged.
- Queue job IDs are returned to webhook callers for correlation.
- Worker failures are isolated and logged; they do not terminate the worker pool.

## Deliberate non-goals

No Redis/external broker, persistent job-state redesign, retry/DLQ, sandbox redesign, horizontal scaling, or frontend changes are introduced in this phase. Those belong to later phases.

## Runtime

The FastAPI lifespan starts the configured local worker pool and drains queued work during shutdown. The queue interface is intentionally small so a durable broker can replace the local implementation in a later scaling phase.
