"""HTTP-level tests for the AWS EventBridge webhook."""
from __future__ import annotations

from app.config import settings


def _payload(status="FAILED"):
    return {
        "version": "0",
        "detail-type": "CodeBuild Build State Change",
        "source": "aws.codebuild",
        "region": "us-east-1",
        "detail": {
            "build-status": status,
            "project-name": "payments-build",
            "build-id": "arn:aws:codebuild:us-east-1:123:build/payments-build:build-123",
            "repository": "acme/payments-api",
            "commit-sha": "a" * 40,
            "source-provider": "github",
        },
    }


def test_aws_webhook_accepts_completed_build(client, monkeypatch):
    monkeypatch.setattr(settings, "aws_webhook_secret", "")
    response = client.post("/api/integrations/aws/webhook", json=_payload())
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["cicd_provider"] == "aws"


def test_aws_webhook_rejects_bad_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "aws_webhook_secret", "secret")
    response = client.post(
        "/api/integrations/aws/webhook",
        json=_payload(),
        headers={"X-AutoHeal-Aws-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_aws_webhook_accepts_matching_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "aws_webhook_secret", "secret")
    response = client.post(
        "/api/integrations/aws/webhook",
        json=_payload(),
        headers={"X-AutoHeal-Aws-Secret": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_aws_webhook_rejects_non_aws_event(client, monkeypatch):
    monkeypatch.setattr(settings, "aws_webhook_secret", "")
    response = client.post(
        "/api/integrations/aws/webhook",
        json={"source": "aws.s3", "detail-type": "Object Created", "detail": {}},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_aws_webhook_accepts_real_codebuild_event_for_worker_hydration(client, monkeypatch):
    monkeypatch.setattr(settings, "aws_webhook_secret", "")
    payload = _payload()
    payload["detail"].pop("repository")
    payload["detail"].pop("commit-sha")
    response = client.post("/api/integrations/aws/webhook", json=payload)
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["hydrated"] is False
