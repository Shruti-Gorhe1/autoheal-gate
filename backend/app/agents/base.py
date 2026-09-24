"""Shared contracts for the agent pipeline.

Six agents run in order. Each reads the accumulated context and writes one
typed artifact back into it:

    1. Triage      -> classify the failure
    2. Retrieval   -> pull matching runbooks from the knowledge base
    3. RootCause   -> explain why it failed, with evidence
    4. Fix         -> propose the smallest unified diff
    5. Validation  -> apply the diff to a throwaway copy and run the tests
    6. Gate        -> hand the evidence to the policy engine

Only Validation and Gate can change the verdict. Everything before them is
evidence gathering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.providers.base import SourceProvider

# --------------------------------------------------------------- taxonomy

FAILURE_CATEGORIES = {
    "assertion_failure": "A test assertion did not hold.",
    "import_error": "A module or symbol could not be imported.",
    "syntax_error": "The source failed to parse.",
    "type_error": "An operation was applied to the wrong type.",
    "dependency_version": "A package version is missing or incompatible.",
    "lint_error": "A linter or formatter rejected the code.",
    "flaky_timeout": "A step exceeded its time budget or a network call failed.",
    "build_error": "Compilation or packaging failed.",
    "security_finding": "A scanner reported a vulnerability.",
    "secret_detected": "A credential appears in the change.",
    "license_violation": "A dependency licence is not permitted.",
    "infrastructure": "The runner or environment failed, not the code.",
    "unknown": "The failure could not be classified.",
}


@dataclass
class AgentContext:
    """Everything the agents may read, and where they write their findings."""

    run_id: str
    repository: str
    commit_sha: str
    branch: str | None = None
    pull_request_number: int | None = None
    workflow_run_id: int | None = None

    ci_passed: bool = False
    logs: str = ""
    diff: str = ""
    repo_path: str | None = None
    # Present only when a caller's GitHub token is available for this run.
    # Lets the Fix agent read a file straight from GitHub when there is no
    # local checkout to read it from. Deliberately excluded from as_dict()
    # and from every stored/serialized run record -- it must never leave
    # this in-memory context.
    github_token: str | None = None
    # A SourceProvider instance (e.g. GitHubProvider), when the caller
    # resolved one. Preferred over github_token by the fix/validation
    # agents when present, so their remote-file and snapshot-download calls
    # go through the provider interface rather than a hard-coded GitHub
    # import -- github_token stays as the fallback so every existing caller
    # and test that only sets it keeps working unchanged. Typed as
    # SourceProvider, not CICDProvider: reading a file and downloading a
    # snapshot are both "where the code lives" concerns. Excluded from
    # as_dict() and every stored/serialized run record, same as
    # github_token.
    provider: "SourceProvider | None" = None
    test_command: str = "python -m pytest -q"
    setup_command: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Filled in by the agents
    triage: dict[str, Any] = field(default_factory=dict)
    knowledge: dict[str, Any] = field(default_factory=dict)
    root_cause: dict[str, Any] = field(default_factory=dict)
    fix: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)

    engine: str = "deterministic"
    timeline: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def note(self, agent: str, status: str, summary: str, duration_ms: float = 0.0) -> None:
        self.timeline.append(
            {
                "agent": agent,
                "status": status,
                "summary": summary,
                "duration_ms": round(duration_ms, 2),
            }
        )

    def evidence(self) -> dict[str, Any]:
        """The flat view the policy engine consumes."""
        return {
            "ci_passed": self.ci_passed,
            "category": self.triage.get("category", "unknown"),
            "confidence": self.root_cause.get("confidence", 0.0),
            "patch": self.fix.get("patch", ""),
            "patch_applied": self.validation.get("patch_applied", False),
            "tests_passed": self.validation.get("tests_passed", False),
            "validation_ran": self.validation.get("attempted", False),
            "branch": self.branch,
            "error": self.errors[0] if self.errors else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repository": self.repository,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "engine": self.engine,
            "ci_passed": self.ci_passed,
            "triage": self.triage,
            "knowledge": self.knowledge,
            "root_cause": self.root_cause,
            "fix": self.fix,
            "validation": self.validation,
            "timeline": self.timeline,
            "errors": self.errors,
        }


class Agent:
    """Base class. Subclasses implement ``run``."""

    name = "agent"

    def execute(self, ctx: AgentContext) -> AgentContext:
        started = time.perf_counter()
        try:
            self.run(ctx)
            ctx.note(
                self.name,
                "ok",
                self.summary(ctx),
                (time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # one agent failing must not lose the run
            ctx.errors.append(f"{self.name}: {exc}")
            ctx.note(
                self.name,
                "error",
                str(exc)[:400],
                (time.perf_counter() - started) * 1000,
            )
        return ctx

    def run(self, ctx: AgentContext) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def summary(self, ctx: AgentContext) -> str:  # pragma: no cover - interface
        return "completed"
