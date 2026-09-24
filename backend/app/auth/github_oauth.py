"""GitHub OAuth web application flow.

    1. /api/auth/github/login  -> 302 to github.com/login/oauth/authorize
    2. GitHub redirects back to the registered callback URL with ?code=...
    3. The backend exchanges that code for an access token, server to server,
       using the client secret. The secret and the token never touch the
       browser.
    4. The backend calls GET /user with the token to learn who this is.

This module only talks to GitHub. Persisting the result is the router's job.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings


class GitHubOAuthError(RuntimeError):
    """Raised when GitHub rejects an OAuth step."""


def authorize_url(state: str) -> str:
    """Step 1: the URL the browser is redirected to."""
    query = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_callback_url,
            "scope": settings.github_oauth_scope,
            "state": state,
            "allow_signup": "true",
        }
    )
    return f"{settings.github_oauth_base}/authorize?{query}"


async def exchange_code_for_token(code: str) -> dict[str, Any]:
    """Step 3: swap the one-time code for an access token, server-side."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.github_oauth_base}/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_callback_url,
            },
        )

    if response.status_code >= 400:
        raise GitHubOAuthError(
            f"GitHub token exchange failed with HTTP {response.status_code}."
        )

    payload = response.json()

    if "error" in payload:
        raise GitHubOAuthError(
            payload.get("error_description") or str(payload.get("error"))
        )

    if not payload.get("access_token"):
        raise GitHubOAuthError("GitHub did not return an access token.")

    return payload


async def fetch_identity(access_token: str) -> dict[str, Any]:
    """Step 4: resolve the token to a GitHub user profile."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        user_response = await client.get(
            f"{settings.github_api_base}/user", headers=headers
        )
        if user_response.status_code >= 400:
            raise GitHubOAuthError(
                f"Could not read the GitHub profile (HTTP {user_response.status_code})."
            )
        profile = user_response.json()

        # The profile email is null when the user keeps it private, so fall
        # back to the primary verified address from /user/emails.
        if not profile.get("email"):
            emails_response = await client.get(
                f"{settings.github_api_base}/user/emails", headers=headers
            )
            if emails_response.status_code < 400:
                for entry in emails_response.json():
                    if entry.get("primary") and entry.get("verified"):
                        profile["email"] = entry.get("email")
                        break

    return profile


async def revoke_token(access_token: str) -> bool:
    """Best-effort revocation of a token's grant when a user logs out."""
    if not settings.github_configured:
        return False

    url = (
        f"{settings.github_api_base}/applications/"
        f"{settings.github_client_id}/token"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                "DELETE",
                url,
                auth=(settings.github_client_id, settings.github_client_secret),
                headers={"Accept": "application/vnd.github+json"},
                json={"access_token": access_token},
            )
        return response.status_code in (204, 404)
    except httpx.HTTPError:
        return False
