"""GitHub implementation of the provider interface.

Every method here delegates to the existing, already-tested
:class:`app.services.github_client.GitHubClient` -- this class adds no new
GitHub API behavior, it only exposes the existing behavior through the
provider-neutral shape. That's deliberate: the safest way to introduce an
abstraction over working code is to wrap it, not rewrite it.
"""

from __future__ import annotations

from typing import Any

from app.providers.base import (
    CICDProvider,
    PipelineEvent,
    ProviderCapabilities,
    SourceProvider,
)
from app.services.github_client import GitHubClient, GitHubError


class GitHubProvider(SourceProvider, CICDProvider):
    """GitHub is, today, the only provider that is both the source of the
    code and the CI/CD system that built it -- so one class satisfies both
    interfaces. A future Azure Repos + GitHub Actions combination would
    instead pass one ``AzureSourceProvider`` and one ``GitHubProvider`` (as
    a ``CICDProvider``) to the same downstream pipeline; nothing about that
    combination requires a single class to implement both roles.
    """

    name = "github"
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
        self._client = GitHubClient(token)

    # ------------------------------------------------------------- reading

    async def get_failure_logs(self, event: PipelineEvent) -> str:
        if not self.token or not event.run_id:
            return ""
        try:
            return await self._client.get_failed_job_logs(event.repository, int(event.run_id))
        except Exception as exc:
            return f"Could not download workflow logs: {exc}"

    async def get_commit_diff(self, event: PipelineEvent) -> str:
        if not self.token:
            return ""
        try:
            return await self._client.get_commit_diff(event.repository, event.commit_sha)
        except Exception:
            return ""

    async def get_file(
        self, event: PipelineEvent, path: str, ref: str | None = None
    ) -> str | None:
        if not self.token:
            return None
        try:
            return await self._client.get_file(event.repository, path, ref or event.commit_sha)
        except Exception:
            return None

    def get_file_sync(self, repository: str, commit_sha: str, path: str) -> str | None:
        # Imported lazily so tests (and any future caller) can monkeypatch
        # app.services.github_client.fetch_file_sync at its origin -- a
        # module-level import here would bind a copy that patching there
        # wouldn't reach.
        from app.services.github_client import fetch_file_sync

        return fetch_file_sync(self.token, repository, path, commit_sha)

    def download_snapshot_sync(
        self, repository: str, commit_sha: str, dest_dir: str
    ) -> str | None:
        from app.services.github_client import download_repository_snapshot

        return download_repository_snapshot(self.token, repository, commit_sha, dest_dir)

    # ------------------------------------------------------------- writing

    async def publish_status(
        self,
        event: PipelineEvent,
        *,
        verdict: str,
        summary: str,
        detail: str = "",
        details_url: str | None = None,
    ) -> None:
        if not self.token:
            return
        conclusion = {"PASS": "success", "BLOCK": "failure", "HOLD": "action_required"}.get(
            verdict, "neutral"
        )
        try:
            await self._client.create_check_run(
                event.repository,
                event.commit_sha,
                name="AutoHeal Gate",
                conclusion=conclusion,
                title=f"{verdict}",
                summary=summary,
                text=detail,
                details_url=details_url,
            )
        except GitHubError:
            state = {"PASS": "success", "BLOCK": "failure", "HOLD": "pending"}.get(
                verdict, "pending"
            )
            try:
                await self._client.create_commit_status(
                    event.repository,
                    event.commit_sha,
                    state=state,
                    description=summary[:140],
                    target_url=details_url,
                )
            except GitHubError:
                pass

    async def publish_comment(self, event: PipelineEvent, body: str) -> None:
        if not self.token or not event.change_request_number:
            return
        try:
            await self._client.comment_on_pull_request(
                event.repository, event.change_request_number, body
            )
        except GitHubError:
            pass

    async def create_fix_pull_request(
        self, event: PipelineEvent, *, patch: str, title: str, body: str
    ) -> dict[str, Any]:
        if not self.token:
            return {"created": False, "reason": "No GitHub token is available."}

        repo = event.repository
        branch = f"autoheal/fix-{event.run_id or event.commit_sha[:8]}"

        try:
            details = await self._client.get_repository(repo)
            base = details.get("default_branch", "main")
            await self._client.create_branch(repo, branch, event.commit_sha)

            written: list[str] = []
            for path, new_content in _apply_patch_to_sources(patch).items():
                sha = await self._client.get_file_sha(repo, path, branch)
                await self._client.put_file(
                    repo, path, new_content, message=title, branch=branch, sha=sha
                )
                written.append(path)

            pr = await self._client.create_pull_request(
                repo, title=title, head=branch, base=base, body=body
            )
            return {
                "created": True,
                "branch": branch,
                "files": written,
                "pull_request_url": pr.get("html_url"),
                "number": pr.get("number"),
            }
        except GitHubError as exc:
            return {"created": False, "reason": str(exc)}

    # -------------------------------------------------------- webhook entry

    @staticmethod
    def normalize_webhook(payload: dict[str, Any]) -> PipelineEvent | None:
        """Convert a GitHub ``workflow_run`` webhook payload into a
        :class:`PipelineEvent`.

        Only ``workflow_run`` is handled -- the only event type the gate has
        ever acted on. Returns ``None`` for anything else, mirroring the
        early-return checks that used to live inline in the webhook route.
        """
        run = payload.get("workflow_run", {})
        repository = payload.get("repository", {}).get("full_name", "")
        if not repository or not run:
            return None

        conclusion = run.get("conclusion")
        if conclusion == "success":
            status = "success"
        elif conclusion in ("failure", "timed_out", "startup_failure"):
            status = "failure"
        elif conclusion == "cancelled":
            status = "cancelled"
        else:
            status = "unknown"

        pull_requests = run.get("pull_requests") or []

        return PipelineEvent(
            provider="github",
            repository=repository,
            commit_sha=run.get("head_sha", ""),
            branch=run.get("head_branch"),
            run_id=str(run["id"]) if run.get("id") is not None else None,
            change_request_number=pull_requests[0]["number"] if pull_requests else None,
            status=status,
            metadata={
                "workflow_name": run.get("name"),
                "html_url": run.get("html_url"),
                "event": run.get("event"),
                "actor": (run.get("actor") or {}).get("login"),
            },
            raw_payload=payload,
        )


def _apply_patch_to_sources(patch: str) -> dict[str, str]:
    """Reconstruct post-patch file contents from a unified diff.

    Only full-context hunks are supported, which is the patch shape emitted
    by the current fix agents. The transformation is repository-provider
    specific because it feeds the source-control contents API.
    """
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
