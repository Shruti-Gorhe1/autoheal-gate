"""Server-side session store.

The mapping ``session_id -> github_access_token`` lives here, in the database,
encrypted. The browser receives only the session id, in an httpOnly cookie.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AuthSession, OAuthState, User, utcnow
from app.security.crypto import decrypt, encrypt, new_oauth_state, new_session_id

OAUTH_STATE_TTL_MINUTES = 10


# --------------------------------------------------------------------- state


def create_oauth_state(db: Session, redirect_to: str | None = None) -> str:
    state = new_oauth_state()
    db.add(
        OAuthState(
            state=state,
            redirect_to=redirect_to,
            expires_at=utcnow() + timedelta(minutes=OAUTH_STATE_TTL_MINUTES),
        )
    )
    db.commit()
    return state


def consume_oauth_state(db: Session, state: str) -> OAuthState | None:
    """Validate and burn a state value. Returns None if it is not usable."""
    record = db.get(OAuthState, state)
    if record is None or record.consumed:
        return None

    expires = record.expires_at
    if expires.tzinfo is None:
        from datetime import timezone

        expires = expires.replace(tzinfo=timezone.utc)
    if expires < utcnow():
        return None

    record.consumed = True
    db.commit()
    return record


def purge_expired_states(db: Session) -> int:
    stale = db.scalars(select(OAuthState).where(OAuthState.expires_at < utcnow())).all()
    for record in stale:
        db.delete(record)
    db.commit()
    return len(stale)


# ------------------------------------------------------------------- session


def create_session(
    db: Session,
    user: User,
    access_token: str,
    scope: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthSession:
    session = AuthSession(
        id=new_session_id(),
        user_id=user.id,
        access_token_encrypted=encrypt(access_token),
        token_scope=scope,
        user_agent=(user_agent or "")[:512] or None,
        ip_address=(ip_address or "")[:64] or None,
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    db.commit()
    return session


def get_active_session(db: Session, session_id: str | None) -> AuthSession | None:
    if not session_id:
        return None
    session = db.get(AuthSession, session_id)
    if session is None or not session.active:
        return None
    return session


def get_access_token(session: AuthSession) -> str:
    """Decrypt the GitHub token for a server-side API call."""
    return decrypt(session.access_token_encrypted)


def revoke_session(db: Session, session: AuthSession) -> None:
    session.revoked_at = utcnow()
    db.commit()


def revoke_all_sessions(db: Session, user: User) -> int:
    sessions = db.scalars(
        select(AuthSession).where(
            AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
        )
    ).all()
    for session in sessions:
        session.revoked_at = utcnow()
    db.commit()
    return len(sessions)


# ---------------------------------------------------------------------- user


def upsert_user(db: Session, profile: dict) -> User:
    github_id = int(profile["id"])
    user = db.scalar(select(User).where(User.github_id == github_id))

    if user is None:
        # First identity to log in administers the instance.
        is_first = db.scalar(select(User).limit(1)) is None
        user = User(github_id=github_id, login=profile.get("login", ""), is_admin=is_first)
        db.add(user)

    user.login = profile.get("login", user.login)
    user.name = profile.get("name")
    user.email = profile.get("email")
    user.avatar_url = profile.get("avatar_url")
    user.last_login_at = utcnow()

    db.commit()
    return user
