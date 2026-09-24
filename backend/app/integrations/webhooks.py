"""GitHub webhook receiver.

Signature is verified before the body is parsed. Work happens in a background
task so GitHub gets its acknowledgement inside the delivery timeout.
"""

from __future__ import annotations

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
from app.services.event_ingestion import claim_event
from app.services.job_queue import job_queue
from app.providers.github import GitHubProvider
from app.security.crypto import decrypt, verify_webhook_signature

logger = logging.getLogger("autoheal.webhook")

router = APIRouter(prefix="/api/integrations/github", tags=["Integrations"])


def _token_for_repository(db, full_name: str) -> tuple[str | None, str | None, str]:
    """Find a stored GitHub token able to act on this repository.

    Preference order: the registering owner's live session, then any API key
    that owner issued. Returns ``(token, user_id, actor)``.
    """
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

    return None, None, "webhook"


async def process_workflow_run(payload: dict) -> None:
    """Evaluate a finished GitHub Actions run through the gate.

    The GitHub-specific shape of the webhook payload is peeled off here, by
    the provider, before anything else sees it -- ``GateCheckRequest``
    downstream is built from a normalized :class:`PipelineEvent`, the same
    representation an Azure or GCP webhook would produce.
    """
    event = GitHubProvider.normalize_webhook(payload)
    if event is None:
        return

    request = GateCheckRequest(
        repository=event.repository,
        commit_sha=event.commit_sha,
        branch=event.branch,
        workflow_run_id=int(event.run_id) if event.run_id else None,
        pipeline_run_id=event.run_id,
        pull_request_number=event.change_request_number,
        ci_passed=event.ci_passed,
        metadata=event.metadata,
        source_provider=event.source_provider or event.provider,
        cicd_provider=event.cicd_provider or event.provider,
    )

    with session_scope() as db:
        token, user_id, actor = _token_for_repository(db, event.repository)
        if token is None:
            logger.warning(
                "No stored GitHub credential can act on %s. Sign in once, or "
                "register the repository, so the gate can read its logs.",
                event.repository,
            )
        try:
            await service.run_gate(
                db,
                request,
                actor=actor,
                user_id=user_id,
                access_token=token,
                source="webhook",
            )
        except Exception:
            logger.exception("Webhook gate run failed for %s", event.repository)


@router.post("/webhook", summary="GitHub webhook endpoint")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    body = await request.body()

    if not verify_webhook_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The webhook signature did not verify.",
        )

    if x_github_event == "ping":
        return {"pong": True}

    payload = await request.json()

    if x_github_event != "workflow_run":
        return {"accepted": False, "reason": f"Event '{x_github_event}' is not handled."}

    run = payload.get("workflow_run", {})
    if payload.get("action") != "completed" or run.get("status") != "completed":
        return {"accepted": False, "reason": "The workflow run has not finished yet."}

    # A gate run triggered by our own check would loop forever.
    if str(run.get("name", "")).lower().startswith("autoheal gate"):
        return {"accepted": False, "reason": "Ignoring the gate's own workflow."}

    repository = payload.get("repository", {}).get("full_name")
    with session_scope() as db:
        claimed, event_key_value = claim_event(
            db, provider="github", delivery_id=x_github_delivery,
            repository=repository, run_id=str(run.get("id")) if run.get("id") is not None else None,
            commit_sha=run.get("head_sha"), event_type=x_github_event,
        )
        if not claimed:
            return {"accepted": False, "duplicate": True, "reason": "Event was already accepted."}
        record_audit(db, action="webhook.processing", actor="github", target=repository,
                     detail={"event": x_github_event, "delivery": x_github_delivery})

    job_id = await job_queue.submit(provider="github", handler=process_workflow_run, payload=payload, event_key=event_key_value)
    return {
        "accepted": True,
        "repository": repository,
        "commit": run.get("head_sha"),
        "job_id": job_id,
    }
