"""Encryption for GitHub tokens at rest, plus id and key helpers.

The Fernet key is derived from SECRET_KEY so operators manage one secret.
Rotating SECRET_KEY invalidates stored tokens, which forces a re-login. That
is the intended behaviour: a rotated secret should not leave old tokens usable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_API_KEY_PREFIX = "ahg_"


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - depends on secret rotation
        raise ValueError(
            "Stored credential could not be decrypted. SECRET_KEY has most "
            "likely changed; affected users must log in again."
        ) from exc


def new_session_id() -> str:
    """Opaque, unguessable session identifier for the httpOnly cookie."""
    return secrets.token_urlsafe(48)


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(full_key, prefix, sha256_hash)``.

    The full key is shown to the user exactly once; only the hash is stored.
    """
    raw = secrets.token_urlsafe(32)
    full = f"{_API_KEY_PREFIX}{raw}"
    return full, full[:12], hash_api_key(full)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_webhook_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` header.

    A missing secret is allowed only for local development; production-like
    deployments must configure webhook authentication.
    """
    if not secret:
        return settings.app_env.lower() in {"local", "test", "development"}
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
