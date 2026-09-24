"""Persistent domain model.

Design notes:
  * A GitHub access token is never sent to the browser. It lives encrypted in
    ``AuthSession.access_token_encrypted`` and is looked up server-side using
    the opaque session id carried in an httpOnly cookie.
  * ``ApiKey`` exists because a CI runner cannot complete a browser OAuth
    redirect. A key is issued by a logged-in user and inherits that user's
    GitHub identity for server-side GitHub calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    """A GitHub identity that has logged into AutoHeal Gate."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    github_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    login: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class OAuthState(Base):
    """Single-use CSRF state for the GitHub authorization redirect."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    redirect_to: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthSession(Base):
    """Server-side session. The browser only ever holds ``id``."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    access_token_encrypted: Mapped[str] = mapped_column(Text)
    token_scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def active(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > utcnow()


class ApiKey(Base):
    """Machine credential for CI runners. Only the SHA-256 hash is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # GitHub token captured at issue time, so the key keeps working after the
    # issuing user's browser session expires.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="api_keys")

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class Repository(Base):
    """A repository under gate control, with its policy."""

    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("full_name", name="uq_repository_full_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    full_name: Mapped[str] = mapped_column(String(256), index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    default_branch: Mapped[str] = mapped_column(String(128), default="main")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    clone_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    test_command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    setup_command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    runs: Mapped[list["GateRun"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )


class GateRun(Base):
    """One gate evaluation of one commit."""

    __tablename__ = "gate_runs"
    __table_args__ = (
        Index("ix_gate_runs_repo_commit", "repository_full_name", "commit_sha"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    repository_id: Mapped[str | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    repository_full_name: Mapped[str] = mapped_column(String(256), index=True)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # queued | running | completed | error
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    # PASS | BLOCK | HOLD | PENDING
    verdict: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)

    remediation_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    fix_proposed: Mapped[bool] = mapped_column(Boolean, default=False)
    patch_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    tests_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    hitl_required: Mapped[bool] = mapped_column(Boolean, default=False)

    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    patch: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    policy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_evaluation: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts: Mapped[dict] = mapped_column(JSON, default=dict)

    triggered_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), default="api")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    repository: Mapped[Repository | None] = relationship(back_populates="runs")
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Approval(Base):
    """A human decision recorded against a held run."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("gate_runs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_login: Mapped[str] = mapped_column(String(128))
    # approve | reject
    decision: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[GateRun] = relationship(back_populates="approvals")


class IngestedEvent(Base):
    """Durable idempotency record for an accepted external event."""

    __tablename__ = "ingested_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ingested_event_key"),
        Index("ix_ingested_events_provider_run", "provider", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    delivery_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    repository: Mapped[str | None] = mapped_column(String(256), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class JobRecord(Base):
    """Durable execution record for queued AutoHeal work."""

    __tablename__ = "job_records"
    __table_args__ = (
        Index("ix_job_records_status_available", "status", "available_at"),
        Index("ix_job_records_provider_created", "provider", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    event_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class AuditEvent(Base):
    """Append-only record of everything the system did or was asked to do."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), default="ok")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
