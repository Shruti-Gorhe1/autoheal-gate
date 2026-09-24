"""Authentication and credential endpoints."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import github_oauth, session_store
from app.auth.deps import Principal, require_principal, require_session
from app.config import settings
from app.db.base import get_db
from app.db.models import ApiKey, utcnow
from app.security.crypto import encrypt, generate_api_key
from app.services.github_client import GitHubClient

logger = logging.getLogger("autoheal.auth")

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


# ===========================================================================
# GitHub OAuth
# ===========================================================================


@router.get("/github/login", summary="Start GitHub sign-in")
def github_login(
    request: Request,
    redirect_to: str | None = Query(
        default=None,
        description="Where to send the browser after a successful sign-in.",
    ),
    db: Session = Depends(get_db),
):
    """Redirect the browser to GitHub's authorization screen."""
    if not settings.github_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "GitHub sign-in is not configured. Set GITHUB_CLIENT_ID and "
                "GITHUB_CLIENT_SECRET in backend/.env and restart."
            ),
        )

    session_store.purge_expired_states(db)
    state = session_store.create_oauth_state(
        db, redirect_to=redirect_to or settings.frontend_base_url
    )

    record_audit(db, action="auth.login_started", actor="anonymous", outcome="ok")
    return RedirectResponse(
        github_oauth.authorize_url(state), status_code=status.HTTP_302_FOUND
    )


@router.get("/github/callback", summary="Complete GitHub sign-in")
async def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    """Exchange the one-time code for a token and open a server-side session."""

    def failure(message: str) -> RedirectResponse:
        record_audit(
            db,
            action="auth.login_failed",
            actor="anonymous",
            outcome="error",
            detail={"message": message},
        )
        target = f"{settings.frontend_base_url}?{urlencode({'auth_error': message})}"
        return RedirectResponse(target, status_code=status.HTTP_302_FOUND)

    if error:
        return failure(error_description or error)
    if not code or not state:
        return failure("GitHub did not return an authorization code.")

    state_record = session_store.consume_oauth_state(db, state)
    if state_record is None:
        return failure("The sign-in request expired or was already used.")

    try:
        token_payload = await github_oauth.exchange_code_for_token(code)
        access_token = token_payload["access_token"]
        profile = await github_oauth.fetch_identity(access_token)
    except github_oauth.GitHubOAuthError as exc:
        return failure(str(exc))
    except Exception as exc:  # pragma: no cover - network failure path
        logger.exception("GitHub OAuth callback failed")
        return failure(f"Sign-in failed: {exc}")

    user = session_store.upsert_user(db, profile)
    auth_session = session_store.create_session(
        db,
        user=user,
        access_token=access_token,
        scope=token_payload.get("scope"),
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )

    record_audit(
        db,
        action="auth.login_succeeded",
        actor=user.login,
        target=f"session:{auth_session.id[:8]}",
    )

    target = state_record.redirect_to or settings.frontend_base_url
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    # Only the opaque session id goes to the browser. The GitHub token stays
    # in the database, encrypted.
    response.set_cookie(
        key=settings.session_cookie_name,
        value=auth_session.id,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return response


@router.get("/me", summary="Current signed-in user")
def me(principal: Principal = Depends(require_principal)):
    user = principal.user
    return {
        "authenticated": True,
        "credential": principal.kind,
        "user": {
            "id": user.id,
            "login": user.login,
            "name": user.name,
            "email": user.email,
            "avatar_url": user.avatar_url,
            "is_admin": user.is_admin,
        },
        "github_token_available": principal.can_call_github,
    }


@router.get("/session", summary="Session probe that never returns 401")
def session_probe(request: Request, db: Session = Depends(get_db)):
    """Lets the dashboard render a signed-out state without an error."""
    from app.auth.deps import optional_principal

    principal = optional_principal(request, db)
    if principal is None:
        return {
            "authenticated": False,
            "github_configured": settings.github_configured,
            "login_url": "/api/auth/github/login",
        }
    return me(principal)


@router.post("/logout", summary="Sign out")
async def logout(
    response: Response,
    request: Request,
    revoke_github_grant: bool = Query(
        default=False,
        description="Also ask GitHub to revoke the stored access token.",
    ),
    db: Session = Depends(get_db),
):
    from app.auth.deps import optional_principal

    principal = optional_principal(request, db)
    if principal and principal.kind == "session":
        session = session_store.get_active_session(db, principal.credential_id)
        if session is not None:
            if revoke_github_grant and principal.access_token:
                await github_oauth.revoke_token(principal.access_token)
            session_store.revoke_session(db, session)
        record_audit(db, action="auth.logout", actor=principal.login)

    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"signed_out": True}


# ===========================================================================
# GitHub resources reached with the stored token
# ===========================================================================


@router.get("/github/repos", summary="Repositories the signed-in user can access")
async def list_repos(
    page: int = Query(default=1, ge=1, le=20),
    per_page: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_principal),
):
    if not principal.can_call_github:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No GitHub token is attached to this credential.",
        )
    client = GitHubClient(principal.access_token)
    return {"repositories": await client.list_repositories(page=page, per_page=per_page)}


# ===========================================================================
# API keys for CI runners
# ===========================================================================


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


@router.post("/api-keys", status_code=201, summary="Issue a CI API key")
def create_api_key(
    payload: ApiKeyCreate,
    principal: Principal = Depends(require_session),
    db: Session = Depends(get_db),
):
    """Issue a key for a CI runner.

    The key inherits the issuing user's GitHub token so the gate can post
    check runs and open pull requests from a pipeline. The full key is
    returned once and is not recoverable afterwards.
    """
    full_key, prefix, key_hash = generate_api_key()

    record = ApiKey(
        user_id=principal.user.id,
        name=payload.name,
        prefix=prefix,
        key_hash=key_hash,
        access_token_encrypted=(
            encrypt(principal.access_token) if principal.access_token else None
        ),
    )
    db.add(record)
    db.commit()

    record_audit(
        db, action="apikey.created", actor=principal.login, target=payload.name
    )

    return {
        "id": record.id,
        "name": record.name,
        "prefix": record.prefix,
        "api_key": full_key,
        "warning": "Copy this key now. It is not shown again.",
    }


@router.get("/api-keys", summary="List your CI API keys")
def list_api_keys(
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    keys = db.scalars(
        select(ApiKey).where(ApiKey.user_id == principal.user.id)
    ).all()
    return {
        "api_keys": [
            {
                "id": key.id,
                "name": key.name,
                "prefix": key.prefix,
                "created_at": key.created_at,
                "last_used_at": key.last_used_at,
                "revoked": not key.active,
            }
            for key in keys
        ]
    }


@router.delete("/api-keys/{key_id}", summary="Revoke a CI API key")
def revoke_api_key(
    key_id: str,
    principal: Principal = Depends(require_session),
    db: Session = Depends(get_db),
):
    record = db.get(ApiKey, key_id)
    if record is None or record.user_id != principal.user.id:
        raise HTTPException(status_code=404, detail="API key not found.")

    record.revoked_at = utcnow()
    db.commit()
    record_audit(db, action="apikey.revoked", actor=principal.login, target=record.name)
    return {"revoked": True, "id": key_id}
