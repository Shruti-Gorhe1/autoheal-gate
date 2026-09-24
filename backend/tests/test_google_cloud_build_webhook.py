"""HTTP-level tests for the Google Cloud Build Pub/Sub push endpoint."""

from __future__ import annotations

import base64
import json

from app.config import settings


BUILD = {
    "id": "build-123",
    "projectId": "demo-project",
    "status": "FAILURE",
    "name": "projects/demo-project/locations/us-central1/builds/build-123",
    "source": {
        "repoSource": {
            "repoName": "acme/payments-api",
            "branchName": "main",
            "commitSha": "a" * 40,
        }
    },
    "substitutions": {"COMMIT_SHA": "a" * 40, "BRANCH_NAME": "main"},
}


def _payload(status="FAILURE"):
    build = {**BUILD, "status": status}
    return {
        "message": {
            "attributes": {"buildId": "build-123", "status": status},
            "data": base64.b64encode(json.dumps(build).encode()).decode(),
        }
    }


def test_gcp_webhook_accepts_completed_build(client, monkeypatch):
    monkeypatch.setattr(settings, "gcp_webhook_secret", "")
    response = client.post("/api/integrations/gcp/webhook", json=_payload())
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["cicd_provider"] == "gcp"


def test_gcp_webhook_rejects_bad_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "gcp_webhook_secret", "secret")
    response = client.post(
        "/api/integrations/gcp/webhook",
        json=_payload(),
        headers={"X-AutoHeal-GCP-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_gcp_webhook_accepts_matching_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "gcp_webhook_secret", "secret")
    response = client.post(
        "/api/integrations/gcp/webhook",
        json=_payload(),
        headers={"X-AutoHeal-GCP-Secret": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_gcp_webhook_rejects_non_terminal_event(client, monkeypatch):
    monkeypatch.setattr(settings, "gcp_webhook_secret", "")
    response = client.post("/api/integrations/gcp/webhook", json=_payload("WORKING"))
    assert response.status_code == 200
    assert response.json()["accepted"] is False
