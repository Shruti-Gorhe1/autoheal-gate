"""Tests for the provider abstraction.

These pin two things: that GitHubProvider's webhook normalization produces
exactly the PipelineEvent the old inline webhook code used to build by hand,
and that each provider method genuinely delegates to the existing, already-
tested GitHubClient rather than reimplementing GitHub API calls.
"""

from __future__ import annotations

from app.providers.base import CICDProvider, PipelineEvent, SourceProvider
from app.providers.github import GitHubProvider

WORKFLOW_RUN_PAYLOAD = {
    "action": "completed",
    "repository": {"full_name": "acme/payments-api"},
    "workflow_run": {
        "id": 123456789,
        "name": "CI",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "conclusion": "failure",
        "status": "completed",
        "event": "push",
        "html_url": "https://github.com/acme/payments-api/actions/runs/123456789",
        "actor": {"login": "octocat"},
        "pull_requests": [{"number": 42}],
    },
}


# --------------------------------------------------------------- PipelineEvent


def test_ci_passed_is_derived_from_status():
    assert PipelineEvent(
        provider="github", repository="acme/x", commit_sha="a" * 40, status="success"
    ).ci_passed is True
    assert PipelineEvent(
        provider="github", repository="acme/x", commit_sha="a" * 40, status="failure"
    ).ci_passed is False
    assert PipelineEvent(
        provider="github", repository="acme/x", commit_sha="a" * 40
    ).ci_passed is False  # default status is "unknown"


def test_a_provider_must_implement_the_interface():
    """CICDProvider is an ABC; a provider missing a required method cannot
    be instantiated, which is what keeps every provider honestly complete."""
    import pytest

    class Incomplete(CICDProvider):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # missing every abstract method


# ---------------------------------------------- source / CI-CD independence


def test_source_and_cicd_provider_default_from_the_shared_provider_field():
    event = PipelineEvent(
        provider="github", repository="acme/x", commit_sha="a" * 40
    )
    assert event.source_provider == "github"
    assert event.cicd_provider == "github"
    assert event.is_cross_provider is False


def test_source_and_cicd_provider_can_be_set_independently():
    """The case this split exists for: a GitHub repository built by Azure
    Pipelines. No AzureProvider exists yet, but the event shape must
    already be able to represent this combination."""
    event = PipelineEvent(
        provider="azure",
        source_provider="github",
        cicd_provider="azure",
        repository="acme/payments-api",
        commit_sha="a" * 40,
    )
    assert event.source_provider == "github"
    assert event.cicd_provider == "azure"
    assert event.is_cross_provider is True


def test_source_provider_and_cicd_provider_are_genuinely_separate_interfaces():
    """A class implementing only the CI/CD side must not satisfy the
    source-provider contract, and vice versa -- proving the split is real,
    not just two names for the same set of methods."""
    import pytest

    class LogsOnly(CICDProvider):
        name = "logs-only"

        async def get_failure_logs(self, event):
            return "logs"

    # A complete CICDProvider does NOT thereby implement SourceProvider.
    assert isinstance(LogsOnly(), CICDProvider)
    assert not isinstance(LogsOnly(), SourceProvider)


def test_github_provider_satisfies_both_interfaces():
    """GitHub is both the source and the CI/CD system in the common case,
    so one class legitimately implements both roles."""
    provider = GitHubProvider("gho_test")
    assert isinstance(provider, SourceProvider)
    assert isinstance(provider, CICDProvider)


def test_get_commit_diff_is_a_source_provider_responsibility_not_cicd():
    assert hasattr(SourceProvider, "get_commit_diff")
    assert not hasattr(CICDProvider, "get_commit_diff")


def test_get_failure_logs_is_a_cicd_provider_responsibility_not_source():
    assert hasattr(CICDProvider, "get_failure_logs")
    assert not hasattr(SourceProvider, "get_failure_logs")


# ---------------------------------------------------- GitHub webhook normalization


def test_a_workflow_run_webhook_normalizes_correctly():
    event = GitHubProvider.normalize_webhook(WORKFLOW_RUN_PAYLOAD)

    assert event is not None
    assert event.provider == "github"
    assert event.repository == "acme/payments-api"
    assert event.commit_sha == "b" * 40
    assert event.branch == "main"
    assert event.run_id == "123456789"
    assert event.change_request_number == 42
    assert event.status == "failure"
    assert event.ci_passed is False
    assert event.metadata["workflow_name"] == "CI"
    assert event.metadata["actor"] == "octocat"
    assert event.raw_payload == WORKFLOW_RUN_PAYLOAD


def test_a_successful_run_normalizes_to_success():
    payload = {
        "repository": {"full_name": "acme/x"},
        "workflow_run": {
            "id": 1,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "conclusion": "success",
            "pull_requests": [],
        },
    }
    event = GitHubProvider.normalize_webhook(payload)
    assert event.status == "success"
    assert event.ci_passed is True
    assert event.change_request_number is None


def test_a_cancelled_run_is_distinct_from_a_failure():
    payload = {
        "repository": {"full_name": "acme/x"},
        "workflow_run": {"id": 1, "head_sha": "a" * 40, "conclusion": "cancelled"},
    }
    event = GitHubProvider.normalize_webhook(payload)
    assert event.status == "cancelled"
    assert event.ci_passed is False


def test_a_payload_without_a_workflow_run_is_rejected():
    assert GitHubProvider.normalize_webhook({"repository": {"full_name": "acme/x"}}) is None


def test_a_payload_without_a_repository_is_rejected():
    assert GitHubProvider.normalize_webhook({"workflow_run": {"id": 1}}) is None


# --------------------------------------------------------------- delegation


def test_get_commit_diff_delegates_to_github_client(monkeypatch):
    calls = []

    async def fake_get_commit_diff(self, repo, sha):
        calls.append((repo, sha))
        return "diff --git a/x.py b/x.py\n"

    monkeypatch.setattr(
        "app.services.github_client.GitHubClient.get_commit_diff", fake_get_commit_diff
    )

    provider = GitHubProvider("gho_test")
    event = GitHubProvider.normalize_webhook(WORKFLOW_RUN_PAYLOAD)

    import asyncio

    result = asyncio.run(provider.get_commit_diff(event))
    assert result == "diff --git a/x.py b/x.py\n"
    assert calls == [("acme/payments-api", "b" * 40)]


def test_get_commit_diff_without_a_token_makes_no_call(monkeypatch):
    def unexpected(*_a, **_k):
        raise AssertionError("should not call GitHubClient without a token")

    monkeypatch.setattr(
        "app.services.github_client.GitHubClient.get_commit_diff", unexpected
    )

    provider = GitHubProvider(None)
    event = GitHubProvider.normalize_webhook(WORKFLOW_RUN_PAYLOAD)

    import asyncio

    assert asyncio.run(provider.get_commit_diff(event)) == ""


def test_get_file_sync_delegates_to_the_module_level_helper(monkeypatch):
    def fake_fetch(token, repo, path, ref):
        return f"{token}:{repo}:{path}:{ref}"

    monkeypatch.setattr("app.services.github_client.fetch_file_sync", fake_fetch)

    provider = GitHubProvider("gho_test")
    result = provider.get_file_sync("acme/x", "deadbeef", "src/a.py")
    assert result == "gho_test:acme/x:src/a.py:deadbeef"


def test_download_snapshot_sync_delegates_to_the_module_level_helper(monkeypatch):
    def fake_download(token, repo, ref, dest):
        return f"{dest}/{repo}-{ref[:7]}"

    monkeypatch.setattr(
        "app.services.github_client.download_repository_snapshot", fake_download
    )

    provider = GitHubProvider("gho_test")
    result = provider.download_snapshot_sync("acme/x", "deadbeefcafe", "/tmp/ws")
    assert result == "/tmp/ws/acme/x-deadbee"


def test_publish_status_falls_back_to_commit_status_on_check_run_failure(monkeypatch):
    from app.services.github_client import GitHubError

    calls = {"check_run": 0, "commit_status": 0}

    async def fail_check_run(self, *a, **k):
        calls["check_run"] += 1
        raise GitHubError("no checks permission", status_code=403)

    async def ok_commit_status(self, repo, sha, *, state, description, target_url=None, context="autoheal/gate"):
        calls["commit_status"] += 1
        return {}

    monkeypatch.setattr(
        "app.services.github_client.GitHubClient.create_check_run", fail_check_run
    )
    monkeypatch.setattr(
        "app.services.github_client.GitHubClient.create_commit_status", ok_commit_status
    )

    provider = GitHubProvider("gho_test")
    event = GitHubProvider.normalize_webhook(WORKFLOW_RUN_PAYLOAD)

    import asyncio

    asyncio.run(provider.publish_status(event, verdict="PASS", summary="ok"))
    assert calls == {"check_run": 1, "commit_status": 1}


def test_publish_comment_is_a_noop_without_a_change_request(monkeypatch):
    def unexpected(*_a, **_k):
        raise AssertionError("should not comment without a PR number")

    monkeypatch.setattr(
        "app.services.github_client.GitHubClient.comment_on_pull_request", unexpected
    )

    provider = GitHubProvider("gho_test")
    payload = {
        "repository": {"full_name": "acme/x"},
        "workflow_run": {"id": 1, "head_sha": "a" * 40, "conclusion": "failure", "pull_requests": []},
    }
    event = GitHubProvider.normalize_webhook(payload)

    import asyncio

    asyncio.run(provider.publish_comment(event, "hello"))  # must not raise


def test_github_advertises_only_implemented_capabilities():
    provider = GitHubProvider("gho_test")
    caps = provider.capabilities
    assert caps.get_commit_diff is True
    assert caps.get_file is True
    assert caps.download_snapshot is True
    assert caps.publish_status is True
    assert caps.publish_comment is True
    assert caps.create_fix_pull_request is True
    assert caps.get_failure_logs is True


def test_pipeline_event_supports_common_cross_provider_combinations():
    combinations = [
        ("github", "github"),
        ("github", "azure"),
        ("github", "gcp"),
        ("github", "aws"),
        ("azure", "azure"),
        ("azure", "github"),
    ]
    for source, cicd in combinations:
        event = PipelineEvent(
            provider=cicd,
            source_provider=source,
            cicd_provider=cicd,
            repository="acme/demo",
            commit_sha="a" * 40,
        )
        assert event.source_provider == source
        assert event.cicd_provider == cicd
        assert event.is_cross_provider is (source != cicd)
