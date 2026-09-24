"""Build the provider-neutral evidence needed by the agent pipeline.

Phase 6 keeps source-control retrieval and CI/CD retrieval as separate
responsibilities. The builder is the boundary where those two streams are
assembled into one immutable-ish context object for the agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.gate.models import GateCheckRequest
from app.providers.base import CICDProvider, PipelineEvent, SourceProvider
from app.observability.telemetry import traced_span


@dataclass(frozen=True)
class FailureContext:
    """Evidence assembled from the request, source and CI/CD providers."""

    event: PipelineEvent
    logs: str = ""
    diff: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    cicd: dict[str, Any] = field(default_factory=dict)

    @property
    def ci_passed(self) -> bool:
        return self.event.ci_passed


class ContextBuilder:
    """Collect source and pipeline evidence without knowing provider details."""

    async def build(
        self,
        request: GateCheckRequest,
        *,
        event: PipelineEvent,
        source: SourceProvider | None,
        cicd: CICDProvider | None,
    ) -> FailureContext:
        logs = request.failure_logs or ""
        diff = request.diff or ""
        log_source = "request" if logs else "none"
        diff_source = "request" if diff else "none"

        with traced_span(
            "context.build",
            **{
                "event.id": event.metadata.get("event_id") or event.metadata.get("delivery_id") or event.run_id,
                "job.id": event.metadata.get("job_id"),
                "provider.source": event.source_provider,
                "provider.cicd": event.cicd_provider,
                "run.id": event.run_id,
            },
        ):
            if not event.ci_passed:
                if cicd is not None and not logs and event.run_id:
                    with traced_span(
                        "context.cicd",
                        **{"provider": event.cicd_provider, "run.id": event.run_id},
                    ):
                        logs = await cicd.get_failure_logs(event)
                    log_source = "cicd_provider"

                if source is not None and not diff:
                    with traced_span(
                        "context.source",
                        **{"provider": event.source_provider, "commit.sha": event.commit_sha},
                    ):
                        diff = await source.get_commit_diff(event)
                    diff_source = "source_provider"

        return FailureContext(
            event=event,
            logs=logs,
            diff=diff,
            source={
                "provider": event.source_provider,
                "repository": event.repository,
                "commit_sha": event.commit_sha,
                "branch": event.branch,
                "diff_source": diff_source,
                "capabilities": {
                    "get_commit_diff": bool(source and source.capabilities and source.capabilities.get_commit_diff),
                    "get_file": bool(source and source.capabilities and source.capabilities.get_file),
                    "download_snapshot": bool(source and source.capabilities and source.capabilities.download_snapshot),
                },
            },
            cicd={
                "provider": event.cicd_provider,
                "pipeline_id": event.pipeline_id,
                "run_id": event.run_id,
                "log_source": log_source,
                "capabilities": {
                    "get_failure_logs": bool(cicd and cicd.capabilities and cicd.capabilities.get_failure_logs),
                },
            },
        )
