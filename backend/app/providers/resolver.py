"""Provider resolution for the normalized gate pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Type

from app.providers.base import CICDProvider, SourceProvider
from app.providers.github import GitHubProvider
from app.providers.azure import AzureProvider
from app.providers.google_cloud_build import GoogleCloudBuildProvider
from app.providers.aws import AWSProvider


@dataclass(frozen=True)
class ProviderPair:
    """The independently resolved source and CI/CD providers."""

    source: SourceProvider
    cicd: CICDProvider


class ProviderResolver:
    """Resolve source and CI/CD adapters independently by provider name.

    GitHub and Azure DevOps are registered from Phases 2-3, and Google Cloud
    Build is registered as a CI/CD provider in Phase 4. The registries remain
    independent so source and CI/CD providers can be combined without
    changing gate orchestration.
    """

    source_registry: dict[str, Type[SourceProvider]] = {
        "github": GitHubProvider,
        "azure": AzureProvider,
    }
    cicd_registry: dict[str, Type[CICDProvider]] = {
        "github": GitHubProvider,
        "azure": AzureProvider,
        "gcp": GoogleCloudBuildProvider,
        "aws": AWSProvider,
    }

    def resolve_source(
        self, provider: str, *, access_token: str | None = None
    ) -> SourceProvider:
        name = self._normalize(provider)
        implementation = self.source_registry.get(name)
        if implementation is None:
            raise ValueError(f"Unsupported source provider: {provider}")
        return implementation(access_token)

    def resolve_cicd(
        self, provider: str, *, access_token: str | None = None
    ) -> CICDProvider:
        name = self._normalize(provider)
        implementation = self.cicd_registry.get(name)
        if implementation is None:
            raise ValueError(f"Unsupported CI/CD provider: {provider}")
        return implementation(access_token)

    def resolve(
        self,
        *,
        source_provider: str,
        cicd_provider: str,
        access_token: str | None = None,
        provider_tokens: dict[str, str | None] | None = None,
    ) -> ProviderPair:
        """Resolve both dimensions independently.

        ``access_token`` remains the backward-compatible single credential.
        ``provider_tokens`` is used when source and CI/CD systems are
        different and therefore cannot safely share one credential.
        """
        tokens = provider_tokens or {}
        source_token = tokens.get(self._normalize(source_provider), access_token)
        cicd_token = tokens.get(self._normalize(cicd_provider), access_token)
        return ProviderPair(
            source=self.resolve_source(source_provider, access_token=source_token),
            cicd=self.resolve_cicd(cicd_provider, access_token=cicd_token),
        )

    @staticmethod
    def _normalize(provider: str) -> str:
        if not provider or not provider.strip():
            raise ValueError("Provider name cannot be empty")
        return provider.strip().lower()


# Backward-compatible function used by existing callers. Keeping this thin
# wrapper avoids an unnecessary API break while the application migrates to
# the resolver object.
def resolve_providers(
    *,
    source_provider: str,
    cicd_provider: str,
    access_token: str | None,
    provider_tokens: dict[str, str | None] | None = None,
) -> ProviderPair:
    return ProviderResolver().resolve(
        source_provider=source_provider,
        cicd_provider=cicd_provider,
        access_token=access_token,
        provider_tokens=provider_tokens,
    )
