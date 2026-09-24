"""Phase 3 tests for Azure Repos + Azure Pipelines."""

from __future__ import annotations

import asyncio

from app.providers.azure import AzureProvider
from app.providers.base import CICDProvider, PipelineEvent, SourceProvider
from app.providers.resolver import ProviderResolver


BUILD_COMPLETE_PAYLOAD = {
    "eventType": "build.complete",
    "publisherId": "tfs",
    "resourceContainers": {
        "collection": {"baseUrl": "https://dev.azure.com/acme/"},
    },
    "resource": {
        "id": 9876,
        "buildNumber": "2026.09.24.1",
        "status": "completed",
        "result": "failed",
        "sourceVersion": "a" * 40,
        "sourceBranch": "refs/heads/main",
        "reason": "individualCI",
        "definition": {"id": 42, "name": "Payments-CI"},
        "project": {"id": "project-id", "name": "Payments"},
        "repository": {
            "id": "repo-id",
            "name": "payments-api",
            "type": "TfsGit",
            "url": "https://dev.azure.com/acme/Payments/_git/payments-api",
        },
        "triggerInfo": {},
    },
}


def test_azure_provider_implements_both_interfaces():
    provider = AzureProvider("pat")
    assert isinstance(provider, SourceProvider)
    assert isinstance(provider, CICDProvider)


def test_resolver_registers_azure_independently():
    resolver = ProviderResolver()
    source = resolver.resolve_source(" AZURE ", access_token="pat")
    cicd = resolver.resolve_cicd("azure", access_token="pat")
    assert isinstance(source, AzureProvider)
    assert isinstance(cicd, AzureProvider)
    assert source is not cicd


def test_azure_pipeline_resolves_as_azure_cicd():
    resolver = ProviderResolver()
    pair = resolver.resolve(
        source_provider="azure",
        cicd_provider="azure",
        access_token="pat",
    )
    assert isinstance(pair.source, AzureProvider)
    assert isinstance(pair.cicd, AzureProvider)


def test_azure_capabilities_match_implemented_surface():
    caps = AzureProvider("pat").capabilities
    assert caps.get_commit_diff is True
    assert caps.get_file is True
    assert caps.download_snapshot is True
    assert caps.publish_status is True
    assert caps.publish_comment is True
    assert caps.create_fix_pull_request is True
    assert caps.get_failure_logs is True


def test_azure_build_webhook_normalizes_to_pipeline_event():
    event = AzureProvider.normalize_webhook(BUILD_COMPLETE_PAYLOAD)
    assert event is not None
    assert event.provider == "azure"
    assert event.source_provider == "azure"
    assert event.cicd_provider == "azure"
    assert event.repository == "acme/Payments/payments-api"
    assert event.commit_sha == "a" * 40
    assert event.branch == "main"
    assert event.pipeline_id == "42"
    assert event.run_id == "9876"
    assert event.status == "failure"
    assert event.organization == "acme"
    assert event.project == "Payments"
    assert event.metadata["repository_id"] == "repo-id"


def test_azure_github_cross_provider_webhook_is_representable():
    payload = {
        **BUILD_COMPLETE_PAYLOAD,
        "resource": {
            **BUILD_COMPLETE_PAYLOAD["resource"],
            "repository": {
                **BUILD_COMPLETE_PAYLOAD["resource"]["repository"],
                "type": "GitHub",
                "name": "acme/payments-api",
                "properties": {"fullName": "acme/payments-api"},
            },
        },
    }
    event = AzureProvider.normalize_webhook(payload)
    assert event is not None
    assert event.source_provider == "github"
    assert event.cicd_provider == "azure"
    assert event.repository == "acme/payments-api"
    assert event.is_cross_provider is True


def test_azure_success_and_cancelled_results_are_normalized():
    success = {
        **BUILD_COMPLETE_PAYLOAD,
        "resource": {**BUILD_COMPLETE_PAYLOAD["resource"], "result": "succeeded"},
    }
    cancelled = {
        **BUILD_COMPLETE_PAYLOAD,
        "resource": {**BUILD_COMPLETE_PAYLOAD["resource"], "result": "canceled"},
    }
    assert AzureProvider.normalize_webhook(success).status == "success"
    assert AzureProvider.normalize_webhook(success).ci_passed is True
    assert AzureProvider.normalize_webhook(cancelled).status == "cancelled"


def test_non_build_event_is_rejected():
    assert AzureProvider.normalize_webhook({"eventType": "git.push", "resource": {}}) is None


def test_missing_build_id_is_rejected():
    payload = {**BUILD_COMPLETE_PAYLOAD, "resource": {**BUILD_COMPLETE_PAYLOAD["resource"], "id": None}}
    assert AzureProvider.normalize_webhook(payload) is None


def test_azure_failure_logs_delegate_to_client(monkeypatch):
    calls = {}

    async def fake_logs(self, organization, project, build_id, max_chars=200_000):
        calls.update(
            organization=organization,
            project=project,
            build_id=build_id,
            max_chars=max_chars,
        )
        return "AZURE FAILURE LOG"

    monkeypatch.setattr(
        "app.services.azure_devops_client.AzureDevOpsClient.get_failure_logs",
        fake_logs,
    )
    event = AzureProvider.normalize_webhook(BUILD_COMPLETE_PAYLOAD)
    result = asyncio.run(AzureProvider("pat").get_failure_logs(event))
    assert result == "AZURE FAILURE LOG"
    assert calls["organization"] == "acme"
    assert calls["project"] == "Payments"
    assert calls["build_id"] == "9876"


def test_azure_file_read_delegates_to_client(monkeypatch):
    calls = {}

    async def fake_file(self, organization, project, repository_id, path, ref):
        calls.update(
            organization=organization,
            project=project,
            repository_id=repository_id,
            path=path,
            ref=ref,
        )
        return "print('ok')"

    monkeypatch.setattr(
        "app.services.azure_devops_client.AzureDevOpsClient.get_file",
        fake_file,
    )
    event = AzureProvider.normalize_webhook(BUILD_COMPLETE_PAYLOAD)
    result = asyncio.run(AzureProvider("pat").get_file(event, "src/main.py"))
    assert result == "print('ok')"
    assert calls == {
        "organization": "acme",
        "project": "Payments",
        "repository_id": "repo-id",
        "path": "src/main.py",
        "ref": "a" * 40,
    }


def test_gcp_cicd_is_registered_without_becoming_an_azure_source_provider():
    from app.providers.google_cloud_build import GoogleCloudBuildProvider

    resolver = ProviderResolver()
    provider = resolver.resolve_cicd("gcp", access_token="token")
    assert isinstance(provider, GoogleCloudBuildProvider)
    with __import__("pytest").raises(ValueError, match="Unsupported source provider"):
        resolver.resolve_source("gcp", access_token="token")


def test_cross_provider_event_shape_supports_azure_source_and_github_cicd():
    event = PipelineEvent(
        provider="github",
        source_provider="azure",
        cicd_provider="github",
        repository="Payments/payments-api",
        commit_sha="b" * 40,
        run_id="123",
    )
    assert event.source_provider == "azure"
    assert event.cicd_provider == "github"
    assert event.is_cross_provider is True


def test_resolver_supports_distinct_credentials_for_cross_provider_pair():
    resolver = ProviderResolver()
    pair = resolver.resolve(
        source_provider="github",
        cicd_provider="azure",
        provider_tokens={"github": "gh-token", "azure": "az-pat"},
    )
    assert pair.source.token == "gh-token"
    assert pair.cicd.token == "az-pat"
