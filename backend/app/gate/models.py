"""Public API contract for the gate.

These schemas are the integration surface other projects code against, so they
change slowly and describe themselves in the generated OpenAPI document.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "BLOCK", "HOLD", "PENDING"]


class GateCheckRequest(BaseModel):
    """What a CI job sends to ask whether a commit may proceed."""

    repository: str = Field(
        description="Repository in owner/name form.", examples=["acme/payments-api"]
    )
    commit_sha: str = Field(description="Commit under evaluation.", min_length=7)
    branch: str | None = Field(default=None, description="Branch being built.")
    pull_request_number: int | None = None
    workflow_run_id: int | None = Field(
        default=None,
        description="Legacy GitHub Actions run id. Kept for backward compatibility.",
    )
    pipeline_run_id: str | None = Field(
        default=None,
        description=(
            "Provider-neutral CI/CD run/build id. Use this for Azure, GCP, AWS, "
            "and GitHub runs when the identifier is not an integer."
        ),
    )

    ci_passed: bool = Field(
        default=False, description="Did the upstream build and test stage succeed?"
    )
    failure_logs: str | None = Field(
        default=None,
        description="Build output. Supply this when the gate cannot fetch logs itself.",
    )
    diff: str | None = Field(default=None, description="Unified diff of the change.")

    repo_path: str | None = Field(
        default=None,
        description=(
            "Path to a checkout the gate can use to validate a fix. Inside a "
            "GitHub Actions job this is usually the workspace directory."
        ),
    )
    test_command: str | None = Field(
        default=None, description="Command used to prove a fix. Defaults to pytest."
    )
    setup_command: str | None = Field(
        default=None,
        description=(
            "Command run once in the patched workspace before test_command "
            "-- typically a dependency install. Matters most when there is "
            "no local checkout, so validation starts from a bare source "
            "tree downloaded from GitHub with nothing installed."
        ),
    )

    policy_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-request policy overrides merged over the repository policy.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Keep source and CI/CD provider identity independent. Direct API callers
    # retain GitHub as the backward-compatible default for this phase.
    source_provider: str = Field(default="github", description="Provider where the source repository lives.")
    cicd_provider: str = Field(default="github", description="Provider where the CI/CD run executed.")

    publish_check_run: bool | None = Field(
        default=None, description="Override the policy's check-run publishing setting."
    )
    wait: bool = Field(
        default=True,
        description="Run synchronously and return the verdict. False queues the run.",
    )


class RuleResult(BaseModel):
    rule: str
    passed: bool
    verdict: str | None = None
    message: str
    weight: int = 0


class TimelineEntry(BaseModel):
    agent: str
    status: str
    summary: str
    duration_ms: float = 0.0


class GateCheckResponse(BaseModel):
    """The verdict, plus everything needed to defend it in a review."""

    verdict: Verdict
    allowed: bool = Field(description="True when CI may continue.")
    run_id: str
    repository: str
    commit_sha: str
    branch: str | None = None

    reason: str
    risk_score: int = 0
    hitl_required: bool = False

    failure_category: str | None = None
    root_cause: str | None = None
    confidence: float = 0.0

    remediation_attempted: bool = False
    fix_proposed: bool = False
    patch: str | None = None
    fix_notes: str | None = Field(
        default=None,
        description=(
            "Why no patch was produced, when fix_proposed is false. Explains "
            "the gap rather than leaving it silent -- e.g. the failure "
            "category has no mechanical repair, or the evidence didn't "
            "reduce to a safe, specific change."
        ),
    )
    patch_applied: bool = False
    tests_passed: bool = False

    rules: list[RuleResult] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)

    engine: str = "deterministic"
    policy_name: str = "default"
    policy_mode: str = "enforce"
    duration_ms: int = 0
    trace_id: str | None = None
    dashboard_url: str | None = None
    errors: list[str] = Field(default_factory=list)


class GateRunSummary(BaseModel):
    run_id: str
    repository: str
    commit_sha: str
    branch: str | None = None
    verdict: Verdict
    status: str
    reason: str
    risk_score: int
    hitl_required: bool
    engine: str | None = None
    created_at: datetime
    duration_ms: int | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)
    open_pull_request: bool = Field(
        default=False,
        description="On approval, open a PR containing the validated fix.",
    )


class RepositoryRegister(BaseModel):
    full_name: str = Field(examples=["acme/payments-api"])
    default_branch: str = "main"
    local_path: str | None = Field(
        default=None, description="Optional checkout used to validate fixes."
    )
    test_command: str | None = None
    setup_command: str | None = None
    policy: dict[str, Any] = Field(default_factory=dict)


class RepositoryUpdate(BaseModel):
    enabled: bool | None = None
    default_branch: str | None = None
    local_path: str | None = None
    test_command: str | None = None
    setup_command: str | None = None
    policy: dict[str, Any] | None = None
