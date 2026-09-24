"""Provider-neutral event model, and two separate provider interfaces.

Two independent questions determine how a failure gets remediated:

  * Where does the CODE live?    -> :class:`SourceProvider`
  * Where does the PIPELINE run? -> :class:`CICDProvider`

These are genuinely independent: a GitHub repository can be built by GitHub
Actions, Azure Pipelines, or a self-hosted Jenkins, and an Azure Repos
project can be built by Azure Pipelines or GitHub Actions. Collapsing both
into one "provider" concept (which is what this module used to do) makes
that combination inexpressible. Splitting them is what makes it expressible
-- resolving each independently from a :class:`PipelineEvent` is the whole
point of the split, not a cosmetic reorganization.

Every provider -- source, CI/CD, or (as with the current GitHub
implementation) both at once -- converts its own webhook/event payload or
API response into this normalized shape. From that point on, nothing
downstream -- the gate, the agent pipeline, the policy engine -- ever looks
at a provider-specific shape again.

The interfaces below are intentionally scoped to what the current pipeline
actually calls (verified against ``app/services/github_client.py`` and
``app/gate/service.py``), not a speculative superset of "things a CI/CD API
might support." A method nothing in this codebase calls yet is easy to add
when something needs it; a method guessed at up front and never exercised is
dead weight and an untested liability. See ``app/providers/README.md`` for
what's deliberately excluded and why.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PipelineStatus = Literal["success", "failure", "cancelled", "unknown"]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Concrete operations a provider can perform today.

    Capabilities describe implemented behavior, not the theoretical surface
    of an external API. This lets later providers advertise only operations
    they genuinely support.
    """

    get_commit_diff: bool = False
    get_file: bool = False
    download_snapshot: bool = False
    publish_status: bool = False
    publish_comment: bool = False
    create_fix_pull_request: bool = False
    get_failure_logs: bool = False


class PipelineEvent(BaseModel):
    """A finished CI/CD run, normalized across providers.

    ``repository`` stays a single opaque string (``"owner/name"`` for GitHub)
    rather than being split into organization/project fields at this layer,
    because that is the identity every existing model, route, and test in
    this codebase already keys on (``GateCheckRequest.repository``,
    ``Repository.full_name``, ``GateRun.repository_full_name``). A future
    Azure Repos or GCP source provider still resolves its own
    organization/project internally -- see ``organization``/``project``
    below -- it just also folds the result into one string here so nothing
    downstream needs a second identity scheme.

    ``source_provider`` and ``cicd_provider`` are independent by design (see
    the module docstring). ``provider`` is kept as a single-value shorthand
    for the common case where both are the same, and for backward
    compatibility with code written before the split; when only ``provider``
    is given, both are auto-derived from it. A genuinely cross-provider
    event (an Azure Pipelines build of a GitHub repository, say) sets
    ``source_provider`` and ``cicd_provider`` explicitly instead; ``provider``
    then holds whichever one identified/ingested the event -- normally the
    CI/CD provider, since that's what delivers the webhook.
    """

    provider: str = Field(description='The provider that produced this event, e.g. "github".')
    source_provider: str | None = Field(
        default=None,
        description='Where the code lives, e.g. "github", "azure". Defaults to provider.',
    )
    cicd_provider: str | None = Field(
        default=None,
        description='Where the pipeline ran, e.g. "github", "azure". Defaults to provider.',
    )

    repository: str = Field(description='Normalized repository identity, e.g. "owner/name".')
    commit_sha: str
    branch: str | None = None

    # The provider's own identifiers for this run, kept as strings since an
    # Azure build ID or a GCP build UUID isn't a GitHub-style integer.
    pipeline_id: str | None = Field(
        default=None, description="The pipeline/workflow definition, if the provider has one."
    )
    run_id: str | None = Field(
        default=None, description="The specific run/build id for this execution."
    )

    change_request_number: int | None = Field(
        default=None,
        description="Pull/merge request number this run belongs to, if any.",
    )

    status: PipelineStatus = "unknown"

    # Present for providers whose identity is org/project-scoped rather than
    # a flat "owner/name" (Azure DevOps, GCP). GitHub leaves these unset.
    organization: str | None = None
    project: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)
    # The original payload, kept for provider-specific follow-up work and
    # for audit trails. Never assume downstream, provider-agnostic code
    # reads this -- if the pipeline needs a value, it belongs in a named
    # field above instead.
    raw_payload: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def _default_split_providers(self) -> "PipelineEvent":
        if self.source_provider is None:
            self.source_provider = self.provider
        if self.cicd_provider is None:
            self.cicd_provider = self.provider
        return self

    @property
    def ci_passed(self) -> bool:
        return self.status == "success"

    @property
    def is_cross_provider(self) -> bool:
        """True when the code and the pipeline that built it live in
        different systems -- the exact case the source/CI-CD split exists
        to make representable."""
        return self.source_provider != self.cicd_provider


class SourceProvider(ABC):
    """What the gate needs from wherever the CODE lives.

    Every method here is already called, in this exact shape, from
    ``app/agents/pipeline.py`` or ``app/gate/service.py`` today via
    ``GitHubClient`` -- this interface is an extraction of a working
    implementation, not a guess at one. ``publish_status``,
    ``publish_comment``, and ``create_fix_pull_request`` live here rather
    than on :class:`CICDProvider` because, concretely, on both providers
    designed for so far (GitHub, Azure DevOps), commit statuses, PR
    comments, and PR creation are all repository-scoped API calls, not
    pipeline-scoped ones -- a build system doesn't own the commit, the
    repository does.
    """

    name: str
    capabilities: ProviderCapabilities | None = None

    # ------------------------------------------------------------- reading

    @abstractmethod
    async def get_commit_diff(self, event: PipelineEvent) -> str:
        """Unified diff of the commit under evaluation."""

    @abstractmethod
    async def get_file(self, event: PipelineEvent, path: str, ref: str | None = None) -> str | None:
        """A file's content at ``ref`` (default: the event's commit)."""

    def get_file_sync(self, repository: str, commit_sha: str, path: str) -> str | None:
        """Synchronous file read, for callers outside the event loop.

        The agent pipeline runs as ordinary synchronous code, so it cannot
        ``await`` :meth:`get_file`. Takes ``repository``/``commit_sha``
        directly rather than a full :class:`PipelineEvent` -- a source read
        at a specific commit needs nothing else an event carries. Providers
        without a natural synchronous primitive may implement this by
        wrapping an event loop, but the GitHub provider -- the only one
        implemented so far -- has a real synchronous HTTP path and uses
        that directly.
        """
        raise NotImplementedError

    def download_snapshot_sync(
        self, repository: str, commit_sha: str, dest_dir: str
    ) -> str | None:
        """Download the exact commit into ``dest_dir`` for validation.

        Synchronous for the same reason as :meth:`get_file_sync`. Returns the
        path to the extracted checkout, or ``None`` on any failure.
        """
        raise NotImplementedError

    # ------------------------------------------------------------- writing

    @abstractmethod
    async def publish_status(
        self,
        event: PipelineEvent,
        *,
        verdict: str,
        summary: str,
        detail: str = "",
        details_url: str | None = None,
    ) -> None:
        """Report the gate's verdict back onto the commit/change request."""

    @abstractmethod
    async def publish_comment(self, event: PipelineEvent, body: str) -> None:
        """Post ``body`` to the change request this event belongs to, if any."""

    @abstractmethod
    async def create_fix_pull_request(
        self, event: PipelineEvent, *, patch: str, title: str, body: str
    ) -> dict[str, Any]:
        """Open a change request containing a human-approved fix."""


class CICDProvider(ABC):
    """What the gate needs from wherever the PIPELINE ran.

    Deliberately small: today's pipeline has exactly one need from the
    CI/CD system itself, as opposed to the source repository -- the build
    output of the failed run. Everything else a "CI/CD provider" interface
    might plausibly expose (rerunning a pipeline, listing jobs, fetching
    artifacts) has no caller anywhere in this codebase yet, so it isn't
    declared here; see ``app/providers/README.md`` for what a real second
    CI/CD provider (Azure Pipelines, say) would need to add and where.
    """

    name: str
    capabilities: ProviderCapabilities | None = None

    @abstractmethod
    async def get_failure_logs(self, event: PipelineEvent) -> str:
        """Build/test output for a failed run. Empty string if unavailable."""

    @staticmethod
    def normalize_webhook(payload: dict[str, Any]) -> "PipelineEvent | None":
        """Convert a native webhook payload into a :class:`PipelineEvent`.

        Returns ``None`` when the payload doesn't describe a finished run
        this provider cares about (not implemented on the base class --
        every concrete provider defines its own webhook shape).
        """
        raise NotImplementedError
