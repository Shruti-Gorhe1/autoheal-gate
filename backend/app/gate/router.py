"""Gate API.

``POST /api/gate/check`` is the endpoint CI calls. Everything else exists to
inspect, govern, or override what that endpoint decided.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth.deps import Principal, optional_principal, require_principal
from app.config import settings
from app.db.base import get_db
from app.db.models import Approval, GateRun, Repository, utcnow
from app.gate import policy as policy_engine
from app.gate import service
from app.gate.models import (
    DecisionRequest,
    GateCheckRequest,
    GateCheckResponse,
    GateRunSummary,
    RepositoryRegister,
    RepositoryUpdate,
)
from app.providers.base import PipelineEvent
from app.providers.resolver import resolve_providers

logger = logging.getLogger("autoheal.gate.api")

router = APIRouter(prefix="/api/gate", tags=["CI/CD Gate"])


# ===========================================================================
# The gate itself
# ===========================================================================


@router.post(
    "/check",
    response_model=GateCheckResponse,
    summary="Evaluate a commit and return a release verdict",
)
async def check(
    request: GateCheckRequest,
    response: Response,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    """Decide whether a commit may continue through the pipeline.

    Verdicts:
      * ``PASS``  — continue.
      * ``HOLD``  — a human must approve first. CI should stop and wait.
      * ``BLOCK`` — do not release.

    A non-PASS verdict also sets HTTP 409 so a shell caller can branch on the
    status code alone, without parsing the body.
    """
    result = await service.run_gate(
        db,
        request,
        actor=principal.login,
        user_id=principal.user.id,
        access_token=principal.access_token,
        source="api",
    )
    if not result.allowed:
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.get("/runs", response_model=list[GateRunSummary], summary="List gate runs")
def list_runs(
    repository: str | None = None,
    verdict: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    query = select(GateRun).order_by(desc(GateRun.created_at))
    if repository:
        query = query.where(GateRun.repository_full_name == repository)
    if verdict:
        query = query.where(GateRun.verdict == verdict.upper())

    runs = db.scalars(query.limit(limit).offset(offset)).all()
    return [
        GateRunSummary(
            run_id=run.id,
            repository=run.repository_full_name,
            commit_sha=run.commit_sha,
            branch=run.branch,
            verdict=run.verdict,
            status=run.status,
            reason=run.reason,
            risk_score=run.risk_score,
            hitl_required=run.hitl_required,
            engine=run.engine,
            created_at=run.created_at,
            duration_ms=run.duration_ms,
        )
        for run in runs
    ]


@router.get("/runs/{run_id}", summary="Full detail of one gate run")
def get_run(
    run_id: str,
    _: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    run = db.get(GateRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Gate run not found.")

    return {
        "run_id": run.id,
        "repository": run.repository_full_name,
        "commit_sha": run.commit_sha,
        "branch": run.branch,
        "pull_request_number": run.pull_request_number,
        "workflow_run_id": run.workflow_run_id,
        "status": run.status,
        "verdict": run.verdict,
        "reason": run.reason,
        "risk_score": run.risk_score,
        "hitl_required": run.hitl_required,
        "root_cause": run.root_cause,
        "patch": run.patch,
        "fix_proposed": run.fix_proposed,
        "fix_notes": None if run.fix_proposed else (run.artifacts or {}).get("fix", {}).get("proposal"),
        "patch_applied": run.patch_applied,
        "tests_passed": run.tests_passed,
        "engine": run.engine,
        "trace_id": run.trace_id,
        "policy": run.policy_snapshot,
        "policy_evaluation": run.policy_evaluation,
        "artifacts": run.artifacts,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "duration_ms": run.duration_ms,
        "approvals": [
            {
                "actor": a.actor_login,
                "decision": a.decision,
                "comment": a.comment,
                "at": a.created_at,
            }
            for a in run.approvals
        ],
    }


# ===========================================================================
# Human in the loop
# ===========================================================================


@router.post("/runs/{run_id}/decision", summary="Approve or reject a held run")
async def decide(
    run_id: str,
    payload: DecisionRequest,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    """Record a reviewer's decision on a run the policy put on hold.

    Approval is only accepted where the policy would otherwise have passed.
    A run that was BLOCKed on a hard rule, such as a secret in the diff or a
    fix that edits tests, cannot be approved through this endpoint.
    """
    run = db.get(GateRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Gate run not found.")

    if run.verdict == "BLOCK":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This run was blocked by a hard policy rule and cannot be "
                f"approved here. Reason: {run.reason}"
            ),
        )
    if run.verdict == "PASS":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run already passed. No decision is needed.",
        )

    db.add(
        Approval(
            run_id=run.id,
            user_id=principal.user.id,
            actor_login=principal.login,
            decision=payload.decision,
            comment=payload.comment,
        )
    )

    run.verdict = "PASS" if payload.decision == "approve" else "BLOCK"
    run.hitl_required = False
    run.reason = (
        f"Approved by @{principal.login} after review."
        if payload.decision == "approve"
        else f"Rejected by @{principal.login}."
    )
    if payload.comment:
        run.reason += f" Note: {payload.comment}"
    db.commit()

    record_audit(
        db,
        action=f"gate.{payload.decision}",
        actor=principal.login,
        target=run.id,
        detail={"repository": run.repository_full_name, "commit": run.commit_sha},
    )

    result: dict = {"run_id": run.id, "verdict": run.verdict, "reason": run.reason}

    if principal.access_token:
        providers = resolve_providers(
            source_provider="github",
            cicd_provider="github",
            access_token=principal.access_token,
        )
        event = PipelineEvent(
            provider="github",
            source_provider="github",
            cicd_provider="github",
            repository=run.repository_full_name,
            commit_sha=run.commit_sha,
            branch=run.branch,
            run_id=run.workflow_run_id,
            change_request_number=run.pull_request_number,
        )
        try:
            await providers.source.publish_status(
                event,
                verdict=run.verdict,
                summary=run.reason,
            )
        except Exception as exc:
            logger.info("Could not update the provider status: %s", exc)

        if payload.decision == "approve" and payload.open_pull_request:
            try:
                result["pull_request"] = await service.open_fix_pull_request(
                    providers.source, run, principal.login
                )
            except Exception as exc:
                result["pull_request"] = {"created": False, "reason": str(exc)}

    return result


# ===========================================================================
# Repositories and policy
# ===========================================================================


@router.get("/repositories", summary="Repositories under gate control")
def list_repositories(
    _: Principal = Depends(require_principal), db: Session = Depends(get_db)
):
    repos = db.scalars(select(Repository).order_by(Repository.full_name)).all()
    return {
        "repositories": [
            {
                "id": r.id,
                "full_name": r.full_name,
                "enabled": r.enabled,
                "default_branch": r.default_branch,
                "local_path": r.local_path,
                "test_command": r.test_command,
                "setup_command": r.setup_command,
                "policy": policy_engine.merge_policy(r.policy),
                "run_count": db.scalar(
                    select(func.count(GateRun.id)).where(GateRun.repository_id == r.id)
                ),
            }
            for r in repos
        ]
    }


@router.post("/repositories", status_code=201, summary="Register a repository")
def register_repository(
    payload: RepositoryRegister,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    existing = service.resolve_repository(db, payload.full_name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="That repository is already registered.")

    repo = Repository(
        full_name=payload.full_name,
        owner_user_id=principal.user.id,
        default_branch=payload.default_branch,
        local_path=payload.local_path,
        test_command=payload.test_command,
        setup_command=payload.setup_command,
        policy=payload.policy,
    )
    db.add(repo)
    db.commit()

    record_audit(
        db, action="repository.registered", actor=principal.login, target=payload.full_name
    )
    return {"id": repo.id, "full_name": repo.full_name, "enabled": repo.enabled}


@router.patch("/repositories/{repo_id}", summary="Update a repository or its policy")
def update_repository(
    repo_id: str,
    payload: RepositoryUpdate,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found.")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(repo, field, value)
    repo.updated_at = utcnow()
    db.commit()

    record_audit(
        db, action="repository.updated", actor=principal.login, target=repo.full_name
    )
    return {"id": repo.id, "full_name": repo.full_name, "policy": repo.policy}


@router.get("/policy/default", summary="The built-in policy and its meaning")
def default_policy():
    return {
        "policy": policy_engine.DEFAULT_POLICY,
        "verdicts": {
            "PASS": "The pipeline may continue.",
            "HOLD": "A human must approve before the pipeline continues.",
            "BLOCK": "The release is refused.",
        },
        "notes": (
            "Place a .autoheal/policy.yml file in a repository to override "
            "these values per branch or per team."
        ),
    }


@router.post("/policy/simulate", summary="Try a policy against hypothetical evidence")
def simulate_policy(
    evidence: dict,
    overrides: dict | None = None,
    _: Principal = Depends(require_principal),
):
    """Check what a policy would decide without running a real pipeline.

    Useful when tuning thresholds: send the evidence shape and see the verdict.
    """
    merged = policy_engine.merge_policy(overrides or {})
    return policy_engine.evaluate(evidence, merged).as_dict()


# ===========================================================================
# Health and statistics
# ===========================================================================


@router.get("/health", summary="Gate liveness")
def gate_health(db: Session = Depends(get_db)):
    total = db.scalar(select(func.count(GateRun.id))) or 0
    return {
        "status": "ok",
        "service": "autoheal-gate",
        "version": settings.app_version,
        "runs_recorded": total,
        "fail_closed": settings.gate_fail_closed,
    }


@router.get("/stats", summary="Aggregate gate statistics")
def stats(
    repository: str | None = None,
    _: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    base = select(GateRun)
    if repository:
        base = base.where(GateRun.repository_full_name == repository)
    runs = db.scalars(base).all()

    if not runs:
        return {"total": 0, "by_verdict": {}, "auto_healed": 0}

    by_verdict: dict[str, int] = {}
    for run in runs:
        by_verdict[run.verdict] = by_verdict.get(run.verdict, 0) + 1

    healed = [r for r in runs if r.tests_passed and r.fix_proposed]
    durations = [r.duration_ms for r in runs if r.duration_ms]

    return {
        "total": len(runs),
        "by_verdict": by_verdict,
        "auto_healed": len(healed),
        "auto_heal_rate": round(len(healed) / len(runs), 3),
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "average_risk_score": round(sum(r.risk_score for r in runs) / len(runs), 1),
        "awaiting_review": sum(1 for r in runs if r.hitl_required),
    }
