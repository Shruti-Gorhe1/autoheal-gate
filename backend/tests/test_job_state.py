from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal, init_db
from app.db.models import JobRecord
from app.services.job_queue import LocalJobQueue


@pytest.mark.asyncio
async def test_job_state_persists_and_completes():
    init_db()
    queue = LocalJobQueue(worker_count=1, max_attempts=2)
    seen = []

    async def handler(payload):
        seen.append(payload["value"])

    await queue.start()
    try:
        job_id = await queue.submit(
            provider="test", handler=handler, payload={"value": 42}, event_key="evt-42"
        )
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    job = queue.get_job(job_id)
    assert job["status"] == "COMPLETED"
    assert job["attempts"] == 1
    assert job["event_key"] == "evt-42"
    assert seen == [42]


@pytest.mark.asyncio
async def test_failed_job_retries_then_completes():
    queue = LocalJobQueue(worker_count=1, max_attempts=3, retry_delay_seconds=0.01)
    attempts = 0

    async def handler(_payload):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("transient")

    await queue.start()
    try:
        job_id = await queue.submit(provider="test", handler=handler, payload={})
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    job = queue.get_job(job_id)
    assert job["status"] == "COMPLETED"
    assert job["attempts"] == 2
    assert job["last_error"] is None


@pytest.mark.asyncio
async def test_failed_job_moves_to_dead_letter_after_retry_limit():
    queue = LocalJobQueue(worker_count=1, max_attempts=2, retry_delay_seconds=0.01)

    async def handler(_payload):
        raise RuntimeError("permanent")

    await queue.start()
    try:
        job_id = await queue.submit(provider="test", handler=handler, payload={})
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    job = queue.get_job(job_id)
    assert job["status"] == "DEAD_LETTER"
    assert job["attempts"] == 2
    assert job["last_error"] == "permanent"


def test_job_record_is_really_persistent(db):
    row = JobRecord(
        id="job_persist_test", provider="test", payload={"x": 1},
        status="QUEUED", max_attempts=3,
    )
    db.add(row)
    db.commit()
    db.expire_all()
    loaded = db.scalar(select(JobRecord).where(JobRecord.id == "job_persist_test"))
    assert loaded is not None
    assert loaded.payload == {"x": 1}
