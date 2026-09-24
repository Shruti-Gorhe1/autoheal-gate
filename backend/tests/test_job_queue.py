from __future__ import annotations

import asyncio

import pytest

from app.db.base import SessionLocal
from app.db.models import JobRecord
from app.services.job_queue import LocalJobQueue


@pytest.mark.asyncio
async def test_queue_dispatches_job_to_worker():
    queue = LocalJobQueue(worker_count=1)
    seen = []

    async def handler(payload):
        seen.append(payload["value"])

    await queue.start()
    try:
        job_id = await queue.submit(provider="github", handler=handler, payload={"value": 7})
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    assert job_id.startswith("job_")
    assert seen == [7]


@pytest.mark.asyncio
async def test_multiple_workers_process_jobs_concurrently():
    queue = LocalJobQueue(worker_count=2)
    started = 0
    lock = asyncio.Lock()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_payload):
        nonlocal started
        async with lock:
            started += 1
            if started == 2:
                both_started.set()
        await release.wait()

    await queue.start()
    try:
        await queue.submit(provider="test", handler=handler, payload={"n": 1})
        await queue.submit(provider="test", handler=handler, payload={"n": 2})
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        assert queue.worker_count_running == 2
        release.set()
        await queue.wait_for_idle()
    finally:
        if not release.is_set():
            release.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_worker_failure_does_not_stop_other_jobs():
    queue = LocalJobQueue(worker_count=1)
    seen = []

    async def handler(payload):
        if payload["fail"]:
            raise RuntimeError("expected test failure")
        seen.append(payload["value"])

    await queue.start()
    try:
        await queue.submit(provider="test", handler=handler, payload={"fail": True, "value": 1})
        await queue.submit(provider="test", handler=handler, payload={"fail": False, "value": 2})
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    assert seen == [2]

@pytest.mark.asyncio
async def test_two_queue_instances_claim_one_durable_job_once():
    """Multiple worker processes may see the same DB job, but only one claims it."""
    q1 = LocalJobQueue(worker_count=1, poll_interval_seconds=0.05)
    q2 = LocalJobQueue(worker_count=1, poll_interval_seconds=0.05)
    calls = 0
    lock = asyncio.Lock()

    async def handler(_payload):
        nonlocal calls
        async with lock:
            calls += 1
        await asyncio.sleep(0.02)

    await q1.start()
    await q2.start()
    try:
        job_id = "job_shared_claim"
        await q1.submit(provider="shared", handler=handler, payload={}, job_id=job_id)
        await q2.submit(provider="shared", handler=handler, payload={}, job_id=job_id)
        await asyncio.sleep(0.2)
        await asyncio.gather(q1.wait_for_idle(), q2.wait_for_idle())
        assert q1.get_job(job_id)["status"] == "COMPLETED"
        assert calls == 1
    finally:
        await q1.stop()
        await q2.stop()


@pytest.mark.asyncio
async def test_dispatcher_discovers_job_created_after_worker_start():
    queue = LocalJobQueue(worker_count=1, poll_interval_seconds=0.05)
    seen = []

    async def handler(payload):
        seen.append(payload["value"])

    await queue.start()
    try:
        with SessionLocal() as db:
            db.add(JobRecord(
                id="job_discovered_later",
                provider="discover",
                payload={"value": 99},
                status="QUEUED",
                max_attempts=2,
            ))
            db.commit()
        await asyncio.sleep(0.2)
        # The handler must be registered before the dispatcher can claim it.
        queue.register_handler("discover", handler)
        await asyncio.sleep(0.2)
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    assert seen == [99]
