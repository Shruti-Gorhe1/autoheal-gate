"""Azure DevOps provider adapters for Azure Repos and Azure Pipelines."""

from __future__ import annotations

from typing import Any

from app.providers.base import (
    CICDProvider,
    PipelineEvent,
    ProviderCapabilities,
    SourceProvider,
)
from app.services.azure_devops_client import AzureDevOpsClient, AzureDevOpsError


class AzureProvider(SourceProvider, CICDProvider):
    """Azure DevOps implementation.

    One concrete class is registered in both resolver dimensions because
    Azure DevOps exposes both source-control and pipeline APIs.  The resolver
    still returns independent SourceProvider/CICDProvider instances, so a
    future GitHub+Azure combination does not couple the two interfaces.
    """

    name = "azure"

    capabilities = ProviderCapabilities(
        get_commit_diff=True,
        get_file=True,
        download_snapshot=True,
        publish_status=True,
        publish_comment=True,
        create_fix_pull_request=True,
        get_failure_logs=True,
    )

    def __init__(self, token: str | None):
        self.token = token
        self._client = AzureDevOpsClient(token)

    @staticmethod
    def _org_project(event: PipelineEvent) -> tuple[str, str] | None:
        org = event.organization or (event.metadata or {}).get("azure_organization")
        project = event.project or (event.metadata or {}).get("azure_project")
        if not org or not project:
            return None
        return str(org), str(project)

    def _repo_id(self, event: PipelineEvent) -> str:
        return str((event.metadata or {}).get("repository_id") or event.repository)

    # ------------------------------------------------------------- source

    async def get_commit_diff(self, event: PipelineEvent) -> str:
        target = self._org_project(event)
        if not self.token or not target:
            return ""
        try:
            org, project = target
            return await self._client.get_commit_diff(
                org, project, self._repo_id(event), event.commit_sha
            )
        except AzureDevOpsError:
            return ""

    async def get_file(
        self, event: PipelineEvent, path: str, ref: str | None = None
    ) -> str | None:
        target = self._org_project(event)
        if not self.token or not target:
            return None
        try:
            org, project = target
            return await self._client.get_file(
                org, project, self._repo_id(event), path, ref or event.commit_sha
            )
        except AzureDevOpsError:
            return None

    def get_file_sync(self, repository: str, commit_sha: str, path: str) -> str | None:
        # This synchronous interface has no PipelineEvent, so the required
        # Azure organization/project/repository identifiers are accepted via
        # the repository string "organization/project/repository".
        parts = repository.split("/", 2)
        if len(parts) != 3:
            return None
        organization, project, repo_id = parts
        return self._client.get_file_sync(
            organization, project, repo_id, path, commit_sha
        )

    def download_snapshot_sync(
        self, repository: str, commit_sha: str, dest_dir: str
    ) -> str | None:
        parts = repository.split("/", 2)
        if len(parts) != 3:
            return None
        organization, project, repo_id = parts
        return self._client.download_snapshot_sync(
            organization, project, repo_id, commit_sha, dest_dir
        )

    # ------------------------------------------------------------- reporting

    async def publish_status(
        self,
        event: PipelineEvent,
        *,
        verdict: str,
        summary: str,
        detail: str = "",
        details_url: str | None = None,
    ) -> None:
        target = self._org_project(event)
        if not self.token or not target:
            return
        state = {"PASS": "succeeded", "BLOCK": "failed", "HOLD": "pending"}.get(
            verdict, "pending"
        )
        try:
            org, project = target
            await self._client.publish_status(
                org,
                project,
                self._repo_id(event),
                event.commit_sha,
                state=state,
                description=summary,
                target_url=details_url,
            )
        except AzureDevOpsError:
            # Reporting must never turn a completed gate evaluation into a
            # failed gate run.
            return

    async def publish_comment(self, event: PipelineEvent, body: str) -> None:
        target = self._org_project(event)
        if not self.token or not target or not event.change_request_number:
            return
        try:
            org, project = target
            await self._client.publish_pr_comment(
                org,
                project,
                self._repo_id(event),
                event.change_request_number,
                body,
            )
        except AzureDevOpsError:
            return

    async def create_fix_pull_request(
        self, event: PipelineEvent, *, patch: str, title: str, body: str
    ) -> dict[str, Any]:
        target = self._org_project(event)
        if not self.token or not target:
            return {"created": False, "reason": "No Azure DevOps credentials are available."}
        if not patch:
            return {"created": False, "reason": "No patch was supplied."}

        try:
            org, project = target
            repo_id = self._repo_id(event)
            repository = await self._client.get_repository(org, project, repo_id)
            default_branch = repository.get("defaultBranch", "refs/heads/main")
            branch = f"autoheal/fix-{event.run_id or event.commit_sha[:8]}"
            changes = _apply_patch_to_sources(patch)
            if not changes:
                return {"created": False, "reason": "Patch did not contain writable file contents."}

            push = await self._client.create_commit(
                org,
                project,
                repo_id,
                branch=branch,
                base_sha=event.commit_sha,
                changes=changes,
                message=title,
            )
            pr = await self._client.create_pull_request(
                org,
                project,
                repo_id,
                source_branch=branch,
                target_branch=default_branch,
                title=title,
                description=body,
            )
            return {
                "created": True,
                "branch": branch,
                "files": list(changes),
                "pull_request_url": pr.get("url"),
                "number": pr.get("pullRequestId"),
                "commit": push.get("commits", [{}])[-1].get("commitId"),
            }
        except AzureDevOpsError as exc:
            return {"created": False, "reason": str(exc)}

    # ------------------------------------------------------------- webhook

    @staticmethod
    def normalize_webhook(payload: dict[str, Any]) -> PipelineEvent | None:
        """Normalize Azure DevOps ``build.complete`` service-hook payloads.

        The source repository is inferred from Azure's repository type:
        Azure Repos -> ``azure``; GitHub-backed Azure Pipelines -> ``github``.
        The CI/CD side is always ``azure``.
        """
        if str(payload.get("eventType", "")).lower() not in {
            "build.complete",
            "build.completed",
        }:
            return None

        build = payload.get("resource") or {}
        if not build or build.get("id") is None:
            return None

        repository = build.get("repository") or {}
        project = build.get("project") or {}
        repo_type = str(repository.get("type") or "").lower()
        repo_name = str(repository.get("name") or repository.get("id") or "")
        if not repo_name:
            return None

        organization = _organization_from_payload(payload, build, repository)

        if "github" in repo_type:
            source_provider = "github"
            source_repository = (
                repository.get("properties", {}).get("fullName")
                or repository.get("name")
                or repo_name
            )
        else:
            source_provider = "azure"
            project_name = str(project.get("name") or project.get("id") or "")
            # The synchronous SourceProvider contract receives only the
            # repository string. Keep the Azure org/project/repository
            # identity recoverable there while also exposing the structured
            # values separately on PipelineEvent.
            parts = [p for p in (organization, project_name, repo_name) if p]
            source_repository = "/".join(parts)

        status = str(build.get("status") or "").lower()
        result = str(build.get("result") or "").lower()
        if result in {"succeeded"}:
            normalized_status = "success"
        elif result in {"failed", "partiallysucceeded", "partially_succeeded"}:
            normalized_status = "failure"
        elif result in {"canceled", "cancelled", "stopped"}:
            normalized_status = "cancelled"
        elif status == "completed":
            normalized_status = "unknown"
        else:
            return None

        branch = str(build.get("sourceBranch") or "")
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]
        elif branch.startswith("refs/pull/"):
            branch = branch[len("refs/pull/"):].split("/", 1)[0]

        change_request = _pull_request_number(build)

        definition = build.get("definition") or {}
        metadata = {
            "build_number": build.get("buildNumber"),
            "build_uri": build.get("uri"),
            "repository_id": repository.get("id"),
            "repository_type": repository.get("type"),
            "repository_url": repository.get("url"),
            "definition_name": definition.get("name"),
            "definition_id": definition.get("id"),
            "reason": build.get("reason"),
            "result": build.get("result"),
            "azure_organization": organization,
            "azure_project": project.get("name") or project.get("id"),
            "source_repository_type": repo_type,
            "pull_request_id": change_request,
        }

        return PipelineEvent(
            provider="azure",
            source_provider=source_provider,
            cicd_provider="azure",
            repository=source_repository,
            commit_sha=str(build.get("sourceVersion") or ""),
            branch=branch or None,
            pipeline_id=str(definition["id"]) if definition.get("id") is not None else None,
            run_id=str(build["id"]),
            change_request_number=change_request,
            status=normalized_status,
            organization=organization,
            project=str(project.get("name") or project.get("id") or ""),
            metadata=metadata,
            raw_payload=payload,
        )

    async def get_failure_logs(self, event: PipelineEvent) -> str:
        target = self._org_project(event)
        if not self.token or not target or not event.run_id:
            return ""
        try:
            org, project = target
            return await self._client.get_failure_logs(org, project, event.run_id)
        except AzureDevOpsError as exc:
            return f"Could not download Azure Pipelines logs: {exc}"


def _pull_request_number(build: dict[str, Any]) -> int | None:
    trigger = build.get("triggerInfo") or {}
    for key in ("pr.number", "pullRequestId", "pullrequestid"):
        value = trigger.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    source_version = str(build.get("sourceBranch") or "")
    if source_version.startswith("refs/pull/"):
        try:
            return int(source_version.split("/")[2])
        except (ValueError, IndexError):
            return None
    return None


def _organization_from_payload(
    payload: dict[str, Any],
    build: dict[str, Any],
    repository: dict[str, Any],
) -> str | None:
    candidates = [
        (payload.get("resourceContainers") or {}).get("collection", {}).get("baseUrl"),
        repository.get("url"),
        build.get("url"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        text = str(candidate)
        marker = "dev.azure.com/"
        if marker in text:
            tail = text.split(marker, 1)[1]
            return tail.split("/", 1)[0]
        marker = ".visualstudio.com/"
        if marker in text:
            return text.split("://", 1)[-1].split(".visualstudio.com", 1)[0]
    return None


def _apply_patch_to_sources(patch: str) -> dict[str, str]:
    """Reconstruct post-patch file contents from full-context unified diffs."""
    files: dict[str, list[str]] = {}
    current: str | None = None

    for line in patch.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip().split("\t", 1)[0]
            if raw in ("/dev/null", "dev/null"):
                current = None
                continue
            current = raw[2:] if raw.startswith(("a/", "b/")) else raw
            files.setdefault(current, [])
        elif line.startswith(("--- ", "diff --git", "index ", "@@")):
            continue
        elif current is not None:
            if line.startswith("+"):
                files[current].append(line[1:])
            elif line.startswith(" "):
                files[current].append(line[1:])

    return {path: "\n".join(lines) + "\n" for path, lines in files.items() if lines}
