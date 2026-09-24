"""Small Google Cloud Build REST client used by the GCP CI/CD adapter.

The client intentionally uses REST rather than the full Google Cloud SDK so
AutoHeal keeps a small dependency footprint. Authentication is resolved
server-side from an explicit access token or Application Default Credentials.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx


class GoogleCloudBuildError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GoogleCloudBuildClient:
    def __init__(
        self,
        access_token: str | None = None,
        *,
        project_id: str | None = None,
        location: str | None = None,
        base_url: str = "https://cloudbuild.googleapis.com",
    ):
        self.access_token = access_token or os.getenv("GOOGLE_CLOUD_ACCESS_TOKEN", "")
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.location = location or os.getenv("GOOGLE_CLOUD_LOCATION", "global")
        self.base_url = base_url.rstrip("/")

    def _auth_token(self) -> str | None:
        if self.access_token:
            return self.access_token

        # Avoid probing the metadata server on a plain local development
        # machine. ADC is attempted only when an explicit credentials file is
        # configured or the standard local gcloud ADC file exists.
        credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        if credentials_file and not os.path.exists(credentials_file):
            return None
        if not credentials_file:
            home = os.path.expanduser("~")
            candidates = [
                os.path.join(home, ".config", "gcloud", "application_default_credentials.json"),
                os.path.join(os.getenv("APPDATA", ""), "gcloud", "application_default_credentials.json"),
            ]
            if not any(path and os.path.exists(path) for path in candidates):
                return None

        try:
            import google.auth
            from google.auth.transport.requests import Request

            credentials, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if not self.project_id:
                self.project_id = project or ""
            credentials.refresh(Request())
            return credentials.token
        except Exception:
            return None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = self._auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _build_url(self, build_id: str, location: str | None = None) -> str:
        project = quote(self.project_id, safe="")
        build = quote(str(build_id), safe="")
        loc = location or self.location
        if loc and loc != "global":
            return f"{self.base_url}/v1/projects/{project}/locations/{quote(loc, safe='')}/builds/{build}"
        return f"{self.base_url}/v1/projects/{project}/builds/{build}"

    async def get_build(self, build_id: str, *, location: str | None = None) -> dict[str, Any]:
        if not self.project_id:
            raise GoogleCloudBuildError("Google Cloud project is not configured.")
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(self._build_url(build_id, location), headers=self._headers())
        if response.status_code >= 400:
            raise GoogleCloudBuildError(
                f"Cloud Build get failed with HTTP {response.status_code}: {response.text[:400]}",
                response.status_code,
            )
        return response.json()

    async def get_failure_logs(
        self,
        build_id: str,
        *,
        location: str | None = None,
        build: dict[str, Any] | None = None,
        max_chars: int = 200_000,
    ) -> str:
        """Fetch build logs from the Cloud Storage log object when available.

        Cloud Build exposes ``logsBucket`` on the Build resource and stores the
        log object as ``log-{buildId}.txt``. Builds using Cloud Logging only do
        not expose a downloadable object through this client yet; in that case
        the returned message includes the Cloud Console log URL instead.
        """
        build = build or await self.get_build(build_id, location=location)
        logs_bucket = str(build.get("logsBucket") or "")
        log_url = build.get("logUrl")
        if not logs_bucket:
            if log_url:
                return f"Cloud Build logs are available at: {log_url}"
            return "Cloud Build did not expose a downloadable logs bucket for this build."

        bucket = logs_bucket.removeprefix("gs://").rstrip("/")
        object_name = f"log-{build_id}.txt"
        url = (
            "https://storage.googleapis.com/storage/v1/b/"
            f"{quote(bucket, safe='')}/o/{quote(object_name, safe='')}?alt=media"
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers=self._headers())
        if response.status_code >= 400:
            detail = response.text[:300]
            return f"Could not download Cloud Build logs: HTTP {response.status_code}: {detail}"
        text = response.text
        return text[-max_chars:]

    @staticmethod
    def decode_pubsub_build(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Decode a Cloud Build Pub/Sub push envelope.

        Google documents ``message.data`` as base64 JSON containing the Build
        resource and ``message.attributes`` as containing buildId/status.
        Direct Build JSON is also accepted for local testing.
        """
        message = payload.get("message")
        if not isinstance(message, dict):
            if payload.get("id") or payload.get("status"):
                return payload, str(payload.get("id")) if payload.get("id") is not None else None
            return None, None

        data = message.get("data")
        build: dict[str, Any] | None = None
        if data:
            try:
                import base64

                decoded = base64.b64decode(data).decode("utf-8")
                parsed = json.loads(decoded)
                if isinstance(parsed, dict):
                    build = parsed
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return None, None

        attrs = message.get("attributes") or {}
        build_id = str(attrs.get("buildId") or (build or {}).get("id") or "") or None
        return build, build_id
