"""Phase 5 tests for AWS CodeBuild/CodePipeline CI/CD integration."""
from __future__ import annotations

import asyncio

from app.providers.aws import AWSProvider
from app.providers.base import CICDProvider
from app.providers.resolver import ProviderResolver


CODEBUILD = {
    "version": "0",
    "detail-type": "CodeBuild Build State Change",
    "source": "aws.codebuild",
    "region": "us-east-1",
    "detail": {
        "build-status": "FAILED",
        "project-name": "payments-build",
        "build-id": "arn:aws:codebuild:us-east-1:123:build/payments-build:build-123",
        "repository": "acme/payments-api",
        "commit-sha": "a" * 40,
        "source-provider": "github",
    },
}

CODEPIPELINE = {
    "version": "0",
    "detail-type": "CodePipeline Pipeline Execution State Change",
    "source": "aws.codepipeline",
    "region": "us-east-1",
    "detail": {
        "pipeline": "payments-pipeline",
        "execution-id": "12345678-1234-5678-abcd-123456789012",
        "state": "FAILED",
        "repository": "acme/payments-api",
        "commit-id": "b" * 40,
        "source-provider": "github",
    },
}


def test_aws_provider_is_cicd_only():
    provider = AWSProvider(None)
    assert isinstance(provider, CICDProvider)
    assert provider.capabilities.get_failure_logs is True
    assert provider.capabilities.get_commit_diff is False
    assert provider.capabilities.get_file is False
    assert provider.capabilities.publish_status is False
    assert provider.capabilities.publish_comment is False
    assert provider.capabilities.create_fix_pull_request is False


def test_aws_resolver_registers_only_cicd_dimension():
    resolver = ProviderResolver()
    assert isinstance(resolver.resolve_cicd("AWS"), AWSProvider)
    assert "aws" not in resolver.source_registry
    assert "aws" in resolver.cicd_registry


def test_aws_codebuild_event_normalizes():
    event = AWSProvider.normalize_webhook(CODEBUILD)
    assert event is not None
    assert event.provider == "aws"
    assert event.source_provider == "github"
    assert event.cicd_provider == "aws"
    assert event.repository == "acme/payments-api"
    assert event.commit_sha == "a" * 40
    assert event.pipeline_id == "payments-build"
    assert event.run_id.endswith("build-123")
    assert event.status == "failure"
    assert event.metadata["aws_service"] == "codebuild"


def test_aws_codepipeline_event_normalizes():
    event = AWSProvider.normalize_webhook(CODEPIPELINE)
    assert event is not None
    assert event.cicd_provider == "aws"
    assert event.source_provider == "github"
    assert event.pipeline_id == "payments-pipeline"
    assert event.run_id.startswith("12345678-")
    assert event.status == "failure"
    assert event.metadata["aws_service"] == "codepipeline"


def test_aws_success_and_cancelled_statuses():
    success = {**CODEBUILD, "detail": {**CODEBUILD["detail"], "build-status": "SUCCEEDED"}}
    stopped = {**CODEBUILD, "detail": {**CODEBUILD["detail"], "build-status": "STOPPED"}}
    assert AWSProvider.normalize_webhook(success).status == "success"
    assert AWSProvider.normalize_webhook(stopped).status == "cancelled"


def test_aws_unknown_event_is_rejected():
    assert AWSProvider.normalize_webhook({"source": "aws.s3", "detail": {}}) is None
    assert AWSProvider.normalize_webhook({"source": "aws.codebuild", "detail": {}}) is None


def test_aws_cross_provider_resolver_routes_distinct_credentials():
    pair = ProviderResolver().resolve(
        source_provider="github",
        cicd_provider="aws",
        provider_tokens={"github": "gh-token", "aws": "aws-token"},
    )
    assert pair.source.token == "gh-token"
    assert isinstance(pair.cicd, AWSProvider)


def test_aws_codebuild_logs_delegate_to_client(monkeypatch):
    build = {"logs": {"cloudWatchLogs": {"groupName": "group", "streamName": "stream"}}}
    calls = {}

    def fake_get_build(self, build_id):
        calls["build"] = build_id
        return build

    def fake_get_logs(self, value):
        calls["logs"] = value
        return "AWS FAILURE LOG"

    monkeypatch.setattr("app.services.aws_cicd_client.AWSCICDClient.get_build", fake_get_build)
    monkeypatch.setattr("app.services.aws_cicd_client.AWSCICDClient.get_build_logs", fake_get_logs)
    event = AWSProvider.normalize_webhook(CODEBUILD)
    result = asyncio.run(AWSProvider(None).get_failure_logs(event))
    assert result == "AWS FAILURE LOG"
    assert calls["build"].endswith("build-123")
    assert calls["logs"] == build


def test_aws_codepipeline_without_build_id_returns_provider_message():
    event = AWSProvider.normalize_webhook(CODEPIPELINE)
    result = asyncio.run(AWSProvider(None).get_failure_logs(event))
    assert "CodeBuild build ID" in result
