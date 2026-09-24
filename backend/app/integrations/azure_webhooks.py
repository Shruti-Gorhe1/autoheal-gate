"""Azure DevOps service-hook receiver.

Azure DevOps build-completed service hooks POST their native payload here.
The provider owns payload normalization; the gate only sees PipelineEvent /
GateCheckRequest.
"""

from __future__ import annotations

import base64
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from app.audit import record_audit
from app.config import settings
from app.db.base import session_scope
from app.db.models import ApiKey, AuthSession, Repository
from app.auth.session_store import get_access_token
from app.security.crypto import decrypt
from app.gate import service
from app.gate.models import GateCheckRequest
from app.providers.azure import AzureProvider
from app.services.event_ingestion import claim_event
from app.services.job_queue import job_queue

logger = logging.getLogger("autoheal.azure_webhook")

router = APIRouter(prefix="/api/integrations/azure", tags=["Integrations"])


def _verify_basic_auth(authorization: str | None) -> bool:
    """Verify Azure service-hook Basic Auth when credentials are configured.

    For local development both configured values may be left blank. A
    production deployment should configure both values on the AutoHeal side
    and use the same credentials in the Azure DevOps service hook.
    """
    configured_user = settings.azure_webhook_username
    configured_password = settings.azure_webhook_password
    if not configured_user and not configured_password:
        return settings.app_env.lower() in {"local", "test", "development"}
    if not authorization or not authorization.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
        username, password = raw.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(username, configured_user) and hmac.compare_digest(
        password, configured_password
    )


def _github_token_for_repository(db, full_name: str) -> tuple[str | None, str | None, str]:
    """Find the registered owner's GitHub credential for a GitHub-backed Azure build."""
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

    return None, None, "azure-webhook"


async def process_build_completed(payload: dict) -> None:
    event = AzureProvider.normalize_webhook(payload)
    if event is None:
        return

    request = GateCheckRequest(
        repository=event.repository,
        commit_sha=event.commit_sha,
        branch=event.branch,
        workflow_run_id=int(event.run_id) if event.run_id and event.run_id.isdigit() else None,
        pipeline_run_id=event.run_id,
        pull_request_number=event.change_request_number,
        ci_passed=event.ci_passed,
        metadata=event.metadata,
        source_provider=event.source_provider or "azure",
        cicd_provider="azure",
    )

    with session_scope() as db:
        record_audit(
            db,
            action="webhook.processing",
            actor="azure",
            target=event.repository,
            detail={"event_type": payload.get("eventType"), "build_id": event.run_id},
        )
        github_token = None
        if event.source_provider == "github":
            github_token, _, _ = _github_token_for_repository(db, event.repository)

        try:
            await service.run_gate(
                db,
                request,
                actor="azure-webhook",
                access_token=settings.azure_devops_pat,
                provider_tokens={
                    "azure": settings.azure_devops_pat,
                    "github": github_token,
                },
                source="webhook",
            )
        except Exception:
            logger.exception("Azure webhook gate run failed for %s", event.repository)


@router.post("/webhook", summary="Azure DevOps build-completed webhook")
async def azure_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
):
    if not _verify_basic_auth(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The Azure DevOps webhook credentials did not verify.",
        )

    payload = await request.json()
    if str(payload.get("eventType", "")).lower() not in {"build.complete", "build.completed"}:
        return {"accepted": False, "reason": "Event is not a completed Azure build."}

    event = AzureProvider.normalize_webhook(payload)
    if event is None or not event.run_id:
        return {"accepted": False, "reason": "Payload is missing a completed build."}

    if not event.ci_passed and not event.commit_sha:
        return {"accepted": False, "reason": "Build has no source commit."}

    delivery_id = payload.get("id") or payload.get("eventId") or payload.get("subscriptionId")
    with session_scope() as db:
        claimed, event_key_value = claim_event(
            db, provider="azure", delivery_id=str(delivery_id) if delivery_id else None,
            repository=event.repository, run_id=event.run_id, commit_sha=event.commit_sha,
            event_type=str(payload.get("eventType") or "build.complete"),
        )
        if not claimed:
            return {"accepted": False, "duplicate": True, "reason": "Event was already accepted."}
        record_audit(db, action="webhook.received", actor="azure", target=event.repository,
                     detail={"event_type": payload.get("eventType"), "build_id": event.run_id})

    job_id = await job_queue.submit(provider="azure", handler=process_build_completed, payload=payload, event_key=event_key_value)
    return {
        "accepted": True,
        "repository": event.repository,
        "commit": event.commit_sha,
        "run_id": event.run_id,
        "source_provider": event.source_provider,
        "cicd_provider": event.cicd_provider,
        "job_id": job_id,
    }
