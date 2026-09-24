"""Google Cloud Build CI/CD provider adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.providers.base import CICDProvider, PipelineEvent, ProviderCapabilities
from app.services.google_cloud_build_client import GoogleCloudBuildClient, GoogleCloudBuildError
from app.config import settings


class GoogleCloudBuildProvider(CICDProvider):
    """Cloud Build adapter.

    Cloud Build is registered only as a CI/CD provider in this phase. Source
    code remains independently resolved (for example GitHub + Cloud Build),
    which preserves the source/CI-CD separation introduced in Phases 1-3.
    """

    name = "gcp"
    capabilities = ProviderCapabilities(get_failure_logs=True)

    def __init__(self, token: str | None):
        self.token = token
        self._client = GoogleCloudBuildClient(
            token, project_id=settings.google_cloud_project, location=settings.google_cloud_location
        )

    async def get_failure_logs(self, event: PipelineEvent) -> str:
        if not event.run_id:
            return ""
        try:
            self._client.project_id = (
                (event.metadata or {}).get("gcp_project_id")
                or event.project
                or self._client.project_id
            )
            location = (event.metadata or {}).get("gcp_location") or settings.google_cloud_location
            build = await self._client.get_build(event.run_id, location=location)
            return await self._client.get_failure_logs(
                event.run_id,
                location=(event.metadata or {}).get("gcp_location") or settings.google_cloud_location,
                build=build,
            )
        except GoogleCloudBuildError as exc:
            return f"Could not download Google Cloud Build logs: {exc}"

    @staticmethod
    def normalize_webhook(payload: dict[str, Any]) -> PipelineEvent | None:
        build, build_id = GoogleCloudBuildClient.decode_pubsub_build(payload)
        if not build or not build_id:
            return None

        status = str(build.get("status") or (payload.get("message", {}).get("attributes", {}) or {}).get("status") or "").upper()
        if status == "SUCCESS":
            normalized = "success"
        elif status in {"CANCELLED", "CANCELED"}:
            normalized = "cancelled"
        elif status in {"FAILURE", "INTERNAL_ERROR", "TIMEOUT", "EXPIRED"}:
            normalized = "failure"
        else:
            normalized = "unknown"

        substitutions = build.get("substitutions") or {}
        source = build.get("source") or {}
        repo_source = source.get("repoSource") or {}
        connected = source.get("connectedRepository") or {}
        developer_connect = source.get("developerConnectConfig") or {}

        commit_sha = (
            substitutions.get("COMMIT_SHA")
            or substitutions.get("REVISION")
            or (build.get("sourceProvenance") or {}).get("resolvedRepoSource", {}).get("commitSha")
            or repo_source.get("commitSha")
            or ""
        )
        branch = (
            substitutions.get("BRANCH_NAME")
            or repo_source.get("branchName")
            or None
        )

        repository = _repository_identity(build, substitutions, repo_source, connected, developer_connect)
        if not repository or not commit_sha:
            return None

        source_provider = _source_provider(build, substitutions, repository)
        project_id = (
            build.get("projectId")
            or (build.get("name") or "").split("/projects/")[-1].split("/")[0]
            or None
        )
        location = _build_location(build)

        pull_request = _pull_request_number(build, substitutions)

        return PipelineEvent(
            provider="gcp",
            source_provider=source_provider,
            cicd_provider="gcp",
            repository=repository,
            commit_sha=str(commit_sha),
            branch=branch,
            pipeline_id=str(build.get("buildTriggerId")) if build.get("buildTriggerId") else None,
            run_id=str(build_id),
            change_request_number=pull_request,
            status=normalized,
            project=project_id,
            metadata={
                "gcp_project_id": project_id,
                "gcp_location": location,
                "build_id": str(build_id),
                "build_trigger_id": build.get("buildTriggerId"),
                "log_url": build.get("logUrl"),
                "source_repository": repository,
                "source_type": source_provider,
            },
            raw_payload=payload,
        )


def _repository_identity(
    build: dict[str, Any],
    substitutions: dict[str, Any],
    repo_source: dict[str, Any],
    connected: dict[str, Any],
    developer_connect: dict[str, Any],
) -> str | None:
    candidates = [
        substitutions.get("_REPO_NAME"),
        substitutions.get("REPO_NAME"),
        build.get("repository"),
        repo_source.get("repoName"),
        connected.get("repository"),
        developer_connect.get("gitRepositoryLink"),
    ]
    for value in candidates:
        normalized = _normalize_repository(str(value)) if value else None
        if normalized:
            return normalized
    return None


def _normalize_repository(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("projects/"):
        # A Cloud Build 2nd-gen resource is not itself a source repository
        # identity. Keep it in metadata rather than pretending it is GitHub.
        return None
    if value.startswith("git@"):
        value = value.split(":", 1)[-1]
    elif "://" in value:
        parsed = urlparse(value)
        value = parsed.path.lstrip("/")
    value = value.removesuffix(".git").strip("/")
    parts = value.split("/")
    if len(parts) == 2 and all(parts):
        return "/".join(parts)
    return None


def _source_provider(build: dict[str, Any], substitutions: dict[str, Any], repository: str) -> str:
    explicit = substitutions.get("_SOURCE_PROVIDER") or substitutions.get("SOURCE_PROVIDER")
    if explicit:
        return str(explicit).strip().lower()
    if repository.count("/") == 1 and (
        "github" in str(build.get("logUrl", "")).lower()
        or substitutions.get("_GITHUB_REPO")
        or substitutions.get("GITHUB_REPO")
    ):
        return "github"
    # Cloud Build does not expose a universal SCM provider field across all
    # trigger/source generations. GitHub is the supported cross-provider case
    # in this phase; callers can explicitly set _SOURCE_PROVIDER for others.
    return "github"


def _build_location(build: dict[str, Any]) -> str:
    name = str(build.get("name") or "")
    marker = "/locations/"
    if marker in name:
        return name.split(marker, 1)[1].split("/", 1)[0]
    return "global"


def _pull_request_number(build: dict[str, Any], substitutions: dict[str, Any]) -> int | None:
    raw = substitutions.get("_PR_NUMBER") or substitutions.get("PR_NUMBER")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
