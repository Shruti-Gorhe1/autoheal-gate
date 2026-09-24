"""Gate orchestration.

One entry point, ``run_gate``, does the whole job:

    gather evidence -> run the agents -> evaluate policy -> persist
    -> publish the verdict back to GitHub

Failures inside this function are caught and converted into a verdict. A gate
that throws an exception would leave the caller's pipeline in an undefined
state, which is worse than a clean BLOCK.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import AgentContext
from app.agents.pipeline import run_pipeline
from app.audit import record_audit
from app.config import settings
from app.context.builder import ContextBuilder
from app.db.models import GateRun, Repository, utcnow
from app.gate import policy as policy_engine
from app.gate.models import GateCheckRequest, GateCheckResponse
from app.observability.telemetry import current_trace_id
from app.providers.base import CICDProvider, PipelineEvent, SourceProvider
from app.providers.resolver import ProviderResolver
from app.security.guards import safe_error

logger = logging.getLogger("autoheal.gate")

provider_resolver = ProviderResolver()
context_builder = ContextBuilder()

def new_run_id() -> str:
    return f"gate_{uuid.uuid4().hex[:16]}"


def _request_event(request: GateCheckRequest) -> PipelineEvent:
    """Adapt the public gate request to the normalized provider event shape."""
    return PipelineEvent(
        provider=request.cicd_provider,
        source_provider=request.source_provider,
        cicd_provider=request.cicd_provider,
        repository=request.repository,
        commit_sha=request.commit_sha,
        branch=request.branch,
        run_id=(
            request.pipeline_run_id
            if request.pipeline_run_id is not None
            else (str(request.workflow_run_id) if request.workflow_run_id is not None else None)
        ),
        change_request_number=request.pull_request_number,
        status="success" if request.ci_passed else "failure",
        metadata=request.metadata,
    )


# ===========================================================================
# Policy resolution
# ===========================================================================


def resolve_repository(db: Session, full_name: str) -> Repository | None:
    return db.scalar(select(Repository).where(Repository.full_name == full_name))


async def resolve_policy(
    db: Session,
    repository: Repository | None,
    request: GateCheckRequest,
    source: SourceProvider | None,
) -> dict[str, Any]:
    """Layer the policy: defaults < repo record < .autoheal/policy.yml < request."""
    repo_policy = repository.policy if repository else {}

    in_repo_policy: dict[str, Any] = {}
    if source is not None:
        try:
            event = _request_event(request)
            raw = await source.get_file(event, ".autoheal/policy.yml", request.commit_sha)
            if raw:
                in_repo_policy = policy_engine.load_policy_yaml(raw)
        except Exception as exc:
            logger.debug("No in-repo policy for %s: %s", request.repository, exc)

    return policy_engine.merge_policy(
        repo_policy, in_repo_policy, request.policy_overrides
    )


# ===========================================================================
# Evidence collection
# ===========================================================================


async def collect_evidence(
    request: GateCheckRequest,
    source: SourceProvider | None,
    cicd: CICDProvider | None,
) -> tuple[str, str]:
    """Backward-compatible tuple view over the Phase 6 ContextBuilder."""
    context = await context_builder.build(
        request,
        event=_request_event(request),
        source=source,
        cicd=cicd,
    )
    return context.logs, context.diff


# ===========================================================================
# Main entry point
# ===========================================================================


async def run_gate(
    db: Session,
    request: GateCheckRequest,
    *,
    actor: str = "system",
    user_id: str | None = None,
    access_token: str | None = None,
    provider_tokens: dict[str, str | None] | None = None,
    source: str = "api",
) -> GateCheckResponse:
    started = time.perf_counter()
    run_id = new_run_id()

    repository = resolve_repository(db, request.repository)

    try:
        resolver_kwargs = {
            "source_provider": request.source_provider,
            "cicd_provider": request.cicd_provider,
            "access_token": access_token,
        }
        if provider_tokens is not None:
            resolver_kwargs["provider_tokens"] = provider_tokens

        providers = provider_resolver.resolve(**resolver_kwargs)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    source_provider = providers.source
    cicd_provider = providers.cicd

    run = GateRun(
        id=run_id,
        repository_id=repository.id if repository else None,
        repository_full_name=request.repository,
        commit_sha=request.commit_sha,
        branch=request.branch,
        pull_request_number=request.pull_request_number,
        workflow_run_id=request.workflow_run_id,
        status="running",
        verdict="PENDING",
        triggered_by_user_id=user_id,
        source=source,
    )
    db.add(run)
    db.commit()

    try:
        resolved_policy = await resolve_policy(db, repository, request, source_provider)
        run.policy_snapshot = resolved_policy

        evidence_context = await context_builder.build(
            request,
            event=_request_event(request),
            source=source_provider,
            cicd=cicd_provider,
        )
        logs, diff = evidence_context.logs, evidence_context.diff

        repo_path = request.repo_path or (repository.local_path if repository else None)
        test_command = (
            request.test_command
            or (repository.test_command if repository else None)
            or settings.default_test_command
        )
        setup_command = request.setup_command or (
            repository.setup_command if repository else None
        )

        ctx = AgentContext(
            run_id=run_id,
            repository=request.repository,
            commit_sha=request.commit_sha,
            branch=request.branch,
            pull_request_number=request.pull_request_number,
            workflow_run_id=request.workflow_run_id,
            ci_passed=request.ci_passed,
            logs=logs,
            diff=diff,
            repo_path=repo_path,
            test_command=test_command,
            setup_command=setup_command,
            github_token=(
                (provider_tokens or {}).get("github", access_token)
                if request.source_provider == "github"
                else None
            ),
            provider=source_provider,
            policy=resolved_policy,
            metadata=request.metadata,
        )

        ctx = run_pipeline(ctx)
        decision = policy_engine.evaluate(ctx.evidence(), resolved_policy)

    except Exception as exc:
        logger.exception("Gate run %s failed", run_id)
        verdict = "BLOCK" if settings.gate_fail_closed else "PASS"
        run.status = "error"
        run.verdict = verdict
        run.reason = f"The gate encountered an internal error: {safe_error(exc)}"
        run.completed_at = utcnow()
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
        record_audit(
            db,
            action="gate.error",
            actor=actor,
            target=run_id,
            outcome="error",
            detail={"error": safe_error(exc)},
        )
        return GateCheckResponse(
            verdict=verdict,
            allowed=verdict == "PASS",
            run_id=run_id,
            repository=request.repository,
            commit_sha=request.commit_sha,
            branch=request.branch,
            reason=run.reason,
            errors=[str(exc)],
            duration_ms=run.duration_ms or 0,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)

    run.status = "completed"
    run.verdict = decision.verdict
    run.reason = decision.reason
    run.risk_score = decision.risk_score
    run.hitl_required = decision.hitl_required
    run.remediation_attempted = not ctx.ci_passed
    run.fix_proposed = bool(ctx.fix.get("patch"))
    run.patch = ctx.fix.get("patch") or None
    run.patch_applied = bool(ctx.validation.get("patch_applied"))
    run.tests_passed = bool(ctx.validation.get("tests_passed"))
    run.root_cause = ctx.root_cause.get("summary")
    run.engine = ctx.engine
    run.trace_id = current_trace_id()
    run.policy_evaluation = decision.as_dict()
    run.artifacts = ctx.as_dict()
    run.completed_at = utcnow()
    run.duration_ms = duration_ms
    db.commit()

    record_audit(
        db,
        action="gate.evaluated",
        actor=actor,
        target=f"{request.repository}@{request.commit_sha[:8]}",
        outcome=decision.verdict.lower(),
        detail={
            "run_id": run_id,
            "verdict": decision.verdict,
            "risk_score": decision.risk_score,
            "engine": ctx.engine,
        },
    )

    response = _build_response(run, ctx, decision, duration_ms)

    publish = (
        request.publish_check_run
        if request.publish_check_run is not None
        else resolved_policy.get("publish_check_run", True)
    )
    if publish:
        await publish_verdict(
            source_provider,
            _request_event(request),
            response,
            resolved_policy,
        )

    return response


def _build_response(
    run: GateRun,
    ctx: AgentContext,
    decision: policy_engine.PolicyDecision,
    duration_ms: int,
) -> GateCheckResponse:
    return GateCheckResponse(
        verdict=decision.verdict,
        allowed=decision.verdict == "PASS",
        run_id=run.id,
        repository=run.repository_full_name,
        commit_sha=run.commit_sha,
        branch=run.branch,
        reason=decision.reason,
        risk_score=decision.risk_score,
        hitl_required=decision.hitl_required,
        failure_category=ctx.triage.get("category"),
        root_cause=ctx.root_cause.get("summary"),
        confidence=float(ctx.root_cause.get("confidence") or 0.0),
        remediation_attempted=run.remediation_attempted,
        fix_proposed=run.fix_proposed,
        patch=run.patch,
        fix_notes=None if run.fix_proposed else ctx.fix.get("proposal") or None,
        patch_applied=run.patch_applied,
        tests_passed=run.tests_passed,
        rules=[r.as_dict() for r in decision.rules],
        timeline=ctx.timeline,
        engine=ctx.engine,
        policy_name=decision.policy.get("name", "default"),
        policy_mode=decision.policy.get("mode", "enforce"),
        duration_ms=duration_ms,
        trace_id=run.trace_id,
        dashboard_url=f"{settings.frontend_base_url}/runs/{run.id}",
        errors=ctx.errors,
    )


# ===========================================================================
# Reporting back to GitHub
# ===========================================================================


def render_markdown(response: GateCheckResponse) -> str:
    """Human-readable verdict for a check run or PR comment."""
    icon = {"PASS": "✅", "BLOCK": "🛑", "HOLD": "⏸️"}.get(response.verdict, "❔")
    lines = [
        f"## {icon} AutoHeal Gate: {response.verdict}",
        "",
        response.reason,
        "",
        f"- **Risk score:** {response.risk_score}/100",
        f"- **Failure class:** {response.failure_category or 'n/a'}",
        f"- **Analysis engine:** {response.engine}",
        f"- **Policy:** {response.policy_name} ({response.policy_mode})",
    ]

    if response.root_cause:
        lines += ["", "### Root cause", response.root_cause]

    if response.fix_proposed:
        state = (
            "validated against the test suite"
            if response.tests_passed
            else "applied but not proven"
            if response.patch_applied
            else "proposed but not executed"
        )
        lines += ["", f"### Candidate fix ({state})", "", "```diff", (response.patch or "")[:6000], "```"]
    elif response.fix_notes:
        lines += ["", "### Why no fix was proposed", response.fix_notes]

    failed_rules = [r for r in response.rules if not r.passed and r.verdict]
    if failed_rules:
        lines += ["", "### Rules that changed the verdict", ""]
        lines += [f"- `{r.rule}` → **{r.verdict}** — {r.message}" for r in failed_rules]

    if response.hitl_required:
        lines += [
            "",
            "A reviewer must approve or reject this run before the release can continue.",
            f"Open it in the dashboard: {response.dashboard_url}",
        ]

    return "\n".join(lines)


async def publish_verdict(
    source: SourceProvider,
    event: PipelineEvent,
    response: GateCheckResponse,
    resolved_policy: dict[str, Any],
) -> None:
    """Write the verdict through the repository provider."""
    body = render_markdown(response)

    try:
        if getattr(source, "capabilities", None) is not None and not source.capabilities.publish_status:
            logger.info("Provider %s does not advertise publish_status; skipping reporting.", source.name)
        else:
            await source.publish_status(
            event,
            verdict=response.verdict,
            summary=response.reason,
            detail=body,
                details_url=response.dashboard_url,
            )
    except Exception as exc:
        # Providers own their API-specific fallback behavior; the gate should
        # not fail an otherwise completed evaluation just because reporting
        # is unavailable.
        logger.info("Could not publish the gate status: %s", exc)

    if event.change_request_number and resolved_policy.get("comment_on_pull_request", True):
        try:
            if getattr(source, "capabilities", None) is not None and not source.capabilities.publish_comment:
                logger.info("Provider %s does not advertise publish_comment; skipping comment.", source.name)
            else:
                await source.publish_comment(event, body)
        except Exception as exc:
            logger.info("Could not comment on the change request: %s", exc)


# ===========================================================================
# Approved fixes become pull requests
# ===========================================================================


async def open_fix_pull_request(
    source: SourceProvider, run: GateRun, actor: str
) -> dict[str, Any]:
    """Turn a validated, human-approved patch into a change request.

    Repository-specific branch/file/PR operations are delegated to the source
    provider. The gate no longer knows which source-control API is involved.
    """
    if not run.patch:
        return {"created": False, "reason": "This run has no candidate fix."}
    if getattr(source, "capabilities", None) is not None and not source.capabilities.create_fix_pull_request:
        return {"created": False, "reason": f"Provider {source.name} does not support fix pull requests."}

    event = PipelineEvent(
        provider=source.name,
        source_provider=source.name,
        cicd_provider=source.name,
        repository=run.repository_full_name,
        commit_sha=run.commit_sha,
        branch=run.branch,
        run_id=run.workflow_run_id,
        change_request_number=run.pull_request_number,
    )
    artifacts = run.artifacts or {}
    validation = artifacts.get("validation", {})
    title = f"AutoHeal: fix {run.root_cause[:60] if run.root_cause else 'CI failure'}"
    body = (
        f"Automated repair from AutoHeal Gate run `{run.id}`.\n\n"
        f"**Root cause**\n{run.root_cause or 'n/a'}\n\n"
        f"**Validation**\n{validation.get('reason', 'n/a')}\n\n"
        f"**Risk score:** {run.risk_score}/100\n"
        f"**Approved by:** @{actor}\n\n"
        "Review this like any other change before merging."
    )
    return await source.create_fix_pull_request(
        event, patch=run.patch, title=title, body=body
    )
