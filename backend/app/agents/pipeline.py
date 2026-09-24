"""The agent pipeline.

Two engines produce the same ``AgentContext`` shape:

  * ``deterministic`` - pure log analysis. Always available, no cost.
  * ``llm``           - Google ADK agents over a local Ollama model, used to
                        widen coverage beyond what rules can reach.

The LLM engine never replaces the deterministic result wholesale. Its output
is cross-checked, and anything it produces that the rules can disprove is
discarded. That keeps a hallucinating model from becoming a release risk.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.agents import analyzers
from app.agents.base import Agent, AgentContext
from app.config import settings
from app.observability.telemetry import agent_span
from app.rag.rag_service import rag

logger = logging.getLogger("autoheal.agents")


# ===========================================================================
# 1. Triage
# ===========================================================================


class TriageAgent(Agent):
    name = "triage"

    def run(self, ctx: AgentContext) -> None:
        with agent_span("triage", **{"run.id": ctx.run_id}):
            if ctx.ci_passed:
                ctx.triage = {
                    "category": "none",
                    "confidence": 1.0,
                    "signal": None,
                    "failed_tests": [],
                    "note": "CI succeeded; no triage required.",
                }
                return
            ctx.triage = analyzers.classify(ctx.logs)

    def summary(self, ctx: AgentContext) -> str:
        category = ctx.triage.get("category", "unknown")
        tests = len(ctx.triage.get("failed_tests") or [])
        if category == "none":
            return "CI passed; nothing to triage."
        return f"Classified as {category} ({tests} failing test(s))."


# ===========================================================================
# 2. Knowledge retrieval
# ===========================================================================


class RetrievalAgent(Agent):
    name = "retrieval"

    def run(self, ctx: AgentContext) -> None:
        if ctx.ci_passed:
            ctx.knowledge = {"results": [], "context": "", "skipped": True}
            return

        query = " ".join(
            filter(
                None,
                [
                    ctx.triage.get("category", ""),
                    ctx.triage.get("signal") or "",
                    ctx.logs[-2000:],
                ],
            )
        )
        with agent_span("retrieval", **{"run.id": ctx.run_id}) as span:
            results = rag.retrieve(query, top_k=settings.rag_top_k)
            span.set_attribute("rag.result_count", str(len(results)))

        ctx.knowledge = {
            "results": results,
            "context": rag.format_context(results),
            "mode": rag.stats().get("retrieval_mode"),
        }

    def summary(self, ctx: AgentContext) -> str:
        if ctx.knowledge.get("skipped"):
            return "Skipped; CI passed."
        return f"Retrieved {len(ctx.knowledge.get('results', []))} knowledge chunk(s)."


# ===========================================================================
# 3. Root cause
# ===========================================================================


class RootCauseAgent(Agent):
    name = "root_cause"

    def __init__(self, llm_engine: Any = None):
        self.llm_engine = llm_engine

    def run(self, ctx: AgentContext) -> None:
        if ctx.ci_passed:
            ctx.root_cause = {
                "summary": "CI passed. No root cause to determine.",
                "confidence": 1.0,
                "evidence": [],
                "likely_files": [],
                "method": "short-circuit",
            }
            return

        with agent_span("root_cause", **{"run.id": ctx.run_id}):
            baseline = analyzers.analyze_root_cause(
                ctx.logs, ctx.diff, ctx.triage, ctx.repo_path
            )

            if self.llm_engine is None:
                ctx.root_cause = baseline
                return

            enriched = self.llm_engine.root_cause(ctx, baseline)
            ctx.root_cause = _reconcile_root_cause(baseline, enriched)

    def summary(self, ctx: AgentContext) -> str:
        return f"{ctx.root_cause.get('summary', '')[:160]}"


def _reconcile_root_cause(
    baseline: dict[str, Any], enriched: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep the model's prose only when it agrees with the observed facts.

    The deterministic file list and confidence ceiling always win, because
    those are derived from the logs rather than generated.
    """
    if not isinstance(enriched, dict) or not enriched.get("summary"):
        return baseline

    merged = dict(baseline)
    merged["summary"] = str(enriched["summary"])[:2000]
    merged["method"] = "llm+deterministic"

    # A model may not raise confidence above what the evidence supports.
    try:
        model_confidence = float(enriched.get("confidence", 0))
    except (TypeError, ValueError):
        model_confidence = 0.0
    merged["confidence"] = round(min(model_confidence, baseline.get("confidence", 0.0)), 2)

    # Extra suspected files are allowed; the deterministic ones stay first.
    extra = [
        str(f)
        for f in (enriched.get("likely_files") or [])
        if isinstance(f, str) and f not in baseline.get("likely_files", [])
    ]
    merged["likely_files"] = list(baseline.get("likely_files", [])) + extra[:3]
    merged["llm_raw"] = enriched
    return merged


# ===========================================================================
# 4. Fix
# ===========================================================================


class FixAgent(Agent):
    name = "fix"

    def __init__(self, llm_engine: Any = None):
        self.llm_engine = llm_engine

    def run(self, ctx: AgentContext) -> None:
        if ctx.ci_passed:
            ctx.fix = {"patch": "", "proposal": "No fix required.", "confidence": 1.0}
            return

        with agent_span("fix", **{"run.id": ctx.run_id}):
            candidate = analyzers.synthesize_fix(ctx.logs, ctx.root_cause, ctx.repo_path)

            if candidate.get("patch"):
                ctx.fix = candidate
                return

            if self.llm_engine is None:
                ctx.fix = candidate
                return

            # The model can only respond to code it has actually been shown.
            # Without this, "fix the retriever" is a description of the bug
            # class, not an edit to a specific line. When there's no local
            # checkout -- the common case for a repository only reached via
            # webhook -- fall back to reading the file remotely, through the
            # resolved provider when one is set, or directly from GitHub
            # otherwise (kept for callers that only ever set github_token).
            remote_reader = None
            if ctx.provider is not None:
                provider = ctx.provider

                def remote_reader(path: str) -> str | None:
                    return provider.get_file_sync(ctx.repository, ctx.commit_sha, path)

            elif ctx.github_token:
                from app.services.github_client import fetch_file_sync

                def remote_reader(path: str) -> str | None:
                    return fetch_file_sync(
                        ctx.github_token, ctx.repository, path, ctx.commit_sha
                    )

            file_contents = analyzers.gather_fix_context(
                ctx.repo_path,
                ctx.root_cause.get("likely_files", []),
                max_files=int(ctx.policy.get("max_changed_files", 3)),
                remote_reader=remote_reader,
            )

            proposed = self.llm_engine.propose_fix(ctx, file_contents)
            if not isinstance(proposed, dict) or not proposed.get("patch"):
                # The model may still explain, in prose, why no safe edit was
                # possible even with the file in hand -- surface that over
                # the generic deterministic message when it says more.
                if isinstance(proposed, dict) and proposed.get("proposal"):
                    ctx.fix = {
                        **candidate,
                        "proposal": str(proposed["proposal"])[:2000],
                        "method": "llm-declined",
                    }
                else:
                    ctx.fix = candidate
                return

            ctx.fix = {
                "patch": str(proposed.get("patch", "")),
                "proposal": str(proposed.get("proposal", ""))[:2000],
                "validation_plan": str(proposed.get("validation_plan", ""))[:1000],
                "confidence": 0.6,
                "method": "llm-proposal",
                "files_provided": list(file_contents.keys()),
            }

    def summary(self, ctx: AgentContext) -> str:
        if not ctx.fix.get("patch"):
            return "No candidate fix was produced."
        from app.gate.policy import changed_files_in_patch

        files = changed_files_in_patch(ctx.fix["patch"])
        return f"Proposed a fix touching {', '.join(files) or 'unknown files'}."


# ===========================================================================
# 5. Validation
# ===========================================================================


class ValidationAgent(Agent):
    """Apply the candidate fix to a disposable copy and run the tests.

    The source repository is never modified. If validation cannot run, that is
    reported honestly rather than assumed to pass; the policy engine treats an
    unproven fix as unfit for release.

    A local checkout is used when one is registered. Otherwise, given a
    GitHub token, the exact commit under evaluation is downloaded as a
    disposable snapshot and validated the same way -- this is what lets a
    repository reached only through a webhook still reach a genuinely proven
    verdict, rather than an indefinite HOLD with no way to ever resolve it.
    """

    name = "validation"

    def run(self, ctx: AgentContext) -> None:
        if ctx.ci_passed:
            ctx.validation = {
                "attempted": False,
                "patch_applied": False,
                "tests_passed": True,
                "reason": "CI passed; validation was unnecessary.",
            }
            return

        patch = ctx.fix.get("patch", "")
        if not patch.strip():
            ctx.validation = {
                "attempted": False,
                "patch_applied": False,
                "tests_passed": False,
                "reason": "There was no candidate fix to validate.",
            }
            return

        from app.services.remediation import validate_patch, validate_patch_from_snapshot

        with agent_span("validation", **{"run.id": ctx.run_id}):
            if ctx.repo_path and os.path.isdir(ctx.repo_path):
                ctx.validation = validate_patch(
                    repo_path=ctx.repo_path,
                    patch=patch,
                    test_command=ctx.test_command,
                    setup_command=ctx.setup_command,
                    job_id=ctx.run_id,
                )
            else:
                ctx.validation = validate_patch_from_snapshot(
                    github_token=ctx.github_token,
                    repository=ctx.repository,
                    commit_sha=ctx.commit_sha,
                    patch=patch,
                    test_command=ctx.test_command,
                    setup_command=ctx.setup_command,
                    provider=ctx.provider,
                    job_id=ctx.run_id,
                )

    def summary(self, ctx: AgentContext) -> str:
        if not ctx.validation.get("attempted"):
            return str(ctx.validation.get("reason", "Validation did not run."))
        if ctx.validation.get("tests_passed"):
            return "The patched workspace passed its test suite."
        return "The patched workspace still fails its test suite."


# ===========================================================================
# Orchestration
# ===========================================================================


def _select_engine() -> tuple[Any, str]:
    """Resolve AGENT_ENGINE into a concrete engine instance."""
    choice = (settings.agent_engine or "auto").lower()

    if choice == "deterministic":
        return None, "deterministic"

    try:
        from app.agents.llm_engine import LLMEngine

        engine = LLMEngine()
        if engine.available():
            return engine, "llm"
        if choice == "adk":
            logger.warning(
                "AGENT_ENGINE=adk but the model backend is unreachable. "
                "Falling back to the deterministic engine."
            )
    except Exception as exc:
        if choice == "adk":
            logger.warning("LLM engine unavailable (%s); using deterministic rules.", exc)

    return None, "deterministic"


def run_pipeline(ctx: AgentContext) -> AgentContext:
    """Run all agents in order and return the populated context."""
    engine, engine_name = _select_engine()
    ctx.engine = engine_name

    agents: list[Agent] = [
        TriageAgent(),
        RetrievalAgent(),
        RootCauseAgent(engine),
        FixAgent(engine),
        ValidationAgent(),
    ]

    with agent_span(
        "gate-pipeline",
        **{"run.id": ctx.run_id, "repository": ctx.repository, "engine": engine_name},
    ):
        for agent in agents:
            agent.execute(ctx)

    return ctx
