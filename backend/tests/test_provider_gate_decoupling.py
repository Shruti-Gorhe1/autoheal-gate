"""Focused tests for Phase 1 provider decoupling."""

from __future__ import annotations

import asyncio

from app.gate import service
from app.gate.models import GateCheckRequest, GateCheckResponse
from app.providers.base import CICDProvider, PipelineEvent, SourceProvider
from app.providers.github import GitHubProvider
from app.providers.resolver import resolve_providers


class FakeSource(SourceProvider):
    name = "fake-source"

    def __init__(self):
        self.calls = []

    async def get_commit_diff(self, event):
        self.calls.append(("diff", event.repository, event.commit_sha))
        return "diff"

    async def get_file(self, event, path, ref=None):
        self.calls.append(("file", event.repository, path, ref))
        return "name: demo\n"

    async def publish_status(self, event, **kwargs):
        self.calls.append(("status", event.repository, kwargs))

    async def publish_comment(self, event, body):
        self.calls.append(("comment", event.repository, body))

    async def create_fix_pull_request(self, event, **kwargs):
        self.calls.append(("pr", event.repository, kwargs))
        return {"created": True, "number": 7}


class FakeCICD(CICDProvider):
    name = "fake-cicd"

    def __init__(self):
        self.calls = []

    async def get_failure_logs(self, event):
        self.calls.append(event)
        return "failure logs"


def test_resolver_keeps_source_and_cicd_resolution_independent_for_supported_provider():
    pair = resolve_providers(
        source_provider="github",
        cicd_provider="github",
        access_token="token",
    )
    assert isinstance(pair.source, GitHubProvider)
    assert isinstance(pair.cicd, GitHubProvider)
    assert pair.source is not pair.cicd


def test_resolver_accepts_azure_source_provider_in_phase3():
    from app.providers.azure import AzureProvider

    pair = resolve_providers(
        source_provider="azure",
        cicd_provider="github",
        access_token="token",
    )
    assert isinstance(pair.source, AzureProvider)
    assert isinstance(pair.cicd, GitHubProvider)


def test_request_is_adapted_to_normalized_pipeline_event():
    request = GateCheckRequest(
        repository="acme/demo",
        commit_sha="a" * 40,
        branch="main",
        workflow_run_id=123,
        pull_request_number=9,
        ci_passed=False,
    )
    event = service._request_event(request)
    assert isinstance(event, PipelineEvent)
    assert event.source_provider == "github"
    assert event.cicd_provider == "github"
    assert event.run_id == "123"
    assert event.change_request_number == 9
    assert event.status == "failure"


def test_collect_evidence_uses_source_and_cicd_interfaces_only():
    request = GateCheckRequest(
        repository="acme/demo",
        commit_sha="b" * 40,
        workflow_run_id=123,
        ci_passed=False,
    )
    source = FakeSource()
    cicd = FakeCICD()

    logs, diff = asyncio.run(service.collect_evidence(request, source, cicd))

    assert logs == "failure logs"
    assert diff == "diff"
    assert cicd.calls[0].run_id == "123"
    assert source.calls[0] == ("diff", "acme/demo", "b" * 40)


def test_publish_verdict_delegates_reporting_to_source_provider():
    source = FakeSource()
    event = PipelineEvent(
        provider="fake-cicd",
        source_provider="fake-source",
        cicd_provider="fake-cicd",
        repository="acme/demo",
        commit_sha="c" * 40,
        change_request_number=3,
        status="failure",
    )
    response = GateCheckResponse(
        verdict="BLOCK",
        allowed=False,
        run_id="gate_test",
        repository="acme/demo",
        commit_sha="c" * 40,
        reason="blocked",
    )

    asyncio.run(service.publish_verdict(source, event, response, {"comment_on_pull_request": True}))

    assert source.calls[0][0] == "status"
    assert source.calls[1][0] == "comment"


def test_gate_service_has_no_direct_github_client_dependency():
    import inspect

    source = inspect.getsource(service)
    assert "GitHubClient" not in source
    assert "GitHubError" not in source


def test_provider_resolver_object_resolves_each_dimension_independently():
    from app.providers.resolver import ProviderResolver

    resolver = ProviderResolver()
    source = resolver.resolve_source(" GITHUB ", access_token="token")
    cicd = resolver.resolve_cicd("github", access_token="token")
    assert isinstance(source, GitHubProvider)
    assert isinstance(cicd, GitHubProvider)
    assert source is not cicd


def test_provider_resolver_accepts_azure_cicd_provider_in_phase3():
    from app.providers.resolver import ProviderResolver
    from app.providers.azure import AzureProvider

    provider = ProviderResolver().resolve_cicd("azure", access_token="token")
    assert isinstance(provider, AzureProvider)


def test_gate_service_uses_the_provider_resolver(monkeypatch, db):
    from app.db.base import init_db

    init_db()
    calls = []

    class Resolver:
        def resolve(self, *, source_provider, cicd_provider, access_token):
            calls.append((source_provider, cicd_provider, access_token))
            return type("Pair", (), {"source": FakeSource(), "cicd": FakeCICD()})()

    monkeypatch.setattr(service, "provider_resolver", Resolver())

    request = GateCheckRequest(
        repository="acme/demo",
        commit_sha="a" * 40,
        source_provider="github",
        cicd_provider="github",
        ci_passed=True,
    )

    response = asyncio.run(
        service.run_gate(
            db,
            request,
            access_token="token",
        )
    )

    assert calls == [("github", "github", "token")]
    assert response.run_id.startswith("gate_")
