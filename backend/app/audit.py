"""Append-only audit trail.

Auditing must never break the request that triggered it, so failures here are
logged and swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent
from app.security.guards import redact_secrets

logger = logging.getLogger("autoheal.audit")


def record_audit(
    db: Session,
    *,
    action: str,
    actor: str = "system",
    target: str | None = None,
    outcome: str = "ok",
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        db.add(
            AuditEvent(
                action=action,
                actor=actor,
                target=target,
                outcome=outcome,
                detail=redact_secrets(detail or {}),
            )
        )
        db.commit()
    except Exception:  # pragma: no cover - auditing is best effort
        logger.exception("Could not write audit event %s", action)
        db.rollback()


def recent_events(db: Session, limit: int = 100) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
        ).all()
    )
