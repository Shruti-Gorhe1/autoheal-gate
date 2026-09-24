"""AWS CodeBuild/CodePipeline CI/CD provider adapter."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.providers.base import CICDProvider, PipelineEvent, ProviderCapabilities
from app.services.aws_cicd_client import AWSCICDClient, AWSCICDError, repository_from_url


class AWSProvider(CICDProvider):
    """CI/CD adapter for AWS CodeBuild and AWS CodePipeline.

    AWS is intentionally registered only on the CI/CD side in this phase.
    Repository/source access remains independently resolved to GitHub, Azure,
    or another source provider.
    """

    name = "aws"
    capabilities = ProviderCapabilities(get_failure_logs=True)

    def __init__(self, token: str | None):
        self.token = token
        self._client = AWSCICDClient(
            region=settings.aws_region,
            access_key_id=settings.aws_access_key_id,
            secret_access_key=settings.aws_secret_access_key,
            session_token=settings.aws_session_token,
        )

    async def get_failure_logs(self, event: PipelineEvent) -> str:
        try:
            service = str((event.metadata or {}).get("aws_service") or "codebuild").lower()
            if service == "codepipeline":
                build_id = (event.metadata or {}).get("codebuild_build_id")
                if not build_id:
                    return "AWS CodePipeline failure did not include a CodeBuild build ID; action-level logs remain provider-specific."
                build = self._client.get_build(str(build_id))
            else:
                build = self._client.get_build(str(event.run_id))
            return self._client.get_build_logs(build)
        except AWSCICDError as exc:
            return f"Could not download AWS CI/CD logs: {exc}"

    @staticmethod
    def normalize_webhook(payload: dict[str, Any]) -> PipelineEvent | None:
        detail = payload.get("detail") or {}
        detail_type = str(payload.get("detail-type") or payload.get("detailType") or "")
        source = str(payload.get("source") or "")
        if source not in {"aws.codebuild", "aws.codepipeline"}:
            return None

        if source == "aws.codebuild" or "CodeBuild" in detail_type:
            return _normalize_codebuild(payload, detail)
        if source == "aws.codepipeline" or "CodePipeline" in detail_type:
            return _normalize_codepipeline(payload, detail)
        return None


def _status(raw: str) -> str:
    value = raw.upper()
    if value in {"SUCCEEDED", "SUCCESS"}:
        return "success"
    if value in {"FAILED", "FAULT", "TIMED_OUT", "TIMEOUT", "CANCELED", "CANCELLED", "STOPPED", "SUPERSEDED"}:
        return "cancelled" if value in {"CANCELED", "CANCELLED", "STOPPED", "SUPERSEDED"} else "failure"
    return "unknown"


def _source_info(detail: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    additional = detail.get("additional-information") or detail.get("additionalInformation") or {}
    source = additional.get("source") or {}
    location = source.get("location") or detail.get("repository") or detail.get("repository-url")
    commit = (
        additional.get("sourceVersion")
        or detail.get("source-version")
        or detail.get("sourceVersion")
        or detail.get("commit-sha")
    )
    repository = repository_from_url(location)
    provider = str(detail.get("source-provider") or "").strip().lower() or None
    if not provider and repository:
        provider = "github" if repository.count("/") == 1 else None
    return repository, str(commit) if commit else None, provider


def _normalize_codebuild(payload: dict[str, Any], detail: dict[str, Any]) -> PipelineEvent | None:
    build_id = detail.get("build-id") or detail.get("buildId")
    if not build_id:
        return None
    repository, commit, source_provider = _source_info(detail)
    # Direct/local tests may provide normalized source fields. Real AWS events
    # can be hydrated through the build API by the webhook worker.
    repository = repository or str(detail.get("repository") or "")
    commit = commit or str(detail.get("commit-sha") or "")
    if not repository or not commit:
        return None
    return PipelineEvent(
        provider="aws",
        source_provider=source_provider or "github",
        cicd_provider="aws",
        repository=repository,
        commit_sha=commit,
        branch=detail.get("branch") or detail.get("branch-name"),
        pipeline_id=str(detail.get("project-name") or "") or None,
        run_id=str(build_id),
        status=_status(str(detail.get("build-status") or "")),
        metadata={
            "aws_service": "codebuild",
            "aws_region": payload.get("region") or settings.aws_region,
            "aws_project_name": detail.get("project-name"),
            "aws_build_id": build_id,
            "log_url": (detail.get("additional-information") or {}).get("logs", {}).get("deepLink"),
        },
        raw_payload=payload,
    )


def _normalize_codepipeline(payload: dict[str, Any], detail: dict[str, Any]) -> PipelineEvent | None:
    execution_id = detail.get("execution-id") or detail.get("executionId")
    pipeline_name = detail.get("pipeline")
    if not execution_id or not pipeline_name:
        return None
    trigger = detail.get("execution-trigger") or detail.get("executionTrigger") or {}
    repository = detail.get("repository") or trigger.get("full-repository-name")
    commit = detail.get("commit-id") or trigger.get("commit-id")
    revision_url = trigger.get("revision-url") or trigger.get("revisionUrl")
    if not commit and revision_url:
        commit = revision_url.rstrip("/").split("/")[-1]
    if not repository:
        repository = repository_from_url(revision_url)
    if not repository or not commit:
        return None
    source_provider = str(detail.get("source-provider") or trigger.get("provider-type") or "github").lower()
    return PipelineEvent(
        provider="aws",
        source_provider=source_provider,
        cicd_provider="aws",
        repository=str(repository),
        commit_sha=str(commit),
        branch=detail.get("branch"),
        pipeline_id=str(pipeline_name),
        run_id=str(execution_id),
        status=_status(str(detail.get("state") or "")),
        metadata={
            "aws_service": "codepipeline",
            "aws_region": payload.get("region") or settings.aws_region,
            "aws_pipeline_name": pipeline_name,
            "aws_execution_id": execution_id,
            "failed_actions": (payload.get("additionalAttributes") or {}).get("failedActions") or [],
        },
        raw_payload=payload,
    )
