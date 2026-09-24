"""Durable queue + worker pool for AutoHeal jobs.

Phase 9 keeps the free/local asyncio queue from Phase 8, but makes job state
persistent in SQLAlchemy so retries, status inspection and recovery survive a
worker exception or process restart. A future broker can implement the same
logical contract without changing the provider/webhook layer.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, Any

from sqlalchemy import and_, or_, select, update

from app.db.base import SessionLocal, init_db
from app.db.models import JobRecord, utcnow
from app.observability.telemetry import current_traceparent, traced_span

logger = logging.getLogger("autoheal.queue")
JobHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class QueueJob:
    job_id: str
    provider: str


class LocalJobQueue:
    """Bounded in-process workers backed by durable JobRecord rows."""

    def __init__(self, *, max_size: int = 1000, worker_count: int = 2,
                 max_attempts: int = 3, retry_delay_seconds: float = 0.1,
                 poll_interval_seconds: float = 0.25) -> None:
        if max_size < 1 or worker_count < 1 or max_attempts < 1:
            raise ValueError("queue size, worker count and max attempts must be positive")
        self.max_size = max_size
        self.worker_count = worker_count
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.poll_interval_seconds = max(0.05, poll_interval_seconds)
        self._queue: asyncio.Queue[QueueJob] = asyncio.Queue(maxsize=max_size)
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._stopping = False
        self._handlers: dict[str, JobHandler] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._local_job_ids: set[str] = set()

    @property
    def started(self) -> bool:
        return self._started and not self._stopping

    @property
    def worker_count_running(self) -> int:
        return sum(not worker.done() for worker in self._workers)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def register_handler(self, provider: str, handler: JobHandler) -> None:
        self._handlers[provider] = handler

    async def start(self) -> None:
        if self.started:
            return
        init_db()
        self._stopping = False
        self._queue = asyncio.Queue(maxsize=self.max_size)
        # Recover queued/retrying work. RUNNING jobs are also recovered because
        # a previous process may have died while executing them.
        with SessionLocal() as db:
            rows = db.scalars(
                select(JobRecord).where(JobRecord.status.in_(["QUEUED", "RETRYING", "RUNNING"]))
                .order_by(JobRecord.created_at)
            ).all()
            recoverable = [r for r in rows if r.provider in self._handlers]
            for row in recoverable:
                row.status = "QUEUED"
                row.started_at = None
                row.available_at = utcnow()
            db.commit()
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"autoheal-worker-{i}")
            for i in range(self.worker_count)
        ]
        self._started = True
        for row in recoverable:
            await self._enqueue_local(QueueJob(row.id, row.provider))
        # Every API/worker process polls the shared durable job table. This is
        # what lets multiple instances share work without requiring a paid
        # broker. The atomic claim in _process prevents duplicate execution.
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="autoheal-job-dispatcher")
        logger.info("Started durable local queue with %d workers", self.worker_count)

    async def stop(self, *, drain_timeout: float = 0.5) -> None:
        if not self._workers:
            self._started = False
            return
        self._stopping = True
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            logger.warning("Queue shutdown timeout; leaving unfinished jobs durable")
        if self._dispatcher:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._started = False
        self._stopping = False

    async def submit(self, *, provider: str, handler: JobHandler,
                     payload: dict[str, Any], job_id: str | None = None,
                     event_key: str | None = None, max_attempts: int | None = None) -> str:
        if not self.started:
            raise RuntimeError("Job queue is not started")
        self.register_handler(provider, handler)
        jid = job_id or f"job_{uuid.uuid4().hex[:16]}"
        attempts_limit = max_attempts or self.max_attempts
        traceparent = current_traceparent()
        stored_payload = dict(payload)
        if traceparent:
            stored_payload["_autoheal_traceparent"] = traceparent
        with traced_span(
            "queue.submit",
            provider=provider,
            **{"job.id": jid, "event.id": event_key},
        ):
            with SessionLocal() as db:
                existing = db.get(JobRecord, jid)
                if existing is None:
                    db.add(JobRecord(
                        id=jid, provider=provider, event_key=event_key, payload=stored_payload,
                        status="QUEUED", max_attempts=attempts_limit, available_at=utcnow(),
                    ))
                    db.commit()
                elif existing.status in {"COMPLETED", "DEAD_LETTER"}:
                    return jid
            await self._enqueue_local(QueueJob(jid, provider))
        logger.info("Queued job %s provider=%s pending=%d", jid, provider, self.pending)
        return jid

    async def wait_for_idle(self) -> None:
        await self._queue.join()

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job, worker_index)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker %d could not process job %s", worker_index, job.job_id)
            finally:
                self._local_job_ids.discard(job.job_id)
                self._queue.task_done()

    async def _process(self, job: QueueJob, worker_index: int) -> None:
        with SessionLocal() as db:
            row = db.get(JobRecord, job.job_id)
            if row is None or row.status in {"COMPLETED", "DEAD_LETTER"}:
                return
            handler = self._handlers.get(row.provider)
            if handler is None:
                row.status = "DEAD_LETTER"
                row.last_error = f"No handler registered for provider {row.provider}"
                row.failed_at = utcnow()
                db.commit()
                return
            now = utcnow()
            # Multiple worker processes may see the same durable job. Claim it
            # with a conditional UPDATE so only one process can execute it.
            result = db.execute(
                update(JobRecord)
                .execution_options(synchronize_session=False)
                .where(
                    JobRecord.id == job.job_id,
                    JobRecord.status == "QUEUED",
                    JobRecord.available_at <= now,
                )
                .values(
                    status="RUNNING",
                    attempts=JobRecord.attempts + 1,
                    started_at=now,
                )
            )
            if result.rowcount != 1:
                db.rollback()
                return
            db.commit()
            db.refresh(row)
            payload = dict(row.payload)

        traceparent = payload.pop("_autoheal_traceparent", None)
        try:
            with traced_span(
                "worker.process",
                traceparent=traceparent,
                provider=row.provider,
                **{"job.id": job.job_id, "event.id": row.event_key, "worker.index": worker_index},
            ):
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            # Leave RUNNING durable; start() will recover it on the next process.
            raise
        except Exception as exc:
            await self._handle_failure(job.job_id, exc)
            return

        with SessionLocal() as db:
            row = db.get(JobRecord, job.job_id)
            if row:
                row.status = "COMPLETED"
                row.completed_at = utcnow()
                row.last_error = None
                db.commit()
        logger.info("Worker %d completed job %s", worker_index, job.job_id)

    async def _handle_failure(self, job_id: str, exc: Exception) -> None:
        with SessionLocal() as db:
            row = db.get(JobRecord, job_id)
            if row is None:
                return
            row.last_error = str(exc)[:4000]
            if row.attempts >= row.max_attempts:
                row.status = "DEAD_LETTER"
                row.failed_at = utcnow()
                db.commit()
                logger.error("Job %s moved to dead-letter after %d attempts", job_id, row.attempts)
                return
            row.status = "RETRYING"
            row.available_at = utcnow() + timedelta(seconds=self.retry_delay_seconds)
            db.commit()
            provider = row.provider
        await asyncio.sleep(self.retry_delay_seconds)
        with SessionLocal() as db:
            row = db.get(JobRecord, job_id)
            if row is None or row.status != "RETRYING":
                return
            row.status = "QUEUED"
            row.available_at = utcnow()
            db.commit()
        await self._enqueue_local(QueueJob(job_id, provider))

    async def _enqueue_local(self, job: QueueJob) -> None:
        self._local_job_ids.add(job.job_id)
        await self._queue.put(job)

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                await self._discover_durable_jobs()
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Durable job dispatcher failed")
                await asyncio.sleep(self.poll_interval_seconds)

    async def _discover_durable_jobs(self) -> None:
        now = utcnow()
        with SessionLocal() as db:
            rows = db.scalars(
                select(JobRecord)
                .where(
                    JobRecord.status.in_(["QUEUED", "RETRYING"]),
                    JobRecord.available_at <= now,
                )
                .order_by(JobRecord.created_at)
                .limit(self.max_size)
            ).all()
            candidates = [(row.id, row.provider) for row in rows if row.provider in self._handlers]
        for job_id, provider in candidates:
            if job_id in self._local_job_ids:
                continue
            if self.pending >= self.max_size:
                break
            await self._enqueue_local(QueueJob(job_id, provider))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with SessionLocal() as db:
            row = db.get(JobRecord, job_id)
            if row is None:
                return None
            return self._serialize(row)

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with SessionLocal() as db:
            rows = db.scalars(select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)).all()
            return [self._serialize(row) for row in rows]

    @staticmethod
    def _serialize(row: JobRecord) -> dict[str, Any]:
        return {
            "job_id": row.id, "provider": row.provider, "event_key": row.event_key,
            "status": row.status, "attempts": row.attempts, "max_attempts": row.max_attempts,
            "last_error": row.last_error, "created_at": row.created_at,
            "started_at": row.started_at, "completed_at": row.completed_at,
            "failed_at": row.failed_at,
        }


job_queue = LocalJobQueue()
