"""GitHub REST client bound to one access token.

Every instance is constructed with the token resolved from the caller's
session or API key, so all GitHub work happens as that user. There is no
global machine token.
"""

from __future__ import annotations

import base64
import io
import logging
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("autoheal.github")


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def fetch_file_sync(
    token: str | None, repo: str, path: str, ref: str, timeout: float = 15.0
) -> str | None:
    """Synchronous file fetch, for callers outside the event loop.

    The agent pipeline runs as ordinary synchronous code (it may run inside a
    thread pool, a CLI, or a test -- not necessarily inside a running asyncio
    loop), so it cannot ``await`` :meth:`GitHubClient.get_file`. This exists
    specifically for the Fix agent's fallback when a repository has no local
    checkout registered: the file is read straight from GitHub at the exact
    commit under evaluation instead. Returns ``None`` on any failure --
    missing file, bad ref, no access -- exactly like the local-disk read it
    stands in for, so callers don't need a separate error path.
    """
    if not token:
        return None
    try:
        response = httpx.get(
            f"{settings.github_api_base}/repos/{repo}/contents/{path}",
            params={"ref": ref},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            return None
        payload = response.json()
        if payload.get("encoding") != "base64":
            return payload.get("content")
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
    except (httpx.HTTPError, ValueError):
        return None


def download_repository_snapshot(
    token: str | None,
    repo: str,
    ref: str,
    dest_dir: str,
    timeout: float = 60.0,
) -> str | None:
    """Download the exact commit ``ref`` of ``repo`` into ``dest_dir``.

    This is what lets validation actually run for a repository the gate only
    knows about through a webhook, with no local clone registered anywhere:
    GitHub's tarball endpoint (`/repos/{repo}/tarball/{ref}`) returns a full
    snapshot of the repository at a specific commit, no ``git`` binary or
    clone URL needed -- just an authenticated HTTP request. GitHub answers
    with a redirect to a signed, time-limited codeload.github.com URL, which
    ``httpx`` follows automatically.

    Returns the path to the single extracted top-level directory (GitHub
    tarballs always contain exactly one), or ``None`` on any failure --
    network error, bad ref, no access, a malformed archive -- so callers can
    treat "could not get the snapshot" as one uniform case.
    """
    if not token:
        return None

    url = f"{settings.github_api_base}/repos/{repo}/tarball/{ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400:
            return None

        archive_path = Path(dest_dir) / "snapshot.tar.gz"
        archive_path.write_bytes(response.content)

        with tarfile.open(archive_path) as archive:
            # filter="data" (Python 3.12+) refuses absolute paths, symlinks
            # that escape dest_dir, and device files -- this archive comes
            # from GitHub, but it's still untrusted input being extracted to
            # disk, so it's extracted the way any downloaded archive should be.
            archive.extractall(dest_dir, filter="data")

        archive_path.unlink(missing_ok=True)

        top_level = [p for p in Path(dest_dir).iterdir() if p.is_dir()]
        if len(top_level) != 1:
            return None
        return str(top_level[0])

    except (httpx.HTTPError, tarfile.TarError, OSError):
        return None


class GitHubClient:
    def __init__(self, access_token: str | None, base_url: str | None = None):
        self.token = access_token
        self.base = (base_url or settings.github_api_base).rstrip("/")

    # ------------------------------------------------------------- internals

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.request(
                method, url, headers=self._headers(accept), **kwargs
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"{method} {path} failed with HTTP {response.status_code}: "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )
        return response

    # ------------------------------------------------------------- identity

    async def whoami(self) -> dict[str, Any]:
        return (await self._request("GET", "/user")).json()

    async def list_repositories(
        self, page: int = 1, per_page: int = 50
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "/user/repos",
            params={
                "per_page": per_page,
                "page": page,
                "sort": "updated",
                "affiliation": "owner,collaborator,organization_member",
            },
        )
        return [
            {
                "full_name": repo["full_name"],
                "private": repo["private"],
                "default_branch": repo.get("default_branch", "main"),
                "html_url": repo["html_url"],
                "clone_url": repo["clone_url"],
                "permissions": repo.get("permissions", {}),
                "updated_at": repo.get("updated_at"),
            }
            for repo in response.json()
        ]

    # --------------------------------------------------------------- actions

    async def get_workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
        return (await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}")).json()

    async def get_workflow_logs(self, repo: str, run_id: int, max_chars: int = 200_000) -> str:
        """Download a run's log archive and flatten it into readable text."""
        try:
            response = await self._request(
                "GET", f"/repos/{repo}/actions/runs/{run_id}/logs", timeout=90.0
            )
        except GitHubError as exc:
            return f"Could not download GitHub Actions logs: {exc}"

        content = response.content
        if not content.startswith(b"PK"):
            return content.decode("utf-8", errors="replace")[:max_chars]

        chunks: list[str] = []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in sorted(archive.namelist()):
                if name.endswith("/"):
                    continue
                try:
                    text = archive.read(name).decode("utf-8", errors="replace")
                except Exception:
                    continue
                chunks.append(f"===== {name} =====\n{text}")
        return "\n\n".join(chunks)[:max_chars]

    async def get_failed_job_logs(self, repo: str, run_id: int) -> str:
        """Logs from failed jobs only. Far less noise than the whole archive."""
        try:
            jobs = (
                await self._request(
                    "GET",
                    f"/repos/{repo}/actions/runs/{run_id}/jobs",
                    params={"filter": "latest", "per_page": 100},
                )
            ).json().get("jobs", [])
        except GitHubError:
            return await self.get_workflow_logs(repo, run_id)

        failed = [j for j in jobs if j.get("conclusion") in {"failure", "timed_out"}]
        if not failed:
            return await self.get_workflow_logs(repo, run_id)

        blocks: list[str] = []
        for job in failed:
            steps = ", ".join(
                f"{s.get('name')}={s.get('conclusion')}" for s in job.get("steps", [])
            )
            blocks.append(f"===== JOB {job.get('name')} ({job.get('conclusion')}) =====\nSTEPS: {steps}")
            try:
                log = await self._request(
                    "GET", f"/repos/{repo}/actions/jobs/{job['id']}/logs", timeout=60.0
                )
                blocks.append(log.text[-40_000:])
            except GitHubError as exc:
                blocks.append(f"(log download failed: {exc})")
        return "\n\n".join(blocks)

    # ----------------------------------------------------------------- code

    async def get_commit_diff(self, repo: str, sha: str) -> str:
        try:
            response = await self._request(
                "GET",
                f"/repos/{repo}/commits/{sha}",
                accept="application/vnd.github.diff",
            )
            return response.text
        except GitHubError as exc:
            return f"Could not fetch the commit diff: {exc}"

    async def get_file(self, repo: str, path: str, ref: str) -> str | None:
        try:
            payload = (
                await self._request(
                    "GET", f"/repos/{repo}/contents/{path}", params={"ref": ref}
                )
            ).json()
        except GitHubError:
            return None
        if payload.get("encoding") != "base64":
            return payload.get("content")
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")

    async def get_repository(self, repo: str) -> dict[str, Any]:
        return (await self._request("GET", f"/repos/{repo}")).json()

    # ----------------------------------------------------------- check runs

    async def create_check_run(
        self,
        repo: str,
        head_sha: str,
        *,
        name: str = "AutoHeal Gate",
        conclusion: str = "neutral",
        title: str = "",
        summary: str = "",
        text: str = "",
        details_url: str | None = None,
    ) -> dict[str, Any]:
        """Publish the verdict onto the commit so branch protection can use it."""
        body: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": title[:255] or name,
                "summary": summary[:65_000],
                "text": text[:65_000],
            },
        }
        if details_url:
            body["details_url"] = details_url
        return (await self._request("POST", f"/repos/{repo}/check-runs", json=body)).json()

    async def create_commit_status(
        self,
        repo: str,
        sha: str,
        *,
        state: str,
        description: str,
        context: str = "autoheal/gate",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        """Fallback for tokens without the checks permission."""
        body = {"state": state, "description": description[:140], "context": context}
        if target_url:
            body["target_url"] = target_url
        return (await self._request("POST", f"/repos/{repo}/statuses/{sha}", json=body)).json()

    # ------------------------------------------------------- pull requests

    async def comment_on_pull_request(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body[:65_000]}
            )
        ).json()

    async def create_branch(self, repo: str, new_branch: str, from_sha: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{new_branch}", "sha": from_sha},
            )
        ).json()

    async def put_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return (await self._request("PUT", f"/repos/{repo}/contents/{path}", json=body)).json()

    async def get_file_sha(self, repo: str, path: str, ref: str) -> str | None:
        try:
            payload = (
                await self._request(
                    "GET", f"/repos/{repo}/contents/{path}", params={"ref": ref}
                )
            ).json()
            return payload.get("sha")
        except GitHubError:
            return None

    async def create_pull_request(
        self,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = False,
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                f"/repos/{repo}/pulls",
                json={
                    "title": title,
                    "head": head,
                    "base": base,
                    "body": body[:65_000],
                    "draft": draft,
                },
            )
        ).json()
