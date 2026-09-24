"""AWS EventBridge webhook integration for CodeBuild and CodePipeline."""
from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from app.audit import record_audit
from app.auth.session_store import get_access_token
from app.config import settings
from app.db.base import session_scope
from app.db.models import ApiKey, AuthSession, Repository
from app.gate import service
from app.gate.models import GateCheckRequest
from app.providers.aws import AWSProvider
from app.services.aws_cicd_client import AWSCICDClient, AWSCICDError, repository_from_url
from app.security.crypto import decrypt
from app.services.event_ingestion import claim_event
from app.services.job_queue import job_queue

logger = logging.getLogger("autoheal.aws_webhook")
router = APIRouter(prefix="/api/integrations/aws", tags=["Integrations"])


def _verify_secret(value: str | None) -> bool:
    configured = settings.aws_webhook_secret
    if not configured:
        return settings.app_env.lower() in {"local", "test", "development"}
    return bool(value) and hmac.compare_digest(value, configured)


def _github_token_for_repository(db, full_name: str) -> str | None:
    repo = db.scalar(select(Repository).where(Repository.full_name == full_name))
    owner_id = repo.owner_user_id if repo else None
    query = select(AuthSession).where(AuthSession.revoked_at.is_(None))
    if owner_id:
        query = query.where(AuthSession.user_id == owner_id)
    for session in db.scalars(query.order_by(AuthSession.created_at.desc())).all():
        if session.active:
            try:
                return get_access_token(session)
            except ValueError:
                continue
    key_query = select(ApiKey).where(ApiKey.revoked_at.is_(None))
    if owner_id:
        key_query = key_query.where(ApiKey.user_id == owner_id)
    for key in db.scalars(key_query.order_by(ApiKey.created_at.desc())).all():
        if key.access_token_encrypted:
            try:
                return decrypt(key.access_token_encrypted)
            except ValueError:
                continue
    return None


def _hydrate_event(payload: dict[str, Any]):
    event = AWSProvider.normalize_webhook(payload)
    if event is not None:
        return event
    detail = payload.get("detail") or {}
    source = str(payload.get("source") or "")
    client = AWSCICDClient(region=payload.get("region") or settings.aws_region)

    if source == "aws.codebuild":
        build_id = detail.get("build-id") or detail.get("buildId")
        if not build_id:
            return None
        try:
            build = client.get_build(str(build_id))
        except AWSCICDError:
            return None
        source_info = build.get("source") or {}
        location = source_info.get("location")
        repository = repository_from_url(location)
        commit = build.get("resolvedSourceVersion") or build.get("sourceVersion")
        source_provider = "github" if repository else None
        if not repository or not commit:
            return None
        hydrated = dict(payload)
        hydrated["detail"] = {
            **detail,
            "repository": repository,
            "commit-sha": commit,
            "source-provider": source_provider,
            "branch": None,
        }
        return AWSProvider.normalize_webhook(hydrated)

    if source == "aws.codepipeline":
        pipeline = detail.get("pipeline")
        execution_id = detail.get("execution-id") or detail.get("executionId")
        if not pipeline or not execution_id:
            return None
        try:
            execution = client.get_pipeline_execution(str(pipeline), str(execution_id))
        except AWSCICDError:
            return None
        revisions = execution.get("artifactRevisions") or []
        revision = revisions[0] if revisions else {}
        revision_url = revision.get("revisionUrl")
        repository = repository_from_url(revision_url)
        commit = revision.get("revisionId")
        if not repository or not commit:
            return None
        hydrated = dict(payload)
        hydrated["detail"] = {
            **detail,
            "repository": repository,
            "commit-id": commit,
            "source-provider": "github" if "github" in str(revision_url).lower() else "github",
        }
        return AWSProvider.normalize_webhook(hydrated)
    return None


async def process_aws_event(payload: dict[str, Any]) -> None:
    event = _hydrate_event(payload)
    if event is None:
        logger.warning("AWS event could not be hydrated into a complete PipelineEvent")
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
        cicd_provider="aws",
    )
    with session_scope() as db:
        record_audit(db, action="webhook.processing", actor="aws", target=event.repository,
                     detail={"run_id": event.run_id, "status": event.status})
        github_token = _github_token_for_repository(db, event.repository) if event.source_provider == "github" else None
        try:
            await service.run_gate(
                db, request, actor="aws-webhook",
                provider_tokens={"aws": "configured", "github": github_token},
                access_token=None, source="webhook",
            )
        except Exception:
            logger.exception("AWS webhook gate run failed for %s", event.repository)


@router.post("/webhook", summary="AWS EventBridge CodeBuild/CodePipeline webhook")
async def aws_webhook(
    request: Request,
    x_autoheal_aws_secret: str | None = Header(default=None),
):
    if not _verify_secret(x_autoheal_aws_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="The AWS webhook secret did not verify.")
    payload = await request.json()
    detail_type = str(payload.get("detail-type") or payload.get("detailType") or "")
    source = str(payload.get("source") or "")
    if source not in {"aws.codebuild", "aws.codepipeline"}:
        return {"accepted": False, "reason": "Event is not an AWS CodeBuild/CodePipeline event."}
    if "State Change" not in detail_type:
        return {"accepted": False, "reason": "Event is not a supported AWS state-change event."}
    detail = payload.get("detail") or {}
    event = _hydrate_event(payload)
    run_id = None
    if source == "aws.codebuild":
        run_id = detail.get("build-id") or detail.get("buildId")
    else:
        run_id = detail.get("execution-id") or detail.get("executionId")
    if event is not None:
        run_id = event.run_id or run_id
        if event.status not in {"success", "failure", "cancelled"}:
            return {"accepted": False, "reason": "AWS event is not a terminal state."}

    if not run_id:
        return {"accepted": False, "reason": "AWS event is missing its build/execution identifier."}

    delivery_id = payload.get("id") or payload.get("eventId")
    repository = event.repository if event else None
    commit_sha = event.commit_sha if event else None
    with session_scope() as db:
        claimed, event_key_value = claim_event(
            db, provider="aws", delivery_id=str(delivery_id) if delivery_id else None,
            repository=repository, run_id=str(run_id), commit_sha=commit_sha,
            event_type=detail_type,
        )
        if not claimed:
            return {"accepted": False, "duplicate": True, "reason": "Event was already accepted."}
        record_audit(db, action="webhook.processing", actor="aws", target=repository,
                     detail={"run_id": str(run_id), "status": event.status if event else "pending"})

    job_id = await job_queue.submit(provider="aws", handler=process_aws_event, payload=payload, event_key=event_key_value)
    if event is None:
        return {"accepted": True, "hydrated": False, "cicd_provider": "aws", "run_id": str(run_id), "job_id": job_id}
    return {"accepted": True, "repository": event.repository, "commit": event.commit_sha,
            "run_id": event.run_id, "source_provider": event.source_provider,
            "cicd_provider": event.cicd_provider, "job_id": job_id}
