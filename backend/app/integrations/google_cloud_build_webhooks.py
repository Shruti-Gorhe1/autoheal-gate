"""Google Cloud Build Pub/Sub push receiver."""

from __future__ import annotations

import base64
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from app.audit import record_audit
from app.auth.session_store import get_access_token
from app.config import settings
from app.db.base import session_scope
from app.db.models import ApiKey, AuthSession, Repository
from app.gate import service
from app.gate.models import GateCheckRequest
from app.providers.google_cloud_build import GoogleCloudBuildProvider
from app.services.event_ingestion import claim_event
from app.services.job_queue import job_queue
from app.security.crypto import decrypt

logger = logging.getLogger("autoheal.google_cloud_build_webhook")

router = APIRouter(prefix="/api/integrations/gcp", tags=["Integrations"])


def _verify_secret(value: str | None) -> bool:
    configured = settings.gcp_webhook_secret
    if not configured:
        return settings.app_env.lower() in {"local", "test", "development"}
    return bool(value) and hmac.compare_digest(value, configured)


def _github_token_for_repository(db, full_name: str) -> tuple[str | None, str | None, str]:
    repo = db.scalar(select(Repository).where(Repository.full_name == full_name))
    owner_id = repo.owner_user_id if repo else None

    query = select(AuthSession).where(AuthSession.revoked_at.is_(None))
    if owner_id:
        query = query.where(AuthSession.user_id == owner_id)
    for session in db.scalars(query.order_by(AuthSession.created_at.desc())).all():
        if session.active:
            try:
                return get_access_token(session), session.user_id, session.user.login
            except ValueError:
                continue

    key_query = select(ApiKey).where(ApiKey.revoked_at.is_(None))
    if owner_id:
        key_query = key_query.where(ApiKey.user_id == owner_id)
    for key in db.scalars(key_query.order_by(ApiKey.created_at.desc())).all():
        if key.access_token_encrypted:
            try:
                return decrypt(key.access_token_encrypted), key.user_id, key.user.login
            except ValueError:
                continue

    return None, None, "gcp-webhook"


async def process_build_notification(payload: dict) -> None:
    event = GoogleCloudBuildProvider.normalize_webhook(payload)
    if event is None:
        return

    request = GateCheckRequest(
        repository=event.repository,
        commit_sha=event.commit_sha,
        branch=event.branch,
        workflow_run_id=None,
        pull_request_number=event.change_request_number,
        ci_passed=event.ci_passed,
        metadata=event.metadata,
        source_provider=event.source_provider or "github",
        cicd_provider="gcp",
    )

    with session_scope() as db:
        record_audit(
            db,
            action="webhook.processing",
            actor="gcp",
            target=event.repository,
            detail={"build_id": event.run_id, "status": event.status},
        )

        github_token = None
        if event.source_provider == "github":
            github_token, _, _ = _github_token_for_repository(db, event.repository)

        try:
            await service.run_gate(
                db,
                request,
                actor="gcp-webhook",
                access_token=settings.google_cloud_access_token,
                provider_tokens={
                    "gcp": settings.google_cloud_access_token,
                    "github": github_token,
                    "azure": settings.azure_devops_pat,
                },
                source="webhook",
            )
        except Exception:
            logger.exception("GCP Cloud Build gate run failed for %s", event.repository)


@router.post("/webhook", summary="Google Cloud Build Pub/Sub push webhook")
async def google_cloud_build_webhook(
    request: Request,
    x_autoheal_gcp_secret: str | None = Header(default=None),
):
    if not _verify_secret(x_autoheal_gcp_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The Google Cloud Build webhook secret did not verify.",
        )

    payload = await request.json()
    event = GoogleCloudBuildProvider.normalize_webhook(payload)
    if event is None:
        return {"accepted": False, "reason": "Payload is not a supported completed Cloud Build event."}

    if event.status not in {"success", "failure", "cancelled"}:
        return {"accepted": False, "reason": "Cloud Build event is not a terminal build state."}

    message = payload.get("message") or {}
    delivery_id = message.get("messageId") or message.get("message_id")
    with session_scope() as db:
        claimed, event_key_value = claim_event(
            db, provider="gcp", delivery_id=str(delivery_id) if delivery_id else None,
            repository=event.repository, run_id=event.run_id, commit_sha=event.commit_sha,
            event_type="cloud-build.completed",
        )
        if not claimed:
            return {"accepted": False, "duplicate": True, "reason": "Event was already accepted."}
        record_audit(db, action="webhook.received", actor="gcp", target=event.repository,
                     detail={"build_id": event.run_id, "status": event.status})

    job_id = await job_queue.submit(provider="gcp", handler=process_build_notification, payload=payload, event_key=event_key_value)
    return {
        "accepted": True,
        "repository": event.repository,
        "commit": event.commit_sha,
        "run_id": event.run_id,
        "source_provider": event.source_provider,
        "cicd_provider": event.cicd_provider,
        "job_id": job_id,
    }
