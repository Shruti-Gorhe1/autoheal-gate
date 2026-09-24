from __future__ import annotations

import asyncio

from app.context.builder import ContextBuilder
from app.gate.models import GateCheckRequest
from app.providers.base import CICDProvider, PipelineEvent, ProviderCapabilities, SourceProvider


class FakeSource(SourceProvider):
    name = "fake-source"
    capabilities = ProviderCapabilities(get_commit_diff=True, get_file=True, download_snapshot=True)

    def __init__(self):
        self.calls = []

    async def get_commit_diff(self, event):
        self.calls.append(event)
        return "--- a/app.py\n+++ b/app.py\n+fixed\n"

    async def get_file(self, event, path, ref=None):
        return "content"

    async def publish_status(self, event, **kwargs):
        pass

    async def publish_comment(self, event, body):
        pass

    async def create_fix_pull_request(self, event, **kwargs):
        return {"created": True}


class FakeCICD(CICDProvider):
    name = "fake-cicd"
    capabilities = ProviderCapabilities(get_failure_logs=True)

    def __init__(self):
        self.calls = []

    async def get_failure_logs(self, event):
        self.calls.append(event)
        return "ERROR test failed\n"


def make_request(**kwargs):
    return GateCheckRequest(
        repository="acme/demo",
        commit_sha="a" * 40,
        source_provider="fake-source",
        cicd_provider="fake-cicd",
        **kwargs,
    )


def make_event(run_id="build-123", status="failure"):
    return PipelineEvent(
        provider="fake-cicd",
        source_provider="fake-source",
        cicd_provider="fake-cicd",
        repository="acme/demo",
        commit_sha="a" * 40,
        run_id=run_id,
        status=status,
    )


def test_context_builder_keeps_source_and_cicd_retrieval_separate():
    source = FakeSource()
    cicd = FakeCICD()
    context = asyncio.run(ContextBuilder().build(
        make_request(), event=make_event(), source=source, cicd=cicd
    ))

    assert context.logs == "ERROR test failed\n"
    assert context.diff.startswith("--- a/app.py")
    assert cicd.calls[0].run_id == "build-123"
    assert source.calls[0].commit_sha == "a" * 40
    assert context.source["provider"] == "fake-source"
    assert context.cicd["provider"] == "fake-cicd"
    assert context.cicd["log_source"] == "cicd_provider"
    assert context.source["diff_source"] == "source_provider"


def test_context_builder_prefers_caller_supplied_evidence():
    source = FakeSource()
    cicd = FakeCICD()
    context = asyncio.run(ContextBuilder().build(
        make_request(failure_logs="caller logs", diff="caller diff"),
        event=make_event(), source=source, cicd=cicd,
    ))

    assert context.logs == "caller logs"
    assert context.diff == "caller diff"
    assert not source.calls
    assert not cicd.calls
    assert context.source["diff_source"] == "request"
    assert context.cicd["log_source"] == "request"


def test_context_builder_does_not_retrieve_when_ci_passed():
    source = FakeSource()
    cicd = FakeCICD()
    context = asyncio.run(ContextBuilder().build(
        make_request(ci_passed=True),
        event=make_event(status="success"), source=source, cicd=cicd,
    ))

    assert context.ci_passed is True
    assert context.logs == ""
    assert context.diff == ""
    assert not source.calls
    assert not cicd.calls


def test_request_event_preserves_non_integer_pipeline_run_id():
    from app.gate import service

    request = make_request(pipeline_run_id="gcp-build-abc")
    event = service._request_event(request)
    assert event.run_id == "gcp-build-abc"
    assert event.source_provider == "fake-source"
    assert event.cicd_provider == "fake-cicd"
