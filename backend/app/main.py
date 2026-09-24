"""AutoHeal Gate — agentic CI/CD release gate.

Run with:  uvicorn app.main:app --reload --port 8000
API docs:  http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import recent_events
from app.auth.deps import Principal, require_admin, require_principal
from app.auth.router import router as auth_router
from app.config import settings
from app.db.base import get_db, init_db, session_scope
from app.db.models import GateRun, Repository
from app.gate.router import router as gate_router
from app.integrations.webhooks import router as webhook_router, process_workflow_run
from app.integrations.azure_webhooks import router as azure_webhook_router, process_build_completed
from app.integrations.google_cloud_build_webhooks import router as gcp_webhook_router, process_build_notification
from app.integrations.aws_webhooks import router as aws_webhook_router, process_aws_event
from app.observability.telemetry import setup_telemetry, status as telemetry_status
from app.services.job_queue import job_queue

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("autoheal")


def seed_demo_repository() -> None:
    """Register the bundled sample repository so the demo works immediately."""
    from pathlib import Path

    sample = Path(settings.sample_repo_path)
    if not sample.is_dir():
        return

    with session_scope() as db:
        existing = db.scalar(
            select(Repository).where(Repository.full_name == "autoheal/sample-repo")
        )
        if existing is None:
            db.add(
                Repository(
                    full_name="autoheal/sample-repo",
                    default_branch="main",
                    local_path=str(sample),
                    test_command=settings.default_test_command,
                    policy={"name": "demo", "require_human_approval": True},
                )
            )
            logger.info("Registered the bundled demo repository.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting %s %s (%s)", settings.app_name, settings.app_version, settings.app_env)
    init_db()
    setup_telemetry()
    job_queue.max_size = settings.queue_max_size
    job_queue.worker_count = settings.queue_worker_count
    job_queue.max_attempts = settings.queue_max_attempts
    job_queue.retry_delay_seconds = settings.queue_retry_delay_seconds
    job_queue.poll_interval_seconds = settings.queue_poll_interval_seconds
    job_queue.register_handler("github", process_workflow_run)
    job_queue.register_handler("azure", process_build_completed)
    job_queue.register_handler("gcp", process_build_notification)
    job_queue.register_handler("aws", process_aws_event)
    await job_queue.start()
    if settings.seed_demo_repository:
        seed_demo_repository()
    if not settings.github_configured:
        logger.warning(
            "GitHub OAuth is not configured. Set GITHUB_CLIENT_ID and "
            "GITHUB_CLIENT_SECRET to enable sign-in."
        )
    if settings.secret_key == "dev-insecure-change-me" and settings.app_env != "local":
        logger.error("SECRET_KEY is still the development default. Change it now.")
    yield
    await job_queue.stop()
    logger.info("Shutting down.")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "An agentic release gate for CI/CD. It diagnoses a failing pipeline, "
        "proposes and validates a fix in an isolated workspace, and then lets a "
        "deterministic policy engine decide whether the release may proceed."
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "CI/CD Gate", "description": "Evaluate commits and govern releases."},
        {"name": "Authentication", "description": "GitHub sign-in and CI API keys."},
        {"name": "Integrations", "description": "Webhooks from GitHub and Azure DevOps."},
        {"name": "System", "description": "Health, configuration and audit."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,  # required so the session cookie is sent
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------- rate limit

_requests: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """A small in-process limit. Put a real gateway in front for scale."""
    if request.url.path.startswith(("/docs", "/openapi.json", "/redoc")):
        return await call_next(request)

    client = request.client.host if request.client else "unknown"
    now = time.time()
    window = _requests[client]
    while window and now - window[0] > 60:
        window.popleft()

    if len(window) >= settings.rate_limit_per_minute:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many requests. Try again in a minute."},
        )

    window.append(now)
    return await call_next(request)


# ------------------------------------------------------------------- routes

app.include_router(auth_router)
app.include_router(gate_router)
app.include_router(webhook_router)
app.include_router(azure_webhook_router)
app.include_router(gcp_webhook_router)
app.include_router(aws_webhook_router)


@app.get("/", tags=["System"], summary="Service banner")
def root():
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "gate": "/api/gate/check",
        "sign_in": "/api/auth/github/login",
    }


@app.get("/api/health", tags=["System"], summary="Health and configuration")
def health(db: Session = Depends(get_db)):
    from app.agents.llm_engine import LLMEngine
    from app.rag.rag_service import rag

    try:
        db.execute(select(func.count(GateRun.id)))
        database_ok = True
    except Exception:
        database_ok = False

    try:
        llm = LLMEngine().describe()
    except Exception as exc:
        llm = {"error": str(exc)}

    return {
        "status": "ok" if database_ok else "degraded",
        "version": settings.app_version,
        "environment": settings.app_env,
        "database": {"ok": database_ok, "engine": settings.database_url.split(":")[0]},
        "github_oauth_configured": settings.github_configured,
        "webhook_secret_set": bool(settings.github_webhook_secret),
        "agent_engine": settings.agent_engine,
        "llm": llm,
        "rag": rag.stats(),
        "tracing": telemetry_status(),
        "fail_closed": settings.gate_fail_closed,
        "queue": {
            "started": job_queue.started,
            "pending": job_queue.pending,
            "workers": job_queue.worker_count_running,
        },
    }


@app.get("/api/system/providers", tags=["System"], summary="Provider matrix and capabilities")
def provider_matrix(_: Principal = Depends(require_principal)):
    """Expose provider roles/capabilities without exposing credentials."""
    from app.providers.resolver import ProviderResolver

    resolver = ProviderResolver()

    def describe(registry):
        result = {}
        for name, implementation in registry.items():
            caps = getattr(implementation, "capabilities", None)
            result[name] = {
                "capabilities": {
                    field: bool(getattr(caps, field))
                    for field in (
                        "get_commit_diff", "get_file", "download_snapshot",
                        "publish_status", "publish_comment",
                        "create_fix_pull_request", "get_failure_logs",
                    )
                }
            }
        return result

    return {
        "source": describe(resolver.source_registry),
        "cicd": describe(resolver.cicd_registry),
    }


@app.get("/api/system/jobs", tags=["System"], summary="List durable AutoHeal jobs")
def list_jobs(limit: int = 50, _: Principal = Depends(require_principal)):
    return {"jobs": job_queue.list_jobs(limit=min(max(limit, 1), 200))}


@app.get("/api/system/jobs/{job_id}", tags=["System"], summary="Get one durable AutoHeal job")
def get_job(job_id: str, _: Principal = Depends(require_principal)):
    job = job_queue.get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"detail": "Job not found."})
    return job


@app.get("/api/system/audit", tags=["System"], summary="Recent audit events")
def audit(
    limit: int = 100,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {
        "events": [
            {
                "at": e.created_at,
                "actor": e.actor,
                "action": e.action,
                "target": e.target,
                "outcome": e.outcome,
                "detail": e.detail,
            }
            for e in recent_events(db, limit=min(limit, 500))
        ]
    }


@app.post("/api/system/rag/refresh", tags=["System"], summary="Reindex the knowledge base")
def refresh_rag(_: Principal = Depends(require_principal)):
    from app.rag.rag_service import rag

    return rag.refresh()


@app.post("/api/system/evaluation/run", tags=["System"], summary="Run the RAG evaluation suite")
def run_evaluation(_: Principal = Depends(require_principal)):
    from evaluation.evaluate import run

    return run()
