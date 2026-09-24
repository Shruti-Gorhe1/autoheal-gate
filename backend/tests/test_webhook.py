"""Tests for the GitHub webhook route itself.

This is the endpoint the brief requires to keep working byte-for-byte after
the provider refactor: signature verification, event filtering, and the
hand-off to the gate all still need to behave exactly as before, just routed
through GitHubProvider.normalize_webhook() instead of inline field-picking.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.config import settings
from app.integrations import webhooks as webhook_module

WORKFLOW_RUN_PAYLOAD = {
    "action": "completed",
    "repository": {"full_name": "acme/payments-api"},
    "workflow_run": {
        "id": 123456789,
        "name": "CI",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "c" * 40,
        "head_branch": "main",
        "event": "push",
        "html_url": "https://github.com/acme/payments-api/actions/runs/123456789",
        "actor": {"login": "octocat"},
        "pull_requests": [],
    },
}


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    yield


def _post(client, payload, event="workflow_run", secret="test-webhook-secret"):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/api/integrations/github/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body, secret),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "test-delivery-1",
        },
    )


# --------------------------------------------------------------- signature


def test_an_invalid_signature_is_rejected(client):
    body = json.dumps(WORKFLOW_RUN_PAYLOAD).encode("utf-8")
    response = client.post(
        "/api/integrations/github/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Event": "workflow_run",
        },
    )
    assert response.status_code == 401


def test_a_valid_signature_is_accepted(client):
    response = _post(client, WORKFLOW_RUN_PAYLOAD)
    assert response.status_code == 200
    assert response.json()["accepted"] is True


# ------------------------------------------------------------------- events


def test_a_ping_event_is_answered_without_processing(client):
    response = _post(client, {"zen": "hello"}, event="ping")
    assert response.status_code == 200
    assert response.json() == {"pong": True}


def test_a_non_workflow_run_event_is_not_accepted(client):
    response = _post(client, {"action": "opened"}, event="pull_request")
    assert response.json()["accepted"] is False


def test_an_incomplete_run_is_not_accepted(client):
    payload = {
        "action": "in_progress",
        "repository": {"full_name": "acme/x"},
        "workflow_run": {"id": 1, "status": "in_progress", "head_sha": "a" * 40},
    }
    response = _post(client, payload)
    assert response.json()["accepted"] is False


def test_the_gates_own_workflow_is_ignored_to_avoid_a_loop(client):
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/x"},
        "workflow_run": {
            "id": 1,
            "name": "AutoHeal Gate",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "a" * 40,
        },
    }
    response = _post(client, payload)
    assert response.json()["accepted"] is False


def test_accepted_response_reports_the_repository_and_commit(client):
    response = _post(client, WORKFLOW_RUN_PAYLOAD)
    body = response.json()
    assert body["repository"] == "acme/payments-api"
    assert body["commit"] == "c" * 40


# ------------------------------------------------------ end-to-end hand-off


@pytest.mark.asyncio
async def test_process_workflow_run_normalizes_through_the_provider_and_calls_the_gate(
    monkeypatch,
):
    """The actual hand-off: process_workflow_run must build the exact same
    GateCheckRequest fields the old inline code used to construct by hand,
    now sourced from GitHubProvider.normalize_webhook()."""
    captured = {}

    async def fake_run_gate(db, request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs

        class _Result:
            allowed = True

        return _Result()

    monkeypatch.setattr(webhook_module.service, "run_gate", fake_run_gate)
    monkeypatch.setattr(
        webhook_module, "_token_for_repository", lambda db, repo: (None, None, "webhook")
    )

    await webhook_module.process_workflow_run(WORKFLOW_RUN_PAYLOAD)

    request = captured["request"]
    assert request.repository == "acme/payments-api"
    assert request.commit_sha == "c" * 40
    assert request.branch == "main"
    assert request.workflow_run_id == 123456789
    assert request.pull_request_number is None
    assert request.ci_passed is False
    assert request.metadata["workflow_name"] == "CI"
    assert request.metadata["actor"] == "octocat"
    assert captured["kwargs"]["source"] == "webhook"


@pytest.mark.asyncio
async def test_process_workflow_run_is_a_noop_for_an_unrecognized_payload(monkeypatch):
    def unexpected(*_a, **_k):
        raise AssertionError("run_gate should not be called for a payload with no run")

    monkeypatch.setattr(webhook_module.service, "run_gate", unexpected)
    await webhook_module.process_workflow_run({"repository": {"full_name": "acme/x"}})
