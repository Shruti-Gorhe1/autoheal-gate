"""Central configuration.

Every value is read from the environment (or a .env file) so the same image
runs locally, in CI, and in a container without code changes.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("AUTOHEAL_ENV_FILE", str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- app
    app_env: str = "local"
    app_name: str = "AutoHeal Gate"
    app_version: str = "1.0.0"
    log_level: str = "INFO"

    # Public URL of this backend. Used to build the OAuth callback URL.
    backend_base_url: str = "http://localhost:8000"
    # Where the browser is sent after a successful login.
    frontend_base_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ------------------------------------------------------------- secrets
    # Used to encrypt GitHub access tokens at rest and to sign session ids.
    secret_key: str = "dev-insecure-change-me"

    # ------------------------------------------------------------ database
    # SQLite by default (zero cost, zero setup). Any SQLAlchemy URL works,
    # so a team can point this at Postgres without touching code.
    database_url: str = f"sqlite:///{BACKEND_DIR / 'autoheal.db'}"

    # -------------------------------------------------------- github oauth
    github_client_id: str = ""
    github_client_secret: str = ""
    # Must match the Authorization callback URL registered on GitHub.
    github_callback_url: str = "http://localhost:8000/api/auth/github/callback"
    github_oauth_scope: str = "repo read:user"
    github_api_base: str = "https://api.github.com"
    github_oauth_base: str = "https://github.com/login/oauth"

    # Shared secret for GitHub webhook signature verification.
    github_webhook_secret: str = ""

    # ------------------------------------------------------ azure devops
    # Azure DevOps provider authentication currently uses a Personal Access
    # Token. It is read server-side only; it is never sent to the browser or
    # included in LLM context.
    azure_devops_pat: str = ""
    azure_devops_base_url: str = "https://dev.azure.com"
    # Azure DevOps service hooks authenticate with HTTP Basic Auth
    # (configured on the subscription itself), not an HMAC signature like
    # GitHub's. Leave blank to accept unauthenticated requests -- fine for
    # local development, not for a public deployment.
    azure_webhook_username: str = ""
    azure_webhook_password: str = ""

    # ------------------------------------------------------- google cloud build
    # Cloud Build access is server-side only. Prefer Application Default
    # Credentials in deployed environments; an explicit access token is
    # useful for local development/tests.
    google_cloud_project: str = ""
    google_cloud_location: str = "global"
    google_application_credentials: str = ""
    google_cloud_access_token: str = ""
    gcp_webhook_secret: str = ""

    # --------------------------------------------------------------- AWS CI/CD
    # AWS credentials are server-side only. Leave access/secret/session values
    # blank to let boto3 use the normal AWS credential chain (environment,
    # shared profile, or IAM role).
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_webhook_secret: str = ""

    # ------------------------------------------------------------- session
    session_cookie_name: str = "autoheal_session"
    session_ttl_hours: int = 12
    # Set true only when the backend is served over HTTPS.
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"

    # --------------------------------------------------------------- agent
    # "auto" -> use the LLM engine when reachable, otherwise the
    # deterministic engine. "deterministic" and "adk" force one engine.
    agent_engine: str = "auto"
    llm_provider: str = "ollama"
    llm_model: str = "llama3.2"
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: float = 120.0

    # ----------------------------------------------------------- execution
    # Root under which repositories may be checked out and patched.
    workspace_root: str = str(BACKEND_DIR / "workspaces")
    default_test_command: str = "python -m pytest -q"
    test_timeout_seconds: int = 300
    # Local repo used by the built-in demo scenario.
    sample_repo_path: str = str(PROJECT_ROOT / "sample-repo")

    # ----------------------------------------------------------------- rag
    rag_knowledge_dir: str = str(PROJECT_ROOT / "knowledge_base")
    rag_top_k: int = 4
    rag_use_semantic: bool = False
    rag_embedding_model: str = "all-MiniLM-L6-v2"

    # ------------------------------------------------------- observability
    phoenix_enabled: bool = False
    phoenix_project_name: str = "autoheal-gate"
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"
    otel_console_exporter: bool = False

    # --------------------------------------------------------- rate limits
    rate_limit_per_minute: int = 120

    # --------------------------------------------------------------- queue
    # Local/free Phase 8 queue settings. A durable broker can replace this
    # implementation later without changing webhook or gate contracts.
    queue_max_size: int = 1000
    queue_worker_count: int = 2
    queue_max_attempts: int = 3
    queue_retry_delay_seconds: float = 0.5
    queue_poll_interval_seconds: float = 0.25
    queue_claim_timeout_seconds: float = 300.0

    # --------------------------------------------------------------- gate
    # When the gate itself errors, block the pipeline rather than let it pass.
    gate_fail_closed: bool = True

    seed_demo_repository: bool = Field(
        default=True,
        description="Register the bundled sample-repo on first boot.",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def github_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
