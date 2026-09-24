"""Phase 4 tests for Google Cloud Build CI/CD integration."""

from __future__ import annotations

import asyncio
import base64
import json

from app.providers.base import CICDProvider, PipelineEvent
from app.providers.google_cloud_build import GoogleCloudBuildProvider
from app.providers.resolver import ProviderResolver


BUILD = {
    "id": "build-123",
    "projectId": "demo-project",
    "status": "FAILURE",
    "name": "projects/demo-project/locations/us-central1/builds/build-123",
    "buildTriggerId": "trigger-7",
    "logUrl": "https://console.cloud.google.com/cloud-build/builds/build-123",
    "source": {
        "repoSource": {
            "repoName": "acme/payments-api",
            "branchName": "main",
            "commitSha": "a" * 40,
        }
    },
    "substitutions": {
        "COMMIT_SHA": "a" * 40,
        "BRANCH_NAME": "main",
        "_SOURCE_PROVIDER": "github",
    },
    "logsBucket": "gs://demo-logs",
}


def _pubsub(build=BUILD, status="FAILURE"):
    return {
        "message": {
            "attributes": {"buildId": build["id"], "status": status},
            "data": base64.b64encode(json.dumps(build).encode()).decode(),
            "messageId": "message-1",
        },
        "subscription": "projects/demo-project/subscriptions/cloud-builds",
    }


def test_gcp_provider_is_cicd_only():
    provider = GoogleCloudBuildProvider("token")
    assert isinstance(provider, CICDProvider)
    assert provider.capabilities.get_failure_logs is True
    assert provider.capabilities.get_commit_diff is False
    assert provider.capabilities.get_file is False
    assert provider.capabilities.publish_status is False
    assert provider.capabilities.publish_comment is False
    assert provider.capabilities.create_fix_pull_request is False


def test_gcp_resolver_registers_only_cicd_dimension():
    resolver = ProviderResolver()
    provider = resolver.resolve_cicd("GCP", access_token="token")
    assert isinstance(provider, GoogleCloudBuildProvider)
    assert "gcp" not in resolver.source_registry


def test_gcp_pubsub_webhook_normalizes_to_pipeline_event():
    event = GoogleCloudBuildProvider.normalize_webhook(_pubsub())
    assert event is not None
    assert event.provider == "gcp"
    assert event.source_provider == "github"
    assert event.cicd_provider == "gcp"
    assert event.is_cross_provider is True
    assert event.repository == "acme/payments-api"
    assert event.commit_sha == "a" * 40
    assert event.branch == "main"
    assert event.pipeline_id == "trigger-7"
    assert event.run_id == "build-123"
    assert event.status == "failure"
    assert event.project == "demo-project"
    assert event.metadata["gcp_location"] == "us-central1"


def test_gcp_success_and_cancelled_statuses():
    success = dict(BUILD, status="SUCCESS")
    cancelled = dict(BUILD, status="CANCELLED")
    assert GoogleCloudBuildProvider.normalize_webhook(_pubsub(success, "SUCCESS")).status == "success"
    assert GoogleCloudBuildProvider.normalize_webhook(_pubsub(cancelled, "CANCELLED")).status == "cancelled"


def test_gcp_direct_build_payload_is_supported_for_local_testing():
    event = GoogleCloudBuildProvider.normalize_webhook(BUILD)
    assert event is not None
    assert event.run_id == "build-123"


def test_gcp_incomplete_payload_is_rejected():
    assert GoogleCloudBuildProvider.normalize_webhook({"message": {}}) is None
    assert GoogleCloudBuildProvider.normalize_webhook({"id": "build-123", "status": "FAILURE"}) is None


def test_gcp_failure_logs_delegate_to_client(monkeypatch):
    calls = {}

    async def fake_get_build(self, build_id, *, location=None):
        calls["get_build"] = (build_id, location, self.project_id)
        return BUILD

    async def fake_logs(self, build_id, *, location=None, build=None, max_chars=200_000):
        calls["logs"] = (build_id, location, build["id"], max_chars)
        return "GCP FAILURE LOG"

    monkeypatch.setattr(
        "app.services.google_cloud_build_client.GoogleCloudBuildClient.get_build",
        fake_get_build,
    )
    monkeypatch.setattr(
        "app.services.google_cloud_build_client.GoogleCloudBuildClient.get_failure_logs",
        fake_logs,
    )
    event = GoogleCloudBuildProvider.normalize_webhook(_pubsub())
    result = asyncio.run(GoogleCloudBuildProvider("token").get_failure_logs(event))
    assert result == "GCP FAILURE LOG"
    assert calls["get_build"] == ("build-123", "us-central1", "demo-project")
    assert calls["logs"] == ("build-123", "us-central1", "build-123", 200_000)


def test_gcp_cross_provider_resolver_routes_distinct_credentials():
    pair = ProviderResolver().resolve(
        source_provider="github",
        cicd_provider="gcp",
        provider_tokens={"github": "gh-token", "gcp": "gcp-token"},
    )
    assert pair.source.token == "gh-token"
    assert pair.cicd.token == "gcp-token"


def test_gcp_repository_can_be_explicitly_marked_as_another_source_provider():
    build = dict(BUILD)
    build["substitutions"] = {**BUILD["substitutions"], "_SOURCE_PROVIDER": "azure"}
    event = GoogleCloudBuildProvider.normalize_webhook(_pubsub(build))
    assert event is not None
    assert event.source_provider == "azure"
    assert event.cicd_provider == "gcp"
