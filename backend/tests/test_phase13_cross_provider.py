"""Phase 13 integration/concurrency coverage.

These tests exercise the provider split as a real matrix, prove credentials are
resolved per provider, normalize representative terminal events, and run a
small concurrent workload through the durable queue with isolated job state.
They intentionally use fake provider implementations for network boundaries so
CI remains deterministic and free of cloud credentials.
"""
from __future__ import annotations

import asyncio
import pytest

from app.context.builder import ContextBuilder
from app.gate.models import GateCheckRequest
from app.providers.base import CICDProvider, PipelineEvent, ProviderCapabilities, SourceProvider
from app.providers.resolver import ProviderResolver
from app.services.job_queue import LocalJobQueue


class FakeSource(SourceProvider):
    name = "fake-source"
    capabilities = ProviderCapabilities(get_commit_diff=True)

    def __init__(self, token):
        self.token = token

    async def get_commit_diff(self, event):
        return f"diff:{self.token}:{event.commit_sha}"

    async def get_file(self, event, path, ref=None):
        return f"file:{self.token}:{path}"

    async def publish_status(self, event, *, verdict, summary, detail="", details_url=None):
        return None

    async def publish_comment(self, event, body):
        return None

    async def create_fix_pull_request(self, event, *, patch, title, body):
        return {"created": False}


class FakeCICD(CICDProvider):
    name = "fake-cicd"
    capabilities = ProviderCapabilities(get_failure_logs=True)

    def __init__(self, token):
        self.token = token

    async def get_failure_logs(self, event):
        return f"logs:{self.token}:{event.run_id}"


@pytest.mark.parametrize(
    "source,cicd",
    [
        ("github", "github"),
        ("github", "azure"),
        ("github", "gcp"),
        ("github", "aws"),
        ("azure", "azure"),
        ("azure", "gcp"),
        ("azure", "aws"),
    ],
)
def test_provider_matrix_resolves_independent_dimensions(source, cicd):
    pair = ProviderResolver().resolve(
        source_provider=source,
        cicd_provider=cicd,
        provider_tokens={"github": "gh-secret", "azure": "az-secret", "gcp": "gcp-secret", "aws": "aws-secret"},
    )
    assert pair.source.name == source
    assert pair.cicd.name == cicd
    assert pair.source is not pair.cicd or source == cicd
    if hasattr(pair.source, "token"):
        assert pair.source.token == {"github": "gh-secret", "azure": "az-secret"}[source]
    if hasattr(pair.cicd, "token"):
        assert pair.cicd.token == {"github": "gh-secret", "azure": "az-secret", "gcp": "gcp-secret", "aws": "aws-secret"}[cicd]


def test_cross_provider_credentials_never_fall_back_to_other_provider_token():
    resolver = ProviderResolver()
    resolver.source_registry = {"fake": FakeSource}
    resolver.cicd_registry = {"fake-ci": FakeCICD}
    pair = resolver.resolve(
        source_provider="fake",
        cicd_provider="fake-ci",
        provider_tokens={"fake": "SOURCE_ONLY", "fake-ci": "CI_ONLY"},
    )
    assert pair.source.token == "SOURCE_ONLY"
    assert pair.cicd.token == "CI_ONLY"


def test_pipeline_event_preserves_cross_provider_identity():
    event = PipelineEvent(
        provider="azure",
        source_provider="github",
        cicd_provider="azure",
        repository="acme/payments",
        commit_sha="abc123",
        branch="main",
        run_id="77",
        status="failure",
    )
    assert event.is_cross_provider is True
    assert event.source_provider == "github"
    assert event.cicd_provider == "azure"
    assert event.ci_passed is False


@pytest.mark.asyncio
async def test_context_builder_combines_separate_source_and_cicd_evidence():
    source = FakeSource("SOURCE")
    cicd = FakeCICD("CI")
    event = PipelineEvent(
        provider="fake-ci",
        source_provider="fake-source",
        cicd_provider="fake-ci",
        repository="acme/repo",
        commit_sha="abcdef1",
        run_id="42",
        status="failure",
    )
    request = GateCheckRequest(
        repository="acme/repo",
        commit_sha="abcdef1",
        ci_passed=False,
        failure_logs="",
        diff="",
    )
    context = await ContextBuilder().build(request, event=event, source=source, cicd=cicd)
    assert context.logs == "logs:CI:42"
    assert context.diff == "diff:SOURCE:abcdef1"
    assert context.source["provider"] == "fake-source"
    assert context.cicd["provider"] == "fake-ci"


@pytest.mark.asyncio
async def test_ten_concurrent_jobs_complete_independently(tmp_path, monkeypatch):
    """Exercise durable queue concurrency without cloud dependencies."""
    from app.db import base as db_base
    db_path = tmp_path / "phase13.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(db_base, "DATABASE_URL", url, raising=False)
    monkeypatch.setattr(db_base, "engine", db_base.create_engine(url, connect_args={"check_same_thread": False}))
    session_factory = db_base.sessionmaker(bind=db_base.engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db_base, "SessionLocal", session_factory)
    import app.services.job_queue as jq
    monkeypatch.setattr(jq, "SessionLocal", session_factory)
    monkeypatch.setattr(jq, "init_db", db_base.init_db)
    db_base.init_db()

    queue = LocalJobQueue(max_size=20, worker_count=4, max_attempts=1, poll_interval_seconds=0.05)
    seen = {}
    lock = asyncio.Lock()

    async def handler(payload):
        job_id = payload["job_id"]
        await asyncio.sleep(0.01)
        async with lock:
            seen[job_id] = payload["workspace"]

    await queue.start()
    try:
        ids = [f"phase13-{i}" for i in range(10)]
        for i, job_id in enumerate(ids):
            await queue.submit(
                provider="test",
                handler=handler,
                payload={"job_id": job_id, "workspace": str(tmp_path / f"jobs" / job_id)},
                job_id=job_id,
            )
        await queue.wait_for_idle()
    finally:
        await queue.stop()

    assert set(seen) == set(ids)
    assert len(set(seen.values())) == 10
