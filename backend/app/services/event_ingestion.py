"""Persistent webhook/event idempotency for AutoHeal ingestion.

A webhook can be delivered more than once.  The ingestion boundary therefore
claims a stable event key before scheduling any gate work.  The unique
constraint in the database makes the claim atomic across multiple API
processes using the same database, unlike an in-memory set.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IngestedEvent, utcnow
from app.observability.telemetry import traced_span


def _stable_key(*parts: Any) -> str:
    normalized = "|".join("" if value is None else str(value) for value in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def event_key(
    *,
    provider: str,
    delivery_id: str | None = None,
    repository: str | None = None,
    run_id: str | None = None,
    commit_sha: str | None = None,
    event_type: str | None = None,
) -> str:
    """Build a stable, provider-scoped idempotency key.

    Explicit delivery/message IDs win because they identify the exact event.
    When a provider does not expose one, the terminal run identity is used.
    """
    if delivery_id:
        return _stable_key(provider, "delivery", delivery_id)
    return _stable_key(provider, "run", repository, run_id, commit_sha, event_type)


def claim_event(
    db: Session,
    *,
    provider: str,
    delivery_id: str | None = None,
    repository: str | None = None,
    run_id: str | None = None,
    commit_sha: str | None = None,
    event_type: str | None = None,
) -> tuple[bool, str]:
    """Atomically claim an event. Returns ``(claimed, key)``.

    The unique database constraint is the concurrency guard. If another
    request inserted the same key first, the duplicate is rejected and the
    transaction is rolled back without affecting unrelated work.
    """
    key = event_key(
        provider=provider,
        delivery_id=delivery_id,
        repository=repository,
        run_id=run_id,
        commit_sha=commit_sha,
        event_type=event_type,
    )
    with traced_span(
        "event.ingestion",
        **{"provider": provider, "event.id": key, "run.id": run_id, "event.type": event_type},
    ) as span:
        db.add(IngestedEvent(idempotency_key=key, provider=provider, delivery_id=delivery_id,
                             repository=repository, run_id=run_id, event_type=event_type,
                             received_at=utcnow()))
        try:
            db.flush()
            span.set_attribute("event.duplicate", False)
            return True, key
        except IntegrityError:
            db.rollback()
            span.set_attribute("event.duplicate", True)
            return False, key
