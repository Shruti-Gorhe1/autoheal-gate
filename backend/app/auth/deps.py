"""Request authentication.

Two credential types resolve to the same ``Principal``:

  * a session cookie, for people using the dashboard
  * an ``X-API-Key`` header, for CI runners, which cannot follow an OAuth
    browser redirect

Both carry a GitHub access token that the backend uses on the caller's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import session_store
from app.config import settings
from app.db.base import get_db
from app.db.models import ApiKey, User, utcnow
from app.security.crypto import decrypt, hash_api_key

API_KEY_HEADER = "X-API-Key"


@dataclass
class Principal:
    """Who is making this request, and what they can do on GitHub."""

    user: User
    kind: str  # "session" | "api_key"
    access_token: str | None
    credential_id: str

    @property
    def login(self) -> str:
        return self.user.login

    @property
    def can_call_github(self) -> bool:
        return bool(self.access_token)


def _principal_from_cookie(request: Request, db: Session) -> Principal | None:
    session_id = request.cookies.get(settings.session_cookie_name)
    session = session_store.get_active_session(db, session_id)
    if session is None:
        return None

    try:
        token = session_store.get_access_token(session)
    except ValueError:
        return None

    return Principal(
        user=session.user,
        kind="session",
        access_token=token,
        credential_id=session.id,
    )


def _principal_from_api_key(request: Request, db: Session) -> Principal | None:
    raw_key = request.headers.get(API_KEY_HEADER)
    if not raw_key:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer ahg_"):
            raw_key = header.split(" ", 1)[1]
    if not raw_key:
        return None

    record = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key)))
    if record is None or not record.active:
        return None

    record.last_used_at = utcnow()
    db.commit()

    token = None
    if record.access_token_encrypted:
        try:
            token = decrypt(record.access_token_encrypted)
        except ValueError:
            token = None

    return Principal(
        user=record.user,
        kind="api_key",
        access_token=token,
        credential_id=record.id,
    )


def optional_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal | None:
    return _principal_from_cookie(request, db) or _principal_from_api_key(request, db)


def require_principal(
    principal: Principal | None = Depends(optional_principal),
) -> Principal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Authentication required. Sign in at /api/auth/github/login, "
                f"or send a {API_KEY_HEADER} header."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_session(
    request: Request, db: Session = Depends(get_db)
) -> Principal:
    """Endpoints that must come from a browser session, not a CI key."""
    principal = _principal_from_cookie(request, db)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with GitHub to use this endpoint.",
        )
    return principal


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if not principal.user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an administrator account.",
        )
    return principal
