"""Authentication tests.

The property that matters most: a GitHub access token must never leave the
server. Several tests below exist purely to pin that down.
"""

from __future__ import annotations

import httpx
import pytest

from app.auth import github_oauth, session_store
from app.config import settings
from app.security.crypto import decrypt, encrypt, generate_api_key, hash_api_key


# ------------------------------------------------------------------- crypto


def test_a_token_round_trips_through_encryption():
    token = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
    assert decrypt(encrypt(token)) == token


def test_ciphertext_does_not_contain_the_token():
    token = "gho_supersecretvalue"
    assert token not in encrypt(token)


def test_encryption_is_not_deterministic():
    """Two encryptions of the same token must differ, or ciphertext leaks equality."""
    assert encrypt("same") != encrypt("same")


def test_an_api_key_is_stored_only_as_a_hash():
    full, prefix, digest = generate_api_key()
    assert full.startswith("ahg_")
    assert full.startswith(prefix)
    assert digest == hash_api_key(full)
    assert full not in digest


# -------------------------------------------------------------- oauth steps


def test_the_authorize_url_carries_the_required_parameters():
    url = github_oauth.authorize_url("state-123")
    assert url.startswith("https://github.com/login/oauth/authorize?")
    for fragment in ("client_id=test-client-id", "state=state-123", "redirect_uri="):
        assert fragment in url


def test_the_authorize_url_never_contains_the_client_secret():
    assert settings.github_client_secret not in github_oauth.authorize_url("s")


def test_login_redirects_the_browser_to_github(client):
    response = client.get("/api/auth/github/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://github.com/login/oauth/authorize"
    )


def test_a_state_value_can_only_be_used_once(db):
    state = session_store.create_oauth_state(db, redirect_to="/dashboard")
    assert session_store.consume_oauth_state(db, state) is not None
    assert session_store.consume_oauth_state(db, state) is None


def test_an_unknown_state_is_rejected(db):
    assert session_store.consume_oauth_state(db, "never-issued") is None


def test_the_callback_rejects_a_forged_state(client):
    response = client.get(
        "/api/auth/github/callback",
        params={"code": "abc", "state": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "auth_error" in response.headers["location"]


@pytest.mark.asyncio
async def test_a_github_error_during_exchange_is_surfaced(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={"error": "bad_verification_code", "error_description": "expired"},
                request=httpx.Request("POST", "https://github.com"),
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeClient())

    with pytest.raises(github_oauth.GitHubOAuthError, match="expired"):
        await github_oauth.exchange_code_for_token("dead-code")


# ---------------------------------------------------------------- sessions


def test_a_session_stores_the_token_encrypted(db, user):
    session = session_store.create_session(db, user=user, access_token="gho_secret")
    assert "gho_secret" not in session.access_token_encrypted
    assert session_store.get_access_token(session) == "gho_secret"


def test_a_revoked_session_stops_authenticating(db, user, client):
    session = session_store.create_session(db, user=user, access_token="gho_secret")
    client.cookies.set(settings.session_cookie_name, session.id)
    assert client.get("/api/auth/me").status_code == 200

    session_store.revoke_session(db, session)
    assert client.get("/api/auth/me").status_code == 401


def test_the_session_probe_reports_a_signed_out_visitor(client):
    client.cookies.clear()
    payload = client.get("/api/auth/session").json()
    assert payload["authenticated"] is False
    assert payload["login_url"] == "/api/auth/github/login"


def test_me_returns_the_profile_without_the_token(auth_client):
    payload = auth_client.get("/api/auth/me").json()
    assert payload["authenticated"] is True
    assert payload["user"]["login"] == "octocat"
    assert "gho_test_token" not in str(payload)


def test_signing_out_clears_the_cookie(auth_client):
    assert auth_client.post("/api/auth/logout").json()["signed_out"] is True
    assert auth_client.get("/api/auth/me").status_code == 401


def test_protected_endpoints_reject_anonymous_callers(client):
    client.cookies.clear()
    assert client.get("/api/gate/runs").status_code == 401


# ---------------------------------------------------------------- api keys


def test_an_api_key_is_shown_once_then_authenticates(auth_client):
    created = auth_client.post("/api/auth/api-keys", json={"name": "ci-runner"})
    assert created.status_code == 201
    key = created.json()["api_key"]

    listed = auth_client.get("/api/auth/api-keys").json()["api_keys"]
    assert all("api_key" not in entry for entry in listed)

    auth_client.cookies.clear()
    response = auth_client.get("/api/auth/me", headers={"X-API-Key": key})
    assert response.status_code == 200
    assert response.json()["credential"] == "api_key"


def test_a_revoked_api_key_stops_working(auth_client):
    created = auth_client.post("/api/auth/api-keys", json={"name": "temp"}).json()
    auth_client.delete(f"/api/auth/api-keys/{created['id']}")

    auth_client.cookies.clear()
    response = auth_client.get("/api/auth/me", headers={"X-API-Key": created["api_key"]})
    assert response.status_code == 401


def test_an_invented_api_key_is_rejected(client):
    client.cookies.clear()
    response = client.get("/api/auth/me", headers={"X-API-Key": "ahg_not_a_real_key"})
    assert response.status_code == 401


def test_issuing_a_key_requires_a_browser_session(client):
    """A CI key must not be able to mint more keys for itself."""
    client.cookies.clear()
    response = client.post(
        "/api/auth/api-keys",
        json={"name": "escalation"},
        headers={"X-API-Key": "ahg_whatever"},
    )
    assert response.status_code == 401
