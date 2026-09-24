"""AWS CodeBuild/CodePipeline client for the AutoHeal CI/CD adapter."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse


class AWSCICDError(RuntimeError):
    pass


class AWSCICDClient:
    def __init__(self, *, region: str | None = None, access_key_id: str | None = None,
                 secret_access_key: str | None = None, session_token: str | None = None):
        self.region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        self.access_key_id = access_key_id or os.getenv("AWS_ACCESS_KEY_ID", "")
        self.secret_access_key = secret_access_key or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = session_token or os.getenv("AWS_SESSION_TOKEN", "")

    def _session(self):
        try:
            import boto3
        except ImportError as exc:
            raise AWSCICDError("boto3 is required for AWS CodeBuild/CodePipeline integration.") from exc
        kwargs: dict[str, Any] = {"region_name": self.region}
        if self.access_key_id and self.secret_access_key:
            kwargs.update(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
            )
            if self.session_token:
                kwargs["aws_session_token"] = self.session_token
        return boto3.Session(**kwargs)

    def _codebuild(self):
        return self._session().client("codebuild")

    def _codepipeline(self):
        return self._session().client("codepipeline")

    def get_build(self, build_id: str) -> dict[str, Any]:
        try:
            result = self._codebuild().batch_get_builds(ids=[build_id])
        except Exception as exc:
            raise AWSCICDError(f"AWS CodeBuild lookup failed: {exc}") from exc
        builds = result.get("builds") or []
        if not builds:
            raise AWSCICDError(f"AWS CodeBuild build not found: {build_id}")
        return builds[0]

    def get_pipeline_execution(self, pipeline_name: str, execution_id: str) -> dict[str, Any]:
        try:
            result = self._codepipeline().get_pipeline_execution(
                pipelineName=pipeline_name, pipelineExecutionId=execution_id
            )
        except Exception as exc:
            raise AWSCICDError(f"AWS CodePipeline execution lookup failed: {exc}") from exc
        return result.get("pipelineExecution") or {}

    def get_pipeline_state(self, pipeline_name: str) -> dict[str, Any]:
        try:
            return self._codepipeline().get_pipeline_state(name=pipeline_name)
        except Exception as exc:
            raise AWSCICDError(f"AWS CodePipeline state lookup failed: {exc}") from exc

    def get_build_logs(self, build: dict[str, Any], max_chars: int = 200_000) -> str:
        logs = build.get("logs") or {}
        cw = logs.get("cloudWatchLogs") or {}
        group = cw.get("groupName") or logs.get("groupName")
        stream = cw.get("streamName") or logs.get("streamName")
        if not group or not stream:
            links = []
            if logs.get("deepLink"):
                links.append(str(logs["deepLink"]))
            if logs.get("s3DeepLink"):
                links.append(str(logs["s3DeepLink"]))
            return "AWS CodeBuild logs are available at: " + ", ".join(links) if links else "AWS CodeBuild did not expose downloadable logs."

        try:
            client = self._session().client("logs")
            events: list[dict[str, Any]] = []
            token = None
            for _ in range(50):
                kwargs = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
                if token:
                    kwargs["nextToken"] = token
                response = client.get_log_events(**kwargs)
                events.extend(response.get("events") or [])
                new_token = response.get("nextForwardToken")
                if not new_token or new_token == token:
                    break
                token = new_token
            text = "\n".join(str(item.get("message", "")) for item in events)
            return text[-max_chars:]
        except Exception as exc:
            return f"Could not download AWS CodeBuild logs: {exc}"


def repository_from_url(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if value.startswith("git@"):
        value = value.split(":", 1)[-1]
    elif "://" in value:
        value = urlparse(value).path.lstrip("/")
    value = value.removesuffix(".git").strip("/")
    parts = value.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else None
