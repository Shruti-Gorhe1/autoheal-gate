"""Azure DevOps REST client used by the provider adapters.

The client is deliberately provider-specific.  The rest of AutoHeal only sees
SourceProvider/CICDProvider contracts.
"""

from __future__ import annotations

import base64
import difflib
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger("autoheal.azure")


class AzureDevOpsError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AzureDevOpsClient:
    """Small Azure DevOps REST client authenticated with a PAT.

    Azure DevOps accepts a PAT through HTTP Basic authentication with an empty
    username.  The organization and project are taken from the normalized
    PipelineEvent rather than global state, which is important when one
    AutoHeal instance handles several Azure DevOps organizations.
    """

    api_version = "7.1"

    def __init__(self, pat: str | None, base_url: str | None = None):
        self.pat = pat
        self.base = (base_url or settings.azure_devops_base_url).rstrip("/")

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self.pat:
            raw = base64.b64encode(f":{self.pat}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {raw}"
        return headers

    @staticmethod
    def _segment(value: str) -> str:
        return quote(str(value), safe="")

    def _url(self, organization: str, project: str, path: str) -> str:
        return (
            f"{self.base}/{self._segment(organization)}/{self._segment(project)}"
            f"/_apis/{path.lstrip('/')}"
        )

    async def _request(
        self,
        method: str,
        organization: str,
        project: str,
        path: str,
        *,
        timeout: float = 30.0,
        accept: str = "application/json",
        **kwargs: Any,
    ) -> httpx.Response:
        if not self.pat:
            raise AzureDevOpsError("No Azure DevOps PAT is configured.")

        params = dict(kwargs.pop("params", {}) or {})
        params.setdefault("api-version", self.api_version)
        url = self._url(organization, project, path)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.request(
                method, url, headers=self._headers(accept), params=params, **kwargs
            )
        if response.status_code >= 400:
            raise AzureDevOpsError(
                f"{method} {path} failed with HTTP {response.status_code}: "
                f"{response.text[:400]}",
                status_code=response.status_code,
            )
        return response

    def _repo_id(self, event: Any) -> str:
        return str((event.metadata or {}).get("repository_id") or event.repository)

    async def get_build(
        self, organization: str, project: str, build_id: int | str
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                organization,
                project,
                f"build/builds/{quote(str(build_id), safe='')}",
            )
        ).json()

    async def get_failure_logs(
        self, organization: str, project: str, build_id: int | str, max_chars: int = 200_000
    ) -> str:
        """Fetch useful build logs, preferring failed timeline records.

        Azure's build-log endpoints expose a build's log list and individual
        plain-text log files.  We use the list endpoint and then retrieve the
        individual logs; this follows the documented Build Logs API.
        """
        build_id = str(build_id)
        try:
            response = await self._request(
                "GET",
                organization,
                project,
                f"build/builds/{quote(build_id, safe='')}/logs",
                timeout=60.0,
            )
            entries = response.json().get("value", [])
        except AzureDevOpsError as exc:
            return f"Could not download Azure Pipelines logs: {exc}"

        if not entries:
            return ""

        # The logs endpoint does not always expose failure classification, so
        # take the latest logs first and cap the amount passed to the LLM.
        entries = sorted(entries, key=lambda x: x.get("id", 0), reverse=True)[:20]
        blocks: list[str] = []
        remaining = max_chars

        for entry in reversed(entries):
            if remaining <= 0:
                break
            log_id = entry.get("id")
            if log_id is None:
                continue
            try:
                response = await self._request(
                    "GET",
                    organization,
                    project,
                    f"build/builds/{quote(build_id, safe='')}/logs/{quote(str(log_id), safe='')}",
                    timeout=60.0,
                    accept="text/plain",
                )
                text = response.text
            except AzureDevOpsError as exc:
                text = f"(log {log_id} download failed: {exc})"
            block = f"===== AZURE LOG {log_id} =====\n{text}"
            blocks.append(block[:remaining])
            remaining -= len(block)

        return "\n\n".join(blocks)[-max_chars:]

    async def get_file(
        self,
        organization: str,
        project: str,
        repository_id: str,
        path: str,
        ref: str,
    ) -> str | None:
        path = "/" + path.lstrip("/")
        response = await self._request(
            "GET",
            organization,
            project,
            f"git/repositories/{quote(str(repository_id), safe='')}/items",
            params={
                "scopePath": path,
                "includeContent": "true",
                "versionDescriptor.version": ref,
                "versionDescriptor.versionType": "commit",
            },
            timeout=30.0,
        )
        payload = response.json()
        items = payload.get("value", [])
        item = items[0] if items else payload
        content = item.get("content")
        if content is None:
            return None
        return str(content)

    async def _list_items(
        self,
        organization: str,
        project: str,
        repository_id: str,
        ref: str,
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            organization,
            project,
            f"git/repositories/{quote(str(repository_id), safe='')}/items",
            params={
                "scopePath": "/",
                "recursionLevel": "Full",
                "includeContentMetadata": "true",
                "versionDescriptor.version": ref,
                "versionDescriptor.versionType": "commit",
            },
            timeout=60.0,
        )
        return response.json().get("value", [])

    async def get_commit_details(
        self, organization: str, project: str, repository_id: str, sha: str
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/commits/{quote(sha, safe='')}",
            )
        ).json()

    async def get_commit_diff(
        self, organization: str, project: str, repository_id: str, sha: str
    ) -> str:
        """Build a real unified diff from Azure's commit metadata + file APIs.

        Azure's commit-diff endpoint returns changed items rather than a
        GitHub-style textual patch.  We therefore retrieve the parent and
        current file contents and generate the textual diff locally.
        """
        details = await self.get_commit_details(organization, project, repository_id, sha)
        parents = details.get("parents") or []
        parent = parents[0] if parents else None

        response = await self._request(
            "GET",
            organization,
            project,
            f"git/repositories/{quote(str(repository_id), safe='')}/commits/{quote(sha, safe='')}/changes",
            params={"$top": 200},
            timeout=60.0,
        )
        changes = response.json().get("changes", [])
        if not changes:
            return ""

        blocks: list[str] = []
        for change in changes[:100]:
            item = change.get("item") or {}
            path = item.get("path")
            if not path or item.get("gitObjectType") == "tree":
                continue
            change_type = str(change.get("changeType") or "").lower()
            old = ""
            new = ""
            if parent and "add" not in change_type:
                try:
                    old = await self.get_file(
                        organization, project, repository_id, path, parent
                    ) or ""
                except AzureDevOpsError:
                    old = ""
            if "delete" not in change_type:
                try:
                    new = await self.get_file(
                        organization, project, repository_id, path, sha
                    ) or ""
                except AzureDevOpsError:
                    new = ""

            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(True),
                    new.splitlines(True),
                    fromfile=f"a{path}",
                    tofile=f"b{path}",
                    n=3,
                )
            )
            if diff:
                blocks.append(diff)

        return "\n".join(blocks)[:250_000]

    async def get_repository(
        self, organization: str, project: str, repository_id: str
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}",
            )
        ).json()

    async def create_commit(
        self,
        organization: str,
        project: str,
        repository_id: str,
        *,
        branch: str,
        base_sha: str,
        changes: dict[str, str],
        message: str,
    ) -> dict[str, Any]:
        """Create a branch + one commit containing the supplied file contents."""
        ref_name = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        payload = {
            "refUpdates": [{"name": ref_name, "oldObjectId": base_sha}],
            "commits": [
                {
                    "comment": message,
                    "changes": [
                        {
                            "changeType": "edit",
                            "item": {"path": "/" + path.lstrip("/")},
                            "newContent": {
                                "content": content,
                                "contentType": "rawtext",
                            },
                        }
                        for path, content in changes.items()
                    ],
                }
            ],
        }
        return (
            await self._request(
                "POST",
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/pushes",
                json=payload,
                timeout=60.0,
            )
        ).json()

    async def create_pull_request(
        self,
        organization: str,
        project: str,
        repository_id: str,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        payload = {
            "sourceRefName": source_branch
            if source_branch.startswith("refs/")
            else f"refs/heads/{source_branch}",
            "targetRefName": target_branch
            if target_branch.startswith("refs/")
            else f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
        }
        return (
            await self._request(
                "POST",
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/pullrequests",
                json=payload,
                timeout=60.0,
            )
        ).json()

    async def publish_status(
        self,
        organization: str,
        project: str,
        repository_id: str,
        sha: str,
        *,
        state: str,
        description: str,
        target_url: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "state": state,
            "description": description[:140],
            "context": {"name": "autoheal/gate", "genre": "continuous-integration"},
        }
        if target_url:
            payload["targetUrl"] = target_url
        return (
            await self._request(
                "POST",
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/commits/{quote(sha, safe='')}/statuses",
                json=payload,
            )
        ).json()

    async def publish_pr_comment(
        self,
        organization: str,
        project: str,
        repository_id: str,
        pull_request_id: int,
        body: str,
    ) -> dict[str, Any]:
        payload = {"comments": [{"parentCommentId": 0, "content": body, "commentType": 1}]}
        return (
            await self._request(
                "POST",
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/pullrequests/{pull_request_id}/threads",
                json=payload,
            )
        ).json()

    def get_file_sync(
        self, organization: str, project: str, repository_id: str, path: str, ref: str
    ) -> str | None:
        if not self.pat:
            return None
        params = {
            "scopePath": "/" + path.lstrip("/"),
            "includeContent": "true",
            "versionDescriptor.version": ref,
            "versionDescriptor.versionType": "commit",
            "api-version": self.api_version,
        }
        try:
            url = self._url(
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/items",
            )
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                response = client.get(url, headers=self._headers(), params=params)
            if response.status_code >= 400:
                return None
            payload = response.json()
            item = (payload.get("value") or [payload])[0]
            return item.get("content")
        except (httpx.HTTPError, ValueError):
            return None

    def download_snapshot_sync(
        self,
        organization: str,
        project: str,
        repository_id: str,
        ref: str,
        dest_dir: str,
        max_files: int = 500,
    ) -> str | None:
        """Materialize a commit snapshot without requiring the git CLI."""
        if not self.pat:
            return None
        try:
            # Reuse the REST API through a temporary async loop would be
            # awkward here, so use the same HTTP API synchronously.
            url = self._url(
                organization,
                project,
                f"git/repositories/{quote(str(repository_id), safe='')}/items",
            )
            params = {
                "scopePath": "/",
                "recursionLevel": "Full",
                "includeContentMetadata": "true",
                "versionDescriptor.version": ref,
                "versionDescriptor.versionType": "commit",
                "api-version": self.api_version,
            }
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(url, headers=self._headers(), params=params)
                if response.status_code >= 400:
                    return None
                items = response.json().get("value", [])

                root = Path(dest_dir) / "azure-snapshot"
                root.mkdir(parents=True, exist_ok=True)
                count = 0
                for item in items:
                    if count >= max_files:
                        break
                    if item.get("isFolder") or item.get("gitObjectType") == "tree":
                        continue
                    path = str(item.get("path") or "")
                    if not path:
                        continue
                    rel = PurePosixPath(path.lstrip("/"))
                    if any(part in ("", ".", "..") for part in rel.parts):
                        continue
                    file_url = self._url(
                        organization,
                        project,
                        f"git/repositories/{quote(str(repository_id), safe='')}/items",
                    )
                    file_params = {
                        "scopePath": "/" + str(rel),
                        "includeContent": "true",
                        "versionDescriptor.version": ref,
                        "versionDescriptor.versionType": "commit",
                        "api-version": self.api_version,
                    }
                    fr = client.get(file_url, headers=self._headers(), params=file_params)
                    if fr.status_code >= 400:
                        continue
                    payload = fr.json()
                    value = payload.get("value") or [payload]
                    content = value[0].get("content") if value else None
                    if content is None:
                        continue
                    target = root.joinpath(*rel.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(content), encoding="utf-8")
                    count += 1
                return str(root)
        except (httpx.HTTPError, OSError, ValueError):
            return None
